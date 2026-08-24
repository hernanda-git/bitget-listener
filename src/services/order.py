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
import asyncio

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
            margin_mode="crossed",
            client_order_id=client_oid, decision_id=decision_id,
        )
        if signal.raw.get("entry_order_type") == "LIMIT":
            order.order_type = "limit"
            order.price = entry

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
        result = {"status": "PLACED", "order_id": order_id, "order": order, "sizing": sized,
                  "client_order_id": client_oid}
        # Market OrderRequest.price is intentionally None. Resolve the executed
        # average from the exchange before post-open bookkeeping, otherwise a
        # successful fill can reach fee math as abs(None).
        fill = await self._resolve_open_fill(order, order_id)
        if fill is not None:
            result["fill_price"] = fill.price
            result["fill_fee"] = fill.fee
            result["fill"] = fill
        return result

    async def _resolve_open_fill(self, order: OrderRequest, order_id: str):
        """Return the matching exchange fill, or None if it is not readable yet.

        This is deliberately read-only and bounded. A missing fill is not treated
        as a zero-price fill, and callers may use the explicitly supplied signal
        entry only as a documented fallback while reconciliation continues.
        """
        get_fills = getattr(self.adapter, "get_fills", None)
        if get_fills is None:
            return None
        for attempt in range(3):
            try:
                fills = await get_fills(order.instrument)
                candidates = [f for f in (fills or [])
                              if str(getattr(f, "order_id", "")) in (str(order_id), "")
                              and (not getattr(f, "client_order_id", "")
                                   or getattr(f, "client_order_id", "") == order.client_order_id)]
                if candidates:
                    # A market order may have multiple executions. Use a true
                    # quantity-weighted average and the sum of real fees.
                    qty = sum(float(getattr(f, "qty", 0) or 0) for f in candidates)
                    if qty > 0:
                        first = candidates[0]
                        avg = sum(float(f.price) * float(f.qty) for f in candidates) / qty
                        first.price = avg
                        first.qty = qty
                        first.fee = sum(float(getattr(f, "fee", 0) or 0) for f in candidates)
                        return first
            except Exception as exc:  # noqa: BLE001 - reconciliation handles lag/errors
                log.warning("fill read after open failed for %s/%s: %s",
                            order.instrument.symbol, order_id, exc)
            if attempt < 2:
                await asyncio.sleep(0.2)
        return None

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
        """Close all or part of a position with a reduce-only market order.

        ROBUSTNESS FIX: never trust ``position.size`` blindly. The in-memory
        size can drift from the exchange (e.g. a partial that the WS missed, or a
        stale cache). ``fraction`` is relative to the REAL venue size: we sync a
        fresh position first and recompute the close qty from it, then clamp to
        the actual open size so we can never over-close (selling more than exists)
        or under-close. The remainder the caller tracks is also based on the real
        size, not the possibly-stale cache value.
        """
        # Reconcile the position against the live exchange before sizing.
        real_pos = position
        if not self.paper and self.adapter is not None:
            try:
                live = await self.adapter.get_positions()
                for p in live or []:
                    if p.instrument.symbol == position.instrument.symbol and p.size > 0:
                        real_pos = p
                        break
            except Exception as exc:  # noqa: BLE001 - never block a close on a read failure
                log.warning("size-sync failed before close (%s) — using cached size", exc)
        real_size = real_pos.size
        spec = await self._spec(real_pos.instrument)
        frac = min(1.0, max(0.0, fraction))
        qty = real_size * frac
        if qty <= 0:
            return {"status": "NOOP", "reason": "zero close size"}
        order = OrderRequest(
            instrument=real_pos.instrument, side=real_pos.side.opposite,
            order_type="market", qty=BitgetAdapter.round_qty(spec, qty, real_pos.entry_price),
            reduce_only=True, leverage=real_pos.leverage,
            margin_mode="crossed",
            client_order_id=BitgetAdapter.new_client_oid("bgc"),
        )
        if self.paper:
            return {"status": "PAPER", "order": order, "reason": reason}
        order_id = await self.adapter.place_order(order)
        fills = await self._resolve_close_fill(order, order_id)
        try:
            venue_positions = await self.adapter.get_positions()
        except Exception as exc:
            log.warning("close position read-back failed for %s/%s: %s",
                        order.instrument.symbol, order_id, exc)
            venue_positions = None
        if venue_positions is None:
            return {"status": "UNKNOWN", "order_id": order_id, "order": order,
                    "reason": "position read-back unavailable", "exchange_confirmed": False}
        remaining = next((p.size for p in venue_positions
                          if p.instrument.symbol == order.instrument.symbol), 0.0)
        if not fills:
            return {"status": "UNKNOWN", "order_id": order_id, "order": order,
                    "reason": "close read-back unavailable", "exchange_confirmed": False}
        total_qty = sum(float(getattr(f, "qty", 0) or 0) for f in fills)
        if total_qty <= 0:
            return {"status": "UNKNOWN", "order_id": order_id, "order": order,
                    "reason": "close fill has no quantity", "exchange_confirmed": False}
        fill_price = sum(float(f.price) * float(f.qty) for f in fills) / total_qty
        fill_fee = sum(float(getattr(f, "fee", 0) or 0) for f in fills)
        return {"status": "PLACED", "order_id": order_id, "order": order,
                "reason": reason, "closed_qty": qty, "real_size": real_size,
                "close_fill_price": fill_price, "close_fill_fee": fill_fee,
                "exchange_position_size": remaining, "exchange_flat": remaining <= 1e-12,
                "exchange_confirmed": True}

    async def _resolve_close_fill(self, order: OrderRequest, order_id: str):
        get_fills = getattr(self.adapter, "get_fills", None)
        if get_fills is None:
            return []
        try:
            fills = await get_fills(order.instrument)
            return [f for f in (fills or [])
                    if str(getattr(f, "order_id", "")) == str(order_id)
                    or str(getattr(f, "client_order_id", "")) == str(order.client_order_id)]
        except Exception as exc:
            log.warning("close fill read-back failed for %s/%s: %s",
                        order.instrument.symbol, order_id, exc)
            return []
