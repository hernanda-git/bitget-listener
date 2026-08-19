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


class LiquidationPriceUnknown(RuntimeError):
    """Raised rather than silently booking a liquidation at 0.00 (BUG B)."""


class PositionManager:
    def __init__(self, adapter, order_service, ledger=None, stream=None) -> None:
        self.adapter = adapter
        self.orders = order_service
        self.ledger = ledger
        self.stream = stream
        self.positions: dict[str, Position] = {}

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
        res = await self.orders.attach_protective(pos, new_sl, pos.tp_price)
        self.upsert(Position(**{**pos.__dict__, "sl_price": new_sl}))
        return {"status": res.get("status", "OK"), "action": "sl_to_entry", "sl": new_sl}

    async def book_partial(self, symbol: str, fraction: float = 0.5) -> dict:
        """``tp1 booked``: close part of the position, keep protection on the rest."""
        pos = self.get(symbol)
        if pos is None:
            return {"status": "NO_POSITION", "symbol": symbol}
        # Snapshot the size BEFORE closing: the adapter mutates position state, so
        # reading pos.size afterwards would compute the remainder off the reduced size.
        original_size = pos.size
        res = await self.orders.close_position(pos, fraction, reason="tp1_booked")
        remaining = original_size * (1 - min(1.0, max(0.0, fraction)))
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
        return {"status": res.get("status"), "action": "full_close"}

    async def modify_stop(self, symbol: str, sl: float | None = None,
                          tp: float | None = None) -> dict:
        pos = self.get(symbol)
        if pos is None:
            return {"status": "NO_POSITION", "symbol": symbol}
        res = await self.orders.attach_protective(pos, sl, tp)
        self.upsert(Position(**{**pos.__dict__,
                                "sl_price": sl if sl is not None else pos.sl_price,
                                "tp_price": tp if tp is not None else pos.tp_price}))
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

    async def on_mark(self, symbol: str, mark: float) -> dict | None:
        """Real-time stop enforcement: close if the mark has breached the stop."""
        pos = self.get(symbol)
        if pos is None or not self.stop_breached(pos, mark):
            return None
        log.warning("stop breached %s mark=%s sl=%s — closing", symbol, mark, pos.sl_price)
        return await self.full_close(symbol, reason="stop_breached")

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
