"""
OpenClaw Local Execution Agent — Rate Limiter

Sliding-window rate limiter using an in-memory deque of timestamps.
Thread-safe via an asyncio lock.

When the limit is exceeded the caller receives a ``RateLimitExceeded``
exception which the router translates into a structured rejection.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

from config import RATE_LIMIT_PER_MINUTE


class RateLimitExceeded(Exception):
    """Raised when the per-minute action cap is hit."""

    def __init__(self, limit: int, window_seconds: int = 60):
        super().__init__(
            f"Rate limit exceeded: {limit} actions per {window_seconds}s."
        )
        self.limit = limit
        self.window_seconds = window_seconds


class SlidingWindowRateLimiter:
    """
    Allows at most ``max_requests`` actions within a rolling
    ``window_seconds`` window.
    """

    def __init__(
        self,
        max_requests: int = RATE_LIMIT_PER_MINUTE,
        window_seconds: int = 60,
    ):
        self._max = max_requests
        self._window = window_seconds
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """
        Record a new request timestamp.

        Raises ``RateLimitExceeded`` if the window is full.
        """
        async with self._lock:
            now = time.monotonic()
            cutoff = now - self._window

            # Evict expired entries.
            while self._timestamps and self._timestamps[0] < cutoff:
                self._timestamps.popleft()

            if len(self._timestamps) >= self._max:
                raise RateLimitExceeded(self._max, self._window)

            self._timestamps.append(now)

    async def remaining(self) -> int:
        """Return how many requests are still available in the window."""
        async with self._lock:
            now = time.monotonic()
            cutoff = now - self._window
            while self._timestamps and self._timestamps[0] < cutoff:
                self._timestamps.popleft()
            return max(0, self._max - len(self._timestamps))
