"""Tests for app.algo_ratelimit.RateLimiter — the order gateway's fairness gate."""

from app.algo_ratelimit import RateLimiter


class TestRateLimiter:
    def test_burst_then_refusal(self):
        rl = RateLimiter(rate=10, burst=10)
        t = 1000.0
        # Ten in the same instant are fine (that's the burst)…
        assert all(rl.allow("u", now=t) for _ in range(10))
        # …the eleventh in the same instant is not.
        assert not rl.allow("u", now=t)

    def test_refills_over_time(self):
        rl = RateLimiter(rate=10, burst=10)
        t = 0.0
        for _ in range(10):
            rl.allow("u", now=t)
        assert not rl.allow("u", now=t)
        # Half a second later, five tokens are back.
        assert rl.take("u", 5, now=t + 0.5) == 5
        assert rl.take("u", 1, now=t + 0.5) == 0

    def test_take_returns_partial_grant(self):
        rl = RateLimiter(rate=10, burst=10)
        t = 0.0
        assert rl.take("u", 7, now=t) == 7      # 3 left
        assert rl.take("u", 5, now=t) == 3      # only 3 remain
        assert rl.take("u", 1, now=t) == 0

    def test_keys_are_independent(self):
        rl = RateLimiter(rate=10, burst=10)
        t = 0.0
        for _ in range(10):
            rl.allow("a", now=t)
        assert not rl.allow("a", now=t)
        assert rl.allow("b", now=t)             # different user, full bucket

    def test_retry_after_is_positive_when_empty(self):
        rl = RateLimiter(rate=10, burst=10)
        t = 0.0
        for _ in range(10):
            rl.allow("u", now=t)
        wait = rl.retry_after("u", now=t)
        assert 0 < wait <= 0.1                   # ~1/rate seconds
        assert rl.retry_after("fresh-user", now=t) == 0.0

    def test_never_exceeds_burst_ceiling(self):
        rl = RateLimiter(rate=10, burst=10)
        # Idle for a long time — tokens must cap at burst, not accumulate forever.
        assert rl.take("u", 10, now=10_000.0) == 10
        assert rl.take("u", 1, now=10_000.0) == 0
