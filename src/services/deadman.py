"""DeadManSwitch — heartbeat watchdog that halts trading when the feed dies.

If the market stream goes stale we are flying blind: stops cannot be enforced in
real time, so the switch (1) halts new entries, (2) cancels working ENTRY orders,
and (3) optionally flattens.

**Critical safety rule (BUG C):** the switch NEVER cancels protective ``reduce_only``
orders. Stripping stops from an open position while blind is strictly worse than
doing nothing, which is precisely the failure mode the Binance ancestor suffered.
Bitget V2 mix exposes no server-side auto-cancel for this product, so the watchdog
is client-side (verified: no ``set-auto-cancel`` equivalent).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

log = logging.getLogger(__name__)


class DeadManSwitch:
    def __init__(self, stream, adapter, position_manager=None, notifier=None,
                 ttl_s: float = 60.0, check_interval_s: float = 5.0,
                 flatten: bool = False) -> None:
        self.stream = stream
        self.adapter = adapter
        self.positions = position_manager
        self.notifier = notifier
        self.ttl_s = ttl_s
        self.check_interval_s = check_interval_s
        self.flatten = flatten
        self.halted = False
        self.tripped_at: float | None = None
        self._task: asyncio.Task | None = None
        self._running = False

    @property
    def trading_allowed(self) -> bool:
        return not self.halted

    def is_stale(self, now: float | None = None) -> bool:
        last = getattr(self.stream, "last_msg_ts", 0.0) or 0.0
        if not last:
            return True
        return ((now or time.time()) - last) > self.ttl_s

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.check_interval_s)
            try:
                if self.is_stale() and not self.halted:
                    await self.trip("market feed stale")
                elif not self.is_stale() and self.halted:
                    await self.recover()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("deadman loop error: %s", exc)

    async def trip(self, reason: str) -> dict:
        """Halt trading. Cancels ENTRY orders only; protective orders are preserved."""
        self.halted = True
        self.tripped_at = time.time()
        log.error("DEAD-MAN'S SWITCH TRIPPED: %s — halting entries, preserving stops", reason)
        preserved = 0
        try:
            # cancel_all is implemented to skip reduce_only/plan orders (BUG C)
            await self.adapter.cancel_all(None)
            rows = await self.adapter.get_open_orders(None)
            from src.exchanges.bitget import BitgetAdapter
            preserved = sum(1 for r in rows if BitgetAdapter.is_protective(r))
        except Exception as exc:
            log.error("deadman cancel_all failed: %s", exc)
        flattened = []
        if self.flatten and self.positions is not None:
            for symbol in list(getattr(self.positions, "positions", {})):
                with contextlib.suppress(Exception):
                    await self.positions.full_close(symbol, reason="deadman_flatten")
                    flattened.append(symbol)
        if self.notifier is not None:
            with contextlib.suppress(Exception):
                await self.notifier.send(
                    f"DEAD-MAN'S SWITCH: {reason}. Entries halted. "
                    f"{preserved} protective order(s) preserved.")
        return {"halted": True, "reason": reason, "protective_preserved": preserved,
                "flattened": flattened}

    async def recover(self) -> dict:
        self.halted = False
        log.warning("dead-man's switch cleared — feed healthy, entries re-enabled")
        if self.notifier is not None:
            with contextlib.suppress(Exception):
                await self.notifier.send("Feed healthy again — trading re-enabled.")
        return {"halted": False}
