"""
Restricted Python sandbox for user-submitted trading strategies.
===============================================================

Students upload a strategy for the "Market Simulation Py" game and the engine
calls it once per tick.  That code is untrusted, so it never reaches a plain
``exec``.  Three layers stand between a submission and the interpreter:

1. **AST whitelist** (:func:`validate_source`) — only a small set of node types
   survives.  No ``import``, no ``class``, no ``with``, no ``async``, no
   generators.  Every attribute whose name starts with ``_`` is rejected, which
   closes the classic ``().__class__.__bases__[0].__subclasses__()`` escape,
   and a handful of underscore-free frame/code attributes are blocked by name.

2. **Stripped globals** (:data:`SAFE_BUILTINS`) — ``__builtins__`` is replaced
   with a fixed dict holding pure-computation helpers only.  ``open``, ``eval``,
   ``exec``, ``compile``, ``getattr``, ``globals`` and friends simply do not
   exist inside a strategy, so there is nothing to reach for even if a name
   slipped past the AST pass.

3. **Deadline guard** (:meth:`Strategy.call`) — the call runs under a
   ``sys.settrace`` hook that raises :class:`StrategyTimeout` once the wall
   clock or the executed-line budget runs out, so ``while True:`` costs the
   author their tick rather than the server.

Residual risk worth knowing about: the guard stops *looping*, not a single
allocation.  ``[0] * 10**9`` is one bytecode and can still bloat memory, so
this sandbox is a strong barrier against code execution and a best-effort
barrier against denial of service.  Treat it as safe against escapes, not as a
substitute for running the game on an instance you are happy to lose.
"""

from __future__ import annotations

import ast
import math as _math
import random as _random
import sys
import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

__all__ = [
    "SandboxError",
    "StrategyTimeout",
    "Strategy",
    "validate_source",
    "compile_strategy",
    "MAX_SOURCE_BYTES",
]

# Submissions larger than this are rejected before parsing.
MAX_SOURCE_BYTES: int = 40_000

# Wall-clock and executed-line budgets applied to a single strategy call.
DEFAULT_TIMEOUT_SEC: float = 0.25
DEFAULT_LINE_BUDGET: int = 400_000

# Guards on the builtins that can cheaply produce enormous work.
MAX_RANGE_LEN: int = 1_000_000
MAX_POW_EXPONENT: int = 1_000

# Longest single print line kept, and how many lines of output we retain.
MAX_PRINT_CHARS: int = 400


class SandboxError(Exception):
    """Submitted source violated a sandbox rule (or failed to parse)."""

    def __init__(self, message: str, lineno: Optional[int] = None):
        self.lineno = lineno
        super().__init__(f"line {lineno}: {message}" if lineno else message)


class StrategyTimeout(Exception):
    """A strategy call blew its wall-clock or executed-line budget."""


# ─────────────────────────────────────────────────────────────────────────────
# 1. AST whitelist
# ─────────────────────────────────────────────────────────────────────────────

_ALLOWED_NODES = frozenset(
    name
    for name in (
        # module / statements
        "Module", "Expr", "Assign", "AugAssign", "AnnAssign", "Return", "Pass",
        "Break", "Continue", "If", "For", "While", "FunctionDef", "Delete",
        "Try", "TryStar", "ExceptHandler", "Raise", "Assert", "Global", "Nonlocal",
        # expressions
        "Call", "Name", "Attribute", "Subscript", "Slice", "Starred", "Lambda",
        "IfExp", "NamedExpr", "Constant", "JoinedStr", "FormattedValue",
        "List", "Tuple", "Dict", "Set", "ListComp", "SetComp", "DictComp",
        "GeneratorExp", "comprehension", "arguments", "arg", "keyword",
        "alias", "withitem",
        # operators
        "BinOp", "UnaryOp", "BoolOp", "Compare",
        "Add", "Sub", "Mult", "Div", "FloorDiv", "Mod",
        "BitAnd", "BitOr", "BitXor", "Invert",
        "UAdd", "USub", "Not", "And", "Or",
        "Eq", "NotEq", "Lt", "LtE", "Gt", "GtE", "Is", "IsNot", "In", "NotIn",
        # contexts
        "Load", "Store", "Del",
    )
    if hasattr(ast, name)
)

# Node types rejected with a message explaining why, so students get a useful
# error instead of a bare "not allowed".
_REJECTED_NODES: Dict[str, str] = {
    "Import": "import is not available — use the math, stats and random helpers instead",
    "ImportFrom": "import is not available — use the math, stats and random helpers instead",
    "ClassDef": "class definitions are not allowed — use functions and dicts",
    "With": "with blocks are not allowed",
    "AsyncWith": "async code is not allowed",
    "AsyncFor": "async code is not allowed",
    "AsyncFunctionDef": "async code is not allowed",
    "Await": "async code is not allowed",
    "Yield": "generators are not allowed — return a list instead",
    "YieldFrom": "generators are not allowed — return a list instead",
    "Pow": "the ** operator is not allowed — use pow(a, b) instead",
    "MatMult": "the @ operator is not allowed",
    "LShift": "the << operator is not allowed",
    "RShift": "the >> operator is not allowed",
}

# Names a strategy may never read or bind.
_FORBIDDEN_NAMES = frozenset({
    "__import__", "__builtins__", "__globals__", "__class__", "__subclasses__",
    "eval", "exec", "compile", "open", "input", "breakpoint", "exit", "quit",
    "globals", "locals", "vars", "dir", "help", "getattr", "setattr", "delattr",
    "hasattr", "type", "object", "super", "memoryview", "id", "license",
    "credits", "copyright",
})

# Attributes that would leak a frame, a code object or the class graph but do
# not start with an underscore, so the prefix rule alone would miss them.
_FORBIDDEN_ATTRS = frozenset({
    "gi_frame", "gi_code", "gi_yieldfrom", "cr_frame", "cr_code", "cr_await",
    "ag_frame", "ag_code", "f_globals", "f_builtins", "f_locals", "f_back",
    "f_code", "func_globals", "func_code", "func_builtins", "co_consts",
    "co_names", "tb_frame", "tb_next", "mro", "register", "format_map",
    "send", "throw", "close", "gi_running",
})


class _Validator(ast.NodeVisitor):
    def __init__(self) -> None:
        # Operator nodes carry no position, so remember the last one that did
        # and report violations against it.
        self._lineno: Optional[int] = None

    def generic_visit(self, node: ast.AST) -> None:
        self._lineno = getattr(node, "lineno", None) or self._lineno
        name = type(node).__name__
        reason = _REJECTED_NODES.get(name)
        if reason:
            raise SandboxError(reason, self._lineno)
        if name not in _ALLOWED_NODES:
            raise SandboxError(f"{name} is not allowed in a strategy", self._lineno)
        super().generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in _FORBIDDEN_NAMES or node.id.startswith("__"):
            raise SandboxError(f"the name {node.id!r} is not available", node.lineno)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("_") or node.attr in _FORBIDDEN_ATTRS:
            raise SandboxError(f"attribute {node.attr!r} is not accessible", node.lineno)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node.decorator_list:
            raise SandboxError("decorators are not allowed", node.lineno)
        if node.name.startswith("_"):
            raise SandboxError("function names may not start with '_'", node.lineno)
        self.generic_visit(node)

    def visit_arg(self, node: ast.arg) -> None:
        if node.arg.startswith("_"):
            raise SandboxError("argument names may not start with '_'", node.lineno)
        self.generic_visit(node)


def validate_source(source: str) -> ast.Module:
    """Parse and whitelist-check ``source``.

    Returns the parsed module on success; raises :class:`SandboxError` with a
    line number on the first violation.
    """
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise SandboxError(f"strategy is too large (limit {MAX_SOURCE_BYTES} bytes)")
    try:
        tree = ast.parse(source, filename="<strategy>", mode="exec")
    except SyntaxError as e:
        raise SandboxError(f"syntax error: {e.msg}", e.lineno) from None
    _Validator().visit(tree)
    return tree


# ─────────────────────────────────────────────────────────────────────────────
# 2. Stripped globals
# ─────────────────────────────────────────────────────────────────────────────

def _safe_range(*args: int) -> range:
    r = range(*args)
    if len(r) > MAX_RANGE_LEN:
        raise ValueError(f"range() is limited to {MAX_RANGE_LEN} values in a strategy")
    return r


def _safe_pow(base: float, exp: float, mod: Optional[int] = None) -> Any:
    if abs(exp) > MAX_POW_EXPONENT:
        raise ValueError(f"pow() exponent is limited to ±{MAX_POW_EXPONENT} in a strategy")
    return pow(base, exp) if mod is None else pow(int(base), int(exp), mod)


def _mean(values) -> float:
    seq = list(values)
    if not seq:
        raise ValueError("mean() of an empty sequence")
    return sum(seq) / len(seq)


def _median(values) -> float:
    seq = sorted(values)
    if not seq:
        raise ValueError("median() of an empty sequence")
    mid = len(seq) // 2
    return float(seq[mid]) if len(seq) % 2 else (seq[mid - 1] + seq[mid]) / 2


def _stdev(values) -> float:
    seq = list(values)
    if len(seq) < 2:
        return 0.0
    mu = sum(seq) / len(seq)
    return _math.sqrt(sum((x - mu) ** 2 for x in seq) / (len(seq) - 1))


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else (high if value > high else value)


SAFE_BUILTINS: Dict[str, Any] = {
    # constructors & conversion
    "bool": bool, "int": int, "float": float, "str": str, "repr": repr,
    "list": list, "dict": dict, "tuple": tuple, "set": set, "frozenset": frozenset,
    # numeric
    "abs": abs, "round": round, "min": min, "max": max, "sum": sum,
    "divmod": divmod, "pow": _safe_pow,
    # sequence
    "len": len, "range": _safe_range, "sorted": sorted, "reversed": reversed,
    "enumerate": enumerate, "zip": zip, "map": map, "filter": filter,
    "any": any, "all": all, "isinstance": isinstance,
    # exceptions a strategy may raise or catch
    "Exception": Exception, "ValueError": ValueError, "TypeError": TypeError,
    "KeyError": KeyError, "IndexError": IndexError, "ZeroDivisionError": ZeroDivisionError,
    "ArithmeticError": ArithmeticError, "RuntimeError": RuntimeError,
    # constants
    "True": True, "False": False, "None": None,
}

# Pre-bound stand-ins for the modules a strategy would otherwise import.
_MATH_NS = SimpleNamespace(
    pi=_math.pi, e=_math.e, inf=_math.inf, tau=_math.tau,
    sqrt=_math.sqrt, log=_math.log, log10=_math.log10, exp=_math.exp,
    floor=_math.floor, ceil=_math.ceil, fabs=_math.fabs, copysign=_math.copysign,
    sin=_math.sin, cos=_math.cos, tan=_math.tan, atan=_math.atan, atan2=_math.atan2,
    hypot=_math.hypot, isnan=_math.isnan, isinf=_math.isinf, isfinite=_math.isfinite,
)

_STATS_NS = SimpleNamespace(mean=_mean, median=_median, stdev=_stdev, clamp=_clamp)


def _random_namespace(seed: Optional[int]) -> SimpleNamespace:
    """A private RNG per strategy so one author cannot disturb another's draws."""
    rng = _random.Random(seed)
    return SimpleNamespace(
        random=rng.random, uniform=rng.uniform, randint=rng.randint,
        randrange=rng.randrange, choice=rng.choice, shuffle=rng.shuffle,
        sample=rng.sample, gauss=rng.gauss, seed=rng.seed,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Deadline guard + the compiled strategy handle
# ─────────────────────────────────────────────────────────────────────────────

class Strategy:
    """A validated, compiled strategy plus the namespace it runs in.

    The namespace persists between calls, so module-level variables (and
    ``global`` assignments inside ``on_tick``) carry state across ticks the way
    an author would expect.
    """

    def __init__(
        self,
        source: str,
        code: Any,
        namespace: Dict[str, Any],
        on_print: Optional[Callable[[str], None]] = None,
    ):
        self.source = source
        self.code = code
        self.namespace = namespace
        self._on_print = on_print
        self.loaded = False

    # -- output ----------------------------------------------------------
    def _print(self, *args: Any, **kwargs: Any) -> None:
        if self._on_print is None:
            return
        sep = kwargs.get("sep", " ")
        text = sep.join(str(a) for a in args)
        if len(text) > MAX_PRINT_CHARS:
            text = text[:MAX_PRINT_CHARS] + "…"
        self._on_print(text)

    # -- execution -------------------------------------------------------
    def _run_guarded(self, fn: Callable[[], Any], timeout: float, line_budget: int) -> Any:
        """Run ``fn`` under a wall-clock + executed-line watchdog."""
        deadline = time.monotonic() + timeout
        remaining = [line_budget]

        def tracer(frame, event, arg):  # noqa: ANN001 - CPython trace signature
            if event == "line":
                remaining[0] -= 1
                if remaining[0] <= 0:
                    raise StrategyTimeout(
                        f"strategy executed more than {line_budget} lines in one call"
                    )
                if time.monotonic() > deadline:
                    raise StrategyTimeout(
                        f"strategy exceeded its {timeout * 1000:.0f}ms time budget"
                    )
            return tracer

        previous = sys.gettrace()
        sys.settrace(tracer)
        try:
            return fn()
        finally:
            sys.settrace(previous)

    def load(self, timeout: float = DEFAULT_TIMEOUT_SEC * 4) -> None:
        """Execute the module body once, defining the author's functions."""
        self._run_guarded(
            lambda: exec(self.code, self.namespace),  # noqa: S102 - sandboxed by construction
            timeout,
            DEFAULT_LINE_BUDGET,
        )
        self.loaded = True

    def has(self, name: str) -> bool:
        return callable(self.namespace.get(name))

    def call(
        self,
        name: str,
        *args: Any,
        timeout: float = DEFAULT_TIMEOUT_SEC,
        line_budget: int = DEFAULT_LINE_BUDGET,
    ) -> Any:
        """Call a function the strategy defined, under the deadline guard.

        Raises :class:`StrategyTimeout` on budget exhaustion, or whatever the
        strategy itself raised.
        """
        fn = self.namespace.get(name)
        if not callable(fn):
            raise SandboxError(f"strategy does not define a {name}() function")
        return self._run_guarded(lambda: fn(*args), timeout, line_budget)


def compile_strategy(
    source: str,
    on_print: Optional[Callable[[str], None]] = None,
    seed: Optional[int] = None,
) -> Strategy:
    """Validate and compile ``source`` into a ready-to-load :class:`Strategy`.

    Raises :class:`SandboxError` if the source breaks a sandbox rule.  This does
    not execute the module body — call :meth:`Strategy.load` for that.
    """
    tree = validate_source(source)
    code = compile(tree, filename="<strategy>", mode="exec")

    strategy = Strategy(source, code, {}, on_print)
    builtins = dict(SAFE_BUILTINS)
    builtins["print"] = strategy._print
    strategy.namespace.update({
        "__builtins__": builtins,
        "math": _MATH_NS,
        "stats": _STATS_NS,
        "random": _random_namespace(seed),
    })
    return strategy


def check_strategy(source: str) -> List[str]:
    """Validate ``source`` and report problems as a list of human-readable strings.

    Empty list means the submission is acceptable.  Used by the editor's
    "Check" button so students see errors before a run starts.
    """
    problems: List[str] = []
    try:
        strategy = compile_strategy(source)
        strategy.load()
    except SandboxError as e:
        return [str(e)]
    except StrategyTimeout as e:
        return [f"module body timed out: {e}"]
    except Exception as e:  # noqa: BLE001 - author's own error, surfaced as text
        return [f"error while loading strategy: {type(e).__name__}: {e}"]

    if not strategy.has("on_tick"):
        problems.append("strategy must define on_tick(ctx)")
    return problems
