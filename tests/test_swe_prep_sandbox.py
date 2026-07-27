"""Tests for app.swe_prep_sandbox — the restricted interpreter for player strategies.

The escape tests matter most: a submission that gets past these runs in the same
process as the Firestore credentials, so each one pins down a route out that a
student (or an attacker with an account) would realistically reach for.
"""

import pytest

from app.swe_prep_sandbox import (
    MAX_SOURCE_BYTES,
    SandboxError,
    StrategyTimeout,
    check_strategy,
    compile_strategy,
    validate_source,
)


def wrap(body: str) -> str:
    """Put ``body`` inside an on_tick, indented."""
    indented = "\n".join("    " + line for line in body.strip().splitlines())
    return f"def on_tick(ctx):\n{indented}\n"


# ── Escapes that must be refused ───────────────────────────────────────────


class TestBlockedEscapes:
    @pytest.mark.parametrize("source", [
        "import os",
        "import os.path",
        "from os import system",
        "from subprocess import run",
    ])
    def test_imports_rejected(self, source: str):
        with pytest.raises(SandboxError, match="import"):
            validate_source(source)

    @pytest.mark.parametrize("expr", [
        "[].__class__",
        "().__class__.__bases__",
        "(lambda: 0).__globals__",
        "on_tick.__code__",
        "ctx.__dict__",
    ])
    def test_dunder_attributes_rejected(self, expr: str):
        with pytest.raises(SandboxError, match="not accessible|not available"):
            validate_source(wrap(f"return {expr}"))

    @pytest.mark.parametrize("expr", [
        "eval('1+1')",
        "exec('x=1')",
        "compile('1', '<s>', 'eval')",
        "open('/etc/passwd')",
        "getattr(ctx, 'keys')",
        "setattr(ctx, 'x', 1)",
        "globals()",
        "locals()",
        "vars()",
        "type(ctx)",
        "object()",
        "__import__('os')",
    ])
    def test_dangerous_builtins_rejected(self, expr: str):
        with pytest.raises(SandboxError, match="not available"):
            validate_source(wrap(f"return {expr}"))

    def test_frame_attributes_rejected(self):
        with pytest.raises(SandboxError, match="not accessible"):
            validate_source(wrap("return ctx.gi_frame"))

    @pytest.mark.parametrize("source, match", [
        ("class Widget:\n    pass\n", "class"),
        ("async def on_tick(ctx):\n    return []\n", "async"),
        ("def on_tick(ctx):\n    yield 1\n", "generator"),
        ("with open('x') as f:\n    pass\n", "with"),
    ])
    def test_forbidden_constructs(self, source: str, match: str):
        with pytest.raises(SandboxError, match=match):
            validate_source(source)

    def test_decorators_rejected(self):
        with pytest.raises(SandboxError, match="decorator"):
            validate_source("@staticmethod\ndef on_tick(ctx):\n    return []\n")

    def test_names_cannot_be_rebound_to_smuggle_a_builtin(self):
        with pytest.raises(SandboxError, match="not available"):
            validate_source(wrap("eval = 1\nreturn eval"))

    def test_builtins_are_not_reachable_at_runtime(self):
        """Even a valid-looking program has no route to the real builtins."""
        strategy = compile_strategy(wrap("return len(ctx)"))
        strategy.load()
        assert "open" not in strategy.namespace["__builtins__"]
        assert "__import__" not in strategy.namespace["__builtins__"]


# ── Denial-of-service guards ───────────────────────────────────────────────


class TestResourceGuards:
    def test_infinite_loop_times_out(self):
        strategy = compile_strategy("def on_tick(ctx):\n    while True:\n        pass\n")
        strategy.load()
        with pytest.raises(StrategyTimeout):
            strategy.call("on_tick", {}, timeout=0.05)

    def test_line_budget_stops_a_long_loop(self):
        strategy = compile_strategy("def on_tick(ctx):\n    n = 0\n    for i in range(999999):\n        n = n + i\n    return n\n")
        strategy.load()
        with pytest.raises(StrategyTimeout):
            strategy.call("on_tick", {}, timeout=10.0, line_budget=500)

    def test_huge_range_rejected(self):
        strategy = compile_strategy(wrap("return len(range(10000000))"))
        strategy.load()
        with pytest.raises(ValueError, match="range"):
            strategy.call("on_tick", {})

    def test_pow_operator_rejected_in_favour_of_capped_builtin(self):
        with pytest.raises(SandboxError, match=r"\*\*"):
            validate_source(wrap("return 2 ** 99999999"))

        strategy = compile_strategy(wrap("return pow(2, 99999999)"))
        strategy.load()
        with pytest.raises(ValueError, match="exponent"):
            strategy.call("on_tick", {})

    def test_oversized_source_rejected(self):
        with pytest.raises(SandboxError, match="too large"):
            validate_source("x = 1\n" * (MAX_SOURCE_BYTES // 3))

    def test_trace_function_is_restored_after_a_call(self):
        import sys
        before = sys.gettrace()
        strategy = compile_strategy(wrap("return 1"))
        strategy.load()
        strategy.call("on_tick", {})
        assert sys.gettrace() is before


# ── Things that must keep working ──────────────────────────────────────────


class TestAllowedPrograms:
    def test_arithmetic_and_helpers(self):
        strategy = compile_strategy(wrap(
            "values = [1, 2, 3, 4]\n"
            "return [stats.mean(values), math.sqrt(16), stats.clamp(99, 0, 10)]"
        ))
        strategy.load()
        assert strategy.call("on_tick", {}) == [2.5, 4.0, 10]

    def test_state_persists_between_calls(self):
        strategy = compile_strategy(
            "count = 0\n"
            "def on_tick(ctx):\n"
            "    global count\n"
            "    count = count + 1\n"
            "    return count\n"
        )
        strategy.load()
        assert [strategy.call("on_tick", {}) for _ in range(3)] == [1, 2, 3]

    def test_comprehensions_and_control_flow(self):
        strategy = compile_strategy(wrap(
            "out = []\n"
            "for i in range(5):\n"
            "    if i % 2 == 0:\n"
            "        out.append(i)\n"
            "return [x * 2 for x in out]"
        ))
        strategy.load()
        assert strategy.call("on_tick", {}) == [0, 4, 8]

    def test_print_is_captured_not_emitted(self):
        captured = []
        strategy = compile_strategy(wrap("print('hello', 42)\nreturn []"), on_print=captured.append)
        strategy.load()
        strategy.call("on_tick", {})
        assert captured == ["hello 42"]

    def test_try_except_is_allowed(self):
        strategy = compile_strategy(wrap(
            "try:\n"
            "    x = 1 / 0\n"
            "except ZeroDivisionError:\n"
            "    return 'caught'\n"
            "return 'no'"
        ))
        strategy.load()
        assert strategy.call("on_tick", {}) == "caught"

    def test_random_is_seeded_per_strategy(self):
        a = compile_strategy(wrap("return random.random()"), seed=42)
        b = compile_strategy(wrap("return random.random()"), seed=42)
        a.load()
        b.load()
        assert a.call("on_tick", {}) == b.call("on_tick", {})


# ── check_strategy: the editor's front door ────────────────────────────────


class TestCheckStrategy:
    def test_accepts_a_valid_strategy(self):
        assert check_strategy("def on_tick(ctx):\n    return []\n") == []

    def test_requires_on_tick(self):
        problems = check_strategy("def helper(x):\n    return x\n")
        assert problems == ["strategy must define on_tick(ctx)"]

    def test_reports_syntax_errors_with_a_line(self):
        problems = check_strategy("def on_tick(ctx)\n    return []\n")
        assert len(problems) == 1
        assert "line 1" in problems[0]

    def test_reports_an_error_raised_at_module_level(self):
        problems = check_strategy("x = 1 / 0\ndef on_tick(ctx):\n    return []\n")
        assert "ZeroDivisionError" in problems[0]
