"""Per-user sliding-window rate limit (in memory).

In-memory is correct here: there is exactly one bot worker (long polling allows only
one getUpdates consumer per token). A restart resets the windows, which is acceptable.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, per_minute: int, per_hour: int, clock=time.monotonic):
        self.limits = ((60.0, per_minute), (3600.0, per_hour))
        self.clock = clock
        self._hits: dict[int, deque[float]] = defaultdict(deque)

    def check(self, user_id: int) -> float:
        """Record a hit and return 0.0, or return seconds to wait (the hit is NOT recorded)."""
        now = self.clock()
        hits = self._hits[user_id]
        while hits and now - hits[0] >= 3600.0:
            hits.popleft()
        for window, limit in self.limits:
            in_window = [t for t in hits if now - t < window]
            if len(in_window) >= limit:
                return max(1.0, window - (now - in_window[0]))
        hits.append(now)
        return 0.0
