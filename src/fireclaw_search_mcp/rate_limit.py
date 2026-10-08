"""Async minimum-interval rate limiter for the upstream Firecrawl endpoint.

Per SPECS §5: FIRECRAWL_MIN_INTERVAL_MS controls the minimum spacing between
upstream calls. Concurrent calls queue on a shared mutex; the second one
waits until `min_interval` has elapsed since the first one started.
"""
from __future__ import annotations

import asyncio
import time


class MinIntervalLimiter:
    def __init__(self, min_interval_ms: int):
        self._min = max(0.0, min_interval_ms / 1000.0)
        self._lock = asyncio.Lock()
        self._last = 0.0

    @property
    def min_interval_s(self) -> float:
        return self._min

    async def wait(self) -> None:
        if self._min <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            delay = self._last + self._min - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()
