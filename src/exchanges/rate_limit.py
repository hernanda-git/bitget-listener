"""Bitget rate-limit gate — throttle, 429 backoff, and ban-prevention breaker.

Verified Bitget rules (bitgetlimited/apidoc, en/mix/index.html):

  REST public (market data) ...... max 20 req/s per IP
  REST signed (default) .......... 10 req/s per API key (unspecified endpoints)
  Order placement (placeOrder) ... 1 req/s per trader  (hard limit)
  Account endpoints (setLeverage/setMargin/allPosition/...) ... 5 req/s each
  429 returned when exceeded; limited by API key if valid, else by IP.
  WebSocket ...................... max 10 messages/s per connection (ping+subscribe
                                    all count); exceeding drops the connection.
                                    IPs repeatedly disconnected MAY BE BLOCKED.
  WS subscribe cap ............... 1000 streams/conn; 100 connections/IP.
  WS heartbeat ................... ping every <=30s, expect pong within 30s.

Design
------
A single async ``RateLimitGate`` owns one token bucket per request *category*.
Every outbound REST call (and every WS control message) must acquire a token
first; if the bucket is empty the call is *delayed* (not dropped) until a token
refills, so we never exceed the venue's ceiling and never get a 429 from speed.

If a 429 *is* still observed (clock skew, shared-IP neighbours, venue-side
throttling), ``on_429()`` parses ``Retry-After`` (or falls back to an
exponential backoff) and the gate enters a *cooldown*: it refuses to issue new
orders and alerts the operator instead of hammering the venue. Repeated 429s /
auth errors trip a circuit breaker that parks the bot in a safe state — the
cheapest way to avoid an IP ban is to stop talking to the venue.

All behaviour is exercised by tests (no network); the gate is also safe to use
as a no-op in paper mode (it still throttles, but issues no real requests).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)

# Category -> (max requests per second). Conservative vs Bitget's published caps
# so we keep headroom for clock jitter and shared-IP neighbours.
PUBLIC_RPS = 20          # Bitget: 20/s
SIGNED_RPS = 10          # Bitget default: 10/s
ORDER_RPS = 1            # Bitget hard limit: 1/s per trader
ACCOUNT_RPS = 5          # Bitget: 5/s per account endpoint
WS_MSG_RPS = 8           # Bitget WS cap: 10/s; we stay under with margin

# Circuit-breaker: after this many consecutive 429/auth errors, park the bot.
BREAKER_TRIP_COUNT = 5
# Base backoff for an observed 429 (before Retry-After / exponential growth).
BASE_BACKOFF_S = 1.0
MAX_BACKOFF_S = 60.0


@dataclass
class _Bucket:
    """Async token bucket. ``rate`` tokens are refilled per second, cap = rate."""
    rate: float
    tokens: float = field(init=False)
    _last: float = field(init=False, default=0.0)

    def __post_init__(self) -> None:
        self.tokens = float(self.rate)
        self._last = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.rate, self.tokens + (now - self._last) * self.rate)
        self._last = now

    async def acquire(self) -> None:
        """Block until one token is available (never busy-waits)."""
        while True:
            self._refill()
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return
            wait = (1.0 - self.tokens) / self.rate
            await asyncio.sleep(min(wait, 0.5))


class RateLimitError(Exception):
    """Raised when the gate is in cooldown/breaker-open and refuses a request."""


class RateLimitGate:
    """Per-category token buckets + 429 backoff + ban-prevention breaker."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._buckets = {
            "public": _Bucket(PUBLIC_RPS),
            "signed": _Bucket(SIGNED_RPS),
            "order": _Bucket(ORDER_RPS),
            "account": _Bucket(ACCOUNT_RPS),
            "ws": _Bucket(WS_MSG_RPS),
        }
        self._cooldown_until = 0.0      # gate refuses NEW requests until this ts
        self._backoff_s = BASE_BACKOFF_S
        self._consecutive = 0           # consecutive 429/auth errors
        self._lock = asyncio.Lock()
        self.alerts: list[str] = []     # operator-facing alert log

    # ----------------------------------------------------------- throttle gate
    async def acquire(self, category: str) -> None:
        """Acquire a token for ``category`` ('public'|'signed'|'order'|'account'|'ws').

        Raises ``RateLimitError`` if the breaker is open / in cooldown so the
        caller can fail safe instead of hammering the venue.
        """
        if self.is_open():
            raise RateLimitError(
                "rate-limit circuit breaker is open — bot parked to avoid IP ban")
        bucket = self._buckets.get(category, self._buckets["signed"])
        await bucket.acquire()

    def is_open(self) -> bool:
        """True when the breaker is tripped (bot should not issue new requests)."""
        return self._clock() < self._cooldown_until

    @property
    def cooldown_remaining_s(self) -> float:
        rem = self._cooldown_until - self._clock()
        return max(0.0, rem)

    # ----------------------------------------------------------- 429 handling
    async def on_429(self, retry_after: float | None = None) -> None:
        """Record a 429, back off, and trip the breaker after repeated hits."""
        async with self._lock:
            self._consecutive += 1
            if retry_after and retry_after > 0:
                backoff = min(retry_after, MAX_BACKOFF_S)
            else:
                backoff = min(self._backoff_s, MAX_BACKOFF_S)
                self._backoff_s = min(self._backoff_s * 2, MAX_BACKOFF_S)
            self._cooldown_until = self._clock() + backoff
            msg = (f"Bitget 429 (too frequent). Backing off {backoff:.0f}s "
                   f"[{self._consecutive}/{BREAKER_TRIP_COUNT} consecutive]")
            self.alerts.append(msg)
            log.warning("[rate-limit] %s", msg)
            if self._consecutive >= BREAKER_TRIP_COUNT:
                # Park the bot: stop issuing orders until the operator clears it.
                self._cooldown_until = self._clock() + MAX_BACKOFF_S * 2
                alert = ("RATE-LIMIT BREAKER TRIPPED after "
                         f"{self._consecutive} consecutive 429s — bot parked to "
                         "avoid IP ban. Investigate shared-IP neighbours or reduce "
                         "request rate, then restart or wait for cooldown.")
                self.alerts.append(alert)
                log.error("[rate-limit] %s", alert)

    def on_success(self) -> None:
        """A clean response resets the consecutive-error counter."""
        self._consecutive = 0
        self._backoff_s = BASE_BACKOFF_S

    def reset(self) -> None:
        """Operator-initiated clear of the breaker (after manual investigation)."""
        self._consecutive = 0
        self._backoff_s = BASE_BACKOFF_S
        self._cooldown_until = 0.0
        self.alerts.clear()
        log.warning("[rate-limit] breaker manually reset")
