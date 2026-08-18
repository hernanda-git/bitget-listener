"""P11: rate-limit gate — throttle, 429 backoff, and ban-prevention breaker.

No network. Proves the bot can never exceed Bitget's published ceilings and
parks itself (instead of getting IP-banned) when the venue throttles it.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from src.exchanges.rate_limit import RateLimitGate, RateLimitError


def _advancing_clock():
    """A clock that advances by real wall-time but is injectable."""
    return time.monotonic


@pytest.mark.asyncio
async def test_token_bucket_throttles_to_rate():
    """'order' bucket is 1/s — 3 acquires must take >= ~2s."""
    gate = RateLimitGate(clock=_advancing_clock())
    t0 = time.monotonic()
    for _ in range(3):
        await gate.acquire("order")   # 1/s bucket
    elapsed = time.monotonic() - t0
    assert elapsed >= 1.9, f"order bucket should throttle to ~1/s, took {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_categories_independent():
    """'public' (20/s) and 'order' (1/s) buckets are separate."""
    gate = RateLimitGate()
    await gate.acquire("order")
    await gate.acquire("public")   # should not block


@pytest.mark.asyncio
async def test_on_429_backoff_and_breaker_trip():
    start = 1000.0
    state = {"t": start}

    def clock():
        return state["t"]

    gate = RateLimitGate(clock=clock)
    for _ in range(5):
        await gate.on_429(retry_after=2.0)
        state["t"] += 3.0   # advance past each cooldown window
    assert gate.is_open(), "breaker should be open after 5 consecutive 429s"
    with pytest.raises(RateLimitError):
        await gate.acquire("signed")


@pytest.mark.asyncio
async def test_on_429_without_retry_after_uses_exponential():
    gate = RateLimitGate(clock=lambda: 1000.0)
    await gate.on_429(None)
    assert gate.cooldown_remaining_s >= 1.0


@pytest.mark.asyncio
async def test_success_resets_consecutive_counter():
    gate = RateLimitGate(clock=lambda: 1000.0)
    await gate.on_429(1.0)
    gate.on_success()
    assert gate._consecutive == 0


@pytest.mark.asyncio
async def test_reset_clears_breaker():
    gate = RateLimitGate(clock=lambda: 1000.0)
    for _ in range(5):
        await gate.on_429(1.0)
    assert gate.is_open()
    gate.reset()
    assert not gate.is_open()
    await gate.acquire("signed")
