"""
Token-bucket rate limiting for the Market Simulation Py order gateway.
=====================================================================

Strategies run on players' own machines and reach the market only through the
HTTP order API, so "how fast may a bot trade" is enforced here rather than by
any in-process budget. Each key (a user id) gets a bucket that refills at a
fixed rate and holds a small burst. An order costs one token; a request for
more tokens than are available is refused without partially draining below the
request, so callers get clean "allowed / try again" semantics.

The service is pinned to a single instance, so an in-memory limiter is the
whole story — there is no second process to coordinate with.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict


@dataclass
class _Bucket:
    tokens: float
    last: float


class RateLimiter:
    """A per-key token bucket.

    :param rate: tokens added per second.
    :param burst: maximum tokens a key can bank (the most it can spend at once).
    """

    def __init__(self, rate: float, burst: float):
        self.rate = float(rate)
        self.burst = float(burst)
        self._buckets: Dict[str, _Bucket] = {}
        self._last_prune = 0.0

    def _refill(self, key: str, now: float) -> _Bucket:
        b = self._buckets.get(key)
        if b is None:
            b = _Bucket(tokens=self.burst, last=now)
            self._buckets[key] = b
            return b
        elapsed = now - b.last
        if elapsed > 0:
            b.tokens = min(self.burst, b.tokens + elapsed * self.rate)
            b.last = now
        return b

    def take(self, key: str, cost: int = 1, now: float | None = None) -> int:
        """Spend up to ``cost`` tokens for ``key``.

        Returns how many were actually granted (0..cost). Fewer than ``cost``
        means the bucket ran dry partway — the caller decides what to do with a
        partial grant (the gateway applies that many orders and reports the
        rest as rate-limited).
        """
        now = time.monotonic() if now is None else now
        b = self._refill(key, now)
        granted = min(cost, int(b.tokens))
        if granted > 0:
            b.tokens -= granted
        self._maybe_prune(now)
        return granted

    def allow(self, key: str, now: float | None = None) -> bool:
        """True if a single token was available and spent."""
        return self.take(key, 1, now) == 1

    def retry_after(self, key: str, now: float | None = None) -> float:
        """Seconds until at least one token is available for ``key``."""
        now = time.monotonic() if now is None else now
        b = self._buckets.get(key)
        if b is None or b.tokens >= 1:
            return 0.0
        return max(0.0, (1 - b.tokens) / self.rate)

    def _maybe_prune(self, now: float) -> None:
        """Drop buckets that have sat full and untouched, so memory stays bounded."""
        if now - self._last_prune < 60:
            return
        self._last_prune = now
        stale = [
            key for key, b in self._buckets.items()
            if now - b.last > 300 and b.tokens >= self.burst
        ]
        for key in stale:
            self._buckets.pop(key, None)

    def reset(self) -> None:
        self._buckets.clear()
