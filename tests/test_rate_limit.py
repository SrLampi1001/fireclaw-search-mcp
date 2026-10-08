"""Tests for the async min-interval rate limiter."""
from __future__ import annotations

import asyncio
import time

import pytest

from fireclaw_search_mcp.rate_limit import MinIntervalLimiter


async def test_disabled_when_zero():
    limiter = MinIntervalLimiter(0)
    start = time.monotonic()
    for _ in range(5):
        await limiter.wait()
    elapsed = time.monotonic() - start
    assert elapsed < 0.05  # 5 zero-interval waits must be effectively instant


async def test_serial_waits_are_spaced():
    limiter = MinIntervalLimiter(100)
    start = time.monotonic()
    for _ in range(3):
        await limiter.wait()
    elapsed = time.monotonic() - start
    # 3 calls × 100ms = 300ms minimum, but allow scheduler slop.
    assert elapsed >= 0.18
    assert elapsed < 0.6


async def test_concurrent_waits_queue():
    limiter = MinIntervalLimiter(150)

    async def task():
        await limiter.wait()
        return time.monotonic()

    start = time.monotonic()
    times = await asyncio.gather(task(), task(), task())
    gaps = [t - start for t in times]
    # Spaced roughly 0ms, 150ms, 300ms.
    assert gaps[1] - gaps[0] >= 0.1
    assert gaps[2] - gaps[1] >= 0.1
