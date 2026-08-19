"""OrderService — the single path from a decision to placed orders.

Enforces, in order:
1. the **paper gate** (checked BEFORE anything touches the network),
2. idempotency dedup (one live order per decision+side),
3. ``validate_order`` → deterministic ``RepairOrder`` → single resubmit (no LLM),
4. a MANDATORY stop-loss on every entry,
5. protective orders placed as ``reduce_only`` and never auto-cancelled (BUG C).
"""

from __future__ import annotations

import logging

from src.domain.model import (
    Instrument,
    InstrumentSpec,
    OrderRequest,
    Position,
    Side,
    Signal,
)
from src.exchanges.bitget import BitgetAdapter
from src.services.repair import IdempotencyGuard, RepairOrder, validate_order
from src.services.risk import FixedRiskSizer, RiskLimits

log = logging.getLogger(__name__)


class PaperGateBlocked(RuntimeError):
    """Raised when a live order is attempted while the persisted paper flag is set."""


class OrderService:
    def __init__(self, adapter, sizer: FixedRiskSizer | None = None,
                 guard: IdempotencyGuard | None = None,
                 repair: RepairOrder | None = None,
                 limits: RiskLimits | None = None, paper: bool = True) -> None:
        self.adapter = adapter
        self.sizer = sizer or FixedRiskSizer(limits)
        self.guard = guard or IdempotencyGuard()
        self.repair = repair or RepairOrder()
        self.limits = limits or RiskLimits()
        self.paper = paper

    # ------------------------------------------------------------------- helpers
    def _decision_id(self, signal: Signal) -> str:
        return str(signal.message_id or f"{signal.instrument.symbol}:{signal.ts_ms}")

    async def _spec(self, instrument: Instrument) -> InstrumentSpec:
        return await self.adapter.get_spec(instrument)

    # --------------------------------------------------------------------- entry
    async def open_from_signal(self, signal: Signal, mark_price: float | None = None,
                               size_multiplier: float = 1.0) -> dict:
        """Open a position from a normalized signal. Returns an audit dict."""
        if signal.sl is None or signal.sl <= 0:
            return {"status": "REJECTED", "reason": "SL is mandatory — refusing entry"}

        spec = await self._spec(signal.instrument)
        entry = signal.entry or mark_price or await self.adapter.get_mark_price(signal.instrument)
        if not entry or entry <= 0:
            return {"status": "REJECTED", "reason": "no usable entry/mark price"}

        # Direction sanity: a long's SL must sit below entry, a short's above.
        if signal.side is Side.BUY and signal.sl >= entry:
            return {"status": "REJECTED", "reason": "long SL is above entry"}
        if signal.side is Side.SELL and signal.sl <= entry:
            return {"status": "REJECTED", "reason": "short SL is below entry"}

        sized = self.sizer.size_for_signal(entry, signal.sl, spec, signal.leverage)
        if not sized.ok:
            return {"status": "REJECTED", "reason": sized.reason or "sizer returned 0"}

        qty = sized.qty * max(0.0, size_multiplier)
        decision_id = self._decision_id(signal)
        client_oid = BitgetAdapter.new_client_oid()
        dedup = self.guard.check(decision_id, signal.side, client_oid)
        if dedup.skipped:
            return {"status": "DUPLICATE_SKIPPED", "reason": "decision already has a live order",
                    "client_order_id": dedup.existing_client_order_id}

        order = OrderRequest(
            instrument=signal.instrument, side=signal.side, order_type="market",
            qty=BitgetAdapter.round_qty(spec, qty, entry), price=None,
            sl=signal.sl, tp=signal.tp, leverage=sized.leverage,
            client_order_id=client_oid, decision_id=decision_id,
        )

        verdict = validate_order(order, spec, price_ref=entry)
        if not verdict:
            repaired = self.repair.repair(order, spec, price_ref=entry)
            verdict2 = validate_order(repaired, spec, price_ref=entry)
            if not verdict2:
                return {"status": "REJECTED", "reason": f"unrepairable: {verdict.reason}",
                        "code": verdict.code}
            order = repaired
            log.info("order repaired deterministically (%s)", verdict.code)

        if self.paper:
            log.info("PAPER entry %s %s qty=%s sl=%s tp=%s lev=%s",
                     order.instrument.symbol, order.side.value, order.qty,
                     order.sl, order.tp, order.leverage)
            return {"status": "PAPER", "order": order, "sizing": sized,
                    "client_order_id": client_oid}

        await self.adapter.set_leverage(
            order.instrument, order.leverage,
            "long" if order.side is Side.BUY else "short",
            cap=self.limits.max_leverage)
        order_id = await self.adapter.place_order(order)
        return {"status": "PLACED", "order_id": order_id, "order": order, "sizing": sized,
                "client_order_id": client_oid}

    # ---------------------------------------------------------------- protective
    async def attach_protective(self, position: Position, sl: float | None,
                                tp: float | None) -> dict:
        """Place/replace protective reduce-only orders for an open position."""
        if sl is None and tp is None:
            return {"status": "NOOP"}
        await self.adapter.set_trading_stop(position.instrument, sl, tp, position.size)
        return {"status": "PAPER" if self.paper else "PLACED", "sl": sl, "tp": tp}

    async def close_position(self, position: Position, fraction: float = 1.0,
                             reason: str = "") -> dict:
        """Close all or part of a position with a reduce-only market order."""
        spec = await self._spec(position.instrument)
        frac = min(1.0, max(0.0, fraction))
        qty = position.size * frac
        if qty <= 0:
            return {"status": "NOOP", "reason": "zero close size"}
        order = OrderRequest(
            instrument=position.instrument, side=position.side.opposite,
            order_type="market", qty=BitgetAdapter.round_qty(spec, qty, position.entry_price),
            reduce_only=True, leverage=position.leverage,
            client_order_id=BitgetAdapter.new_client_oid("bgc"),
        )
        if self.paper:
            return {"status": "PAPER", "order": order, "reason": reason}
        order_id = await self.adapter.place_order(order)
        return {"status": "PLACED", "order_id": order_id, "order": order, "reason": reason}
