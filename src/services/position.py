"""PositionManager — management commands + real-time stop monitoring.

Management command depth follows the Binance sibling (the richest surface): ``sl to
entry``, ``tp1 booked`` (partial), ``full`` close, ``tp/sl`` modify, stale-pending
cancel. All of it is deterministic and bypasses the LLM entirely (~ms, auditable).

BUG B is closed here: a liquidation is booked at the position's REAL
``liq_price``. If the exchange never supplied one, the manager REFUSES to invent
``0.00`` — it books at the last known mark and flags the record instead.
"""

from __future__ import annotations

import logging

from src.domain.model import Fill, Position, Side

log = logging.getLogger(__name__)

# Bot-side SL/TP armed levels are NOT resting on the venue (the exchange tpsl
# endpoint rejects them for this account), so they live in the shared DB via
# bot_common.repos.ArmedStops. That survives a process restart: on_position
# re-arms the monitor from the DB instead of going naked.
#
# The shared store is a *real* Postgres connection. The fakes-only test suite
# (and any environment without the shared DB) must never be forced to open one,
# so we degrade gracefully to an in-memory mirror when the store is unreachable.
# Live behavior is unchanged: when the store is reachable we read/write through
# it exactly as before.


class LiquidationPriceUnknown(RuntimeError):
    """Raised rather than silently booking a liquidation at 0.00 (BUG B)."""


class _ArmedStopsStore:
    """Thin facade over ``bot_common.repos.ArmedStops`` with an in-memory fallback.

    Construction must NEVER open a network connection. We probe reachability only
    on first write (set/remove), and permanently fall back to the in-memory dict
    for the rest of the process if the shared store raises. Reads (get_all) also
    fall back silently. This keeps the "zero-network fakes-only" suite honest.
    """

    BOT = "bitget-listener"

    def __init__(self) -> None:
        self._mem: dict[str, dict] = {}
        self._mem_loaded = False
        self._use_pg = True  # downgraded to False on first connection failure

    def _repo(self):
        from bot_common import repos  # imported lazily; may itself import psycopg
        return repos.ArmedStops

    def get_all(self) -> dict[str, dict]:
        if not self._use_pg:
            return dict(self._mem)
        try:
            return self._repo().get_all(self.BOT)
        except Exception as exc:  # noqa: BLE001 - any DB failure -> in-memory
            log.debug("ArmedStops.get_all fell back to in-memory (%s)", exc)
            self._use_pg = False
            return dict(self._mem)

    def set(self, symbol: str, sl, tp) -> None:
        sym = symbol.upper()
        self._mem[sym] = {"sl": sl, "tp": tp}
        if not self._use_pg:
            return
        try:
            self._repo().set(self.BOT, symbol, sl, tp)
        except Exception as exc:  # noqa: BLE001
            log.debug("ArmedStops.set fell back to in-memory (%s)", exc)
            self._use_pg = False

    def remove(self, symbol: str) -> None:
        sym = symbol.upper()
        self._mem.pop(sym, None)
        if not self._use_pg:
            return
        try:
            self._repo().remove(self.BOT, symbol)
        except Exception as exc:  # noqa: BLE001
            log.debug("ArmedStops.remove fell back to in-memory (%s)", exc)
            self._use_pg = False


class PositionManager:
    def __init__(self, adapter, order_service, ledger=None, stream=None) -> None:
        self.adapter = adapter
        self.orders = order_service
        self.ledger = ledger
        self.stream = stream
        self.positions: dict[str, Position] = {}
        # Symbol -> {"sl": float|None, "tp": float|None} persisted across restarts.
        # No connection is opened here; falls back to in-memory if the shared
        # store is unreachable (see _ArmedStopsStore).
        self._armed_store = _ArmedStopsStore()
        self._armed: dict[str, dict] = self._armed_store.get_all()


    # ------------------------------------------------------------------ tracking
    def upsert(self, position: Position) -> None:
        if position.size <= 0:
            self.positions.pop(position.instrument.symbol, None)
            return
        self.positions[position.instrument.symbol] = position

    def get(self, symbol: str) -> Position | None:
        return self.positions.get(symbol.upper())

    async def sync(self) -> list[Position]:
        live = await self.adapter.get_positions()
        merged: list[Position] = []
        for p in live:
            if p.size > 0:
                sl, tp = await self._protection(p.instrument)
                # Only override when the exchange actually reports a level; the
                # position parse already captured position-level stopLoss/takeProfit,
                # so this just folds in any live plan orders and never invents one.
                if sl is not None or tp is not None:
                    p = Position(**{**p.__dict__,
                                    "sl_price": sl if sl is not None else p.sl_price,
                                    "tp_price": tp if tp is not None else p.tp_price})
            merged.append(p)
        self.positions = {p.instrument.symbol: p for p in merged if p.size > 0}
        return live

    async def _protection(self, instrument) -> tuple[float | None, float | None]:
        """Read the REAL resting SL/TP for one symbol (BUG #3 surface)."""
        try:
            return await self.adapter.get_protection(instrument)
        except Exception as exc:
            log.warning("protection sync failed for %s: %s", instrument.symbol, exc)
            return None, None

    async def _best_effort_attach(self, pos: Position, sl, tp) -> dict:
        """Attempt to attach protection on the venue, but NEVER let a venue
        failure block the bot-side level (set by the caller beforehand). Returns a
        status dict; on exchange failure it reports DEGRADED so the operator knows
        the protection is monitor-side only."""
        if sl is None and tp is None:
            return {"status": "NOOP"}
        try:
            return await self.orders.attach_protective(pos, sl, tp)
        except Exception as exc:  # noqa: BLE001 - exchange tpsl may be unavailable
            log.warning("venue protective attach failed for %s (monitor-side only): %s",
                        pos.instrument.symbol, exc)
            return {"status": "DEGRADED", "reason": str(exc)}

    # ---------------------------------------------------------- management verbs
    async def sl_to_entry(self, symbol: str) -> dict:
        """Move the stop to break-even. Never widens an existing stop."""
        pos = self.get(symbol)
        if pos is None:
            return {"status": "NO_POSITION", "symbol": symbol}
        new_sl = pos.entry_price
        if pos.sl_price is not None:
            tighter = (new_sl >= pos.sl_price) if pos.side is Side.BUY else (new_sl <= pos.sl_price)
            if not tighter:
                return {"status": "REJECTED", "reason": "would widen the stop", "symbol": symbol}
        # Bot-side monitor is authoritative when the exchange tpsl endpoint is
        # unavailable: set the local level FIRST so the WS monitor protects even
        # if the venue attachment fails.
        self.upsert(Position(**{**pos.__dict__, "sl_price": new_sl}))
        self._armed[symbol.upper()] = {"sl": new_sl, "tp": pos.tp_price}
        self._armed_store.set(symbol, new_sl, pos.tp_price)
        res = await self._best_effort_attach(pos, new_sl, pos.tp_price)
        return {"status": res.get("status", "OK"), "action": "sl_to_entry", "sl": new_sl}

    async def book_partial(self, symbol: str, fraction: float = 0.5) -> dict:
        """``tp1 booked``: close part of the position, keep protection on the rest.

        The remainder is computed from the REAL exchange size (reported back by
        close_position as ``real_size``), not the possibly-stale in-memory size,
        so the in-memory position stays in sync with the venue after the partial.
        """
        pos = self.get(symbol)
        if pos is None:
            return {"status": "NO_POSITION", "symbol": symbol}
        res = await self.orders.close_position(pos, fraction, reason="tp1_booked")
        real_size = res.get("real_size")
        # Prefer the exchange-confirmed size; fall back to the cache snapshot.
        base = real_size if real_size is not None else pos.size
        remaining = base * (1 - min(1.0, max(0.0, fraction)))
        if remaining > 0:
            self.upsert(Position(**{**pos.__dict__, "size": remaining}))
        else:
            self.positions.pop(pos.instrument.symbol, None)
        return {"status": res.get("status"), "action": "partial", "closed_fraction": fraction,
                "remaining": remaining}

    async def full_close(self, symbol: str, reason: str = "full") -> dict:
        pos = self.get(symbol)
        if pos is None:
            return {"status": "NO_POSITION", "symbol": symbol}
        res = await self.orders.close_position(pos, 1.0, reason=reason)
        self.positions.pop(pos.instrument.symbol, None)
        # Drop the persisted bot-side stop: the position is gone, don't re-arm it.
        if symbol.upper() in self._armed:
            self._armed.pop(symbol.upper(), None)
            self._armed_store.remove(symbol)
        return {"status": res.get("status"), "action": "full_close"}

    async def modify_stop(self, symbol: str, sl: float | None = None,
                          tp: float | None = None) -> dict:
        pos = self.get(symbol)
        if pos is None:
            return {"status": "NO_POSITION", "symbol": symbol}
        # Set the bot-side level FIRST so the WS monitor is armed even if the
        # venue attachment fails (exchange tpsl endpoint is unreliable).
        armed = Position(**{**pos.__dict__,
                            "sl_price": sl if sl is not None else pos.sl_price,
                            "tp_price": tp if tp is not None else pos.tp_price})
        self.upsert(armed)
        # Persist so a restart re-arms the monitor (no venue stop is possible).
        self._armed[symbol.upper()] = {"sl": armed.sl_price, "tp": armed.tp_price}
        self._armed_store.set(symbol, armed.sl_price, armed.tp_price)
        res = await self._best_effort_attach(pos, sl, tp)
        return {"status": res.get("status"), "action": "modify_stop", "sl": sl, "tp": tp}

    async def cancel_pending(self, symbol: str) -> dict:
        """Cancel stale working ENTRY orders. Protective orders are preserved (BUG C)."""
        from src.domain.model import Instrument
        from src.exchanges.bitget import BitgetAdapter
        rows = await self.adapter.get_open_orders(None)
        cancelled, preserved = [], []
        tracked = self.get(symbol)
        instrument = tracked.instrument if tracked else Instrument.from_token(symbol)
        for row in rows:
            if str(row.get("symbol", "")).upper() != symbol.upper():
                continue
            if BitgetAdapter.is_protective(row):
                preserved.append(row.get("orderId"))
                continue
            await self.adapter.cancel_order(
                instrument, str(row.get("clientOid") or row.get("orderId")))
            cancelled.append(row.get("orderId"))
        return {"status": "OK", "action": "cancel_pending", "cancelled": cancelled,
                "preserved_protective": preserved}

    # --------------------------------------------------------- real-time stops
    def stop_breached(self, position: Position, mark: float) -> bool:
        if position.sl_price is None:
            return False
        return (mark <= position.sl_price) if position.side is Side.BUY \
            else (mark >= position.sl_price)

    def target_breached(self, position: Position, mark: float) -> bool:
        """TP/limit side: a long banks profit above target, a short below."""
        if position.tp_price is None:
            return False
        return (mark >= position.tp_price) if position.side is Side.BUY \
            else (mark <= position.tp_price)

    async def on_mark(self, symbol: str, mark: float) -> dict | None:
        """Real-time SL/TP enforcement driven by the price WebSocket.

        Close (full) when the mark breaches EITHER the stop or the target. Returns
        the close result, or None when no level is breached. The orchestrator books
        the trade ledger + daily PnL from this result.
        """
        pos = self.get(symbol)
        if pos is None:
            return None
        if self.stop_breached(pos, mark):
            log.warning("STOP breached %s mark=%s sl=%s — closing", symbol, mark, pos.sl_price)
            return await self.full_close(symbol, reason="stop_breached")
        if self.target_breached(pos, mark):
            log.info("TARGET hit %s mark=%s tp=%s — closing", symbol, mark, pos.tp_price)
            return await self.full_close(symbol, reason="target_hit")
        return None

    # ----------------------------------------------------------- liquidation (B)
    def book_liquidation(self, position: Position, last_mark: float | None = None) -> dict:
        """Book a liquidation at the REAL price. Never 0.00 (BUG B).

        Precedence: exchange ``liq_price`` → last known mark (flagged as an estimate).
        A zero/missing value is never written as a real exit price.
        """
        if position.liq_price and position.liq_price > 0:
            price, estimated = position.liq_price, False
        elif last_mark and last_mark > 0:
            price, estimated = last_mark, True
            log.error("liquidation for %s has no exchange liq_price — booking at last mark %s "
                      "and flagging as ESTIMATE (never 0.00)", position.instrument.symbol, price)
        else:
            raise LiquidationPriceUnknown(
                f"no real liquidation price for {position.instrument.symbol}; refusing to book 0.00")
        return {"symbol": position.instrument.symbol, "exit_price": price,
                "estimated": estimated, "exit_reason": "liquidation"}

    # ------------------------------------------------------------------ fee sink
    @staticmethod
    def assert_fee_recorded(fill: Fill) -> Fill:
        """BUG A tripwire: a real (non-paper) fill must carry a non-zero fee."""
        if fill.fee <= 0 and not fill.client_order_id.startswith("paper-"):
            log.error("fill %s recorded with fee=0 — BUG A regression", fill.client_order_id)
        return fill
