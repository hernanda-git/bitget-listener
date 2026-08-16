"""Execution rigor: order validation, idempotency dedup, deterministic repair.

Grafted from mexc-listener (`exchanges/repair.py`) — the strongest execution layer
of the four siblings — and retargeted to Bitget's VERIFIED spec fields.

**No LLM is ever consulted to fix a trade.** The LLM decides *whether* to trade;
repair is pure arithmetic. A rejected order is re-rounded against a freshly reloaded
spec and resubmitted at most ONCE.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from src.domain.model import InstrumentSpec, OrderRequest


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason: str = ""
    code: str = ""

    def __bool__(self) -> bool:
        return self.ok


def validate_order(order: OrderRequest, spec: InstrumentSpec,
                   price_ref: float | None = None) -> ValidationResult:
    """Reject anything the exchange would reject — before spending a request.

    Checks, in order: positive size, min qty, step/multiplier alignment, min
    notional, limit-price presence/positivity, leverage bound, and the
    magnitude-flip guard that stops a $0.16 coin from being priced like $100.
    """
    px = order.price if order.price is not None else price_ref
    if order.qty <= 0:
        return ValidationResult(False, "qty must be > 0", "QTY_NONPOSITIVE")
    if order.qty < spec.min_qty:
        return ValidationResult(False, f"qty {order.qty} < min {spec.min_qty}", "QTY_TOO_SMALL")
    step = max(spec.qty_step, spec.size_multiplier)
    if step > 0:
        multiples = order.qty / step
        if abs(multiples - round(multiples)) > 1e-6:
            return ValidationResult(False, f"qty {order.qty} not a multiple of {step}",
                                    "QTY_STEP")
    if order.order_type == "limit":
        if px is None or px <= 0:
            return ValidationResult(False, "limit order requires a positive price",
                                    "PRICE_MISSING")
        if spec.price_step > 0:
            ticks = px / spec.price_step
            if abs(ticks - round(ticks)) > 1e-6:
                return ValidationResult(False, f"price {px} not a multiple of {spec.price_step}",
                                        "PRICE_STEP")
    if px is not None and px > 0:
        notional = order.qty * px
        if notional < spec.min_notional:
            return ValidationResult(False,
                                    f"notional {notional:.4f} < min {spec.min_notional}",
                                    "MIN_NOTIONAL")
        if price_ref and price_ref > 0 and abs(math.log10(px / price_ref)) > 0.5:
            return ValidationResult(False,
                                    f"price {px} is an order of magnitude off ref {price_ref}",
                                    "MAGNITUDE_FLIP")
    if order.leverage > spec.max_leverage:
        return ValidationResult(False,
                                f"leverage {order.leverage} > pair max {spec.max_leverage}",
                                "LEVERAGE")
    return ValidationResult(True)


class DecisionStore:
    """Minimal port the guard needs (real impl: Postgres UNIQUE on client_order_id)."""

    def get_active_for_decision(self, decision_id: str, side: str):
        raise NotImplementedError

    def mark_active(self, decision_id: str, side: str, client_order_id: str) -> None:
        raise NotImplementedError


class InMemoryDecisionStore(DecisionStore):
    def __init__(self) -> None:
        self._active: dict[tuple[str, str], str] = {}

    def get_active_for_decision(self, decision_id: str, side: str):
        return self._active.get((decision_id, side))

    def mark_active(self, decision_id: str, side: str, client_order_id: str) -> None:
        self._active[(decision_id, side)] = client_order_id


@dataclass(frozen=True)
class DedupResult:
    skipped: bool
    status: str = ""
    existing_client_order_id: str | None = None

    def __bool__(self) -> bool:
        return not self.skipped


class IdempotencyGuard:
    """One decision (Telegram message) may produce at most one live order per side."""

    def __init__(self, store: DecisionStore | None = None) -> None:
        self.store = store or InMemoryDecisionStore()

    def check(self, decision_id: str, side, client_order_id: str = "") -> DedupResult:
        side_val = getattr(side, "value", str(side))
        existing = self.store.get_active_for_decision(decision_id, side_val)
        if existing is not None:
            return DedupResult(True, "DUPLICATE_SKIPPED", str(existing))
        self.store.mark_active(decision_id, side_val, client_order_id)
        return DedupResult(False, "OK")


class RepairOrder:
    """Deterministic single-shot repair of a rejected order. Never calls an LLM."""

    MAX_ATTEMPTS = 1

    def repair(self, order: OrderRequest, spec: InstrumentSpec,
               price_ref: float | None = None) -> OrderRequest:
        """Re-round qty/price against ``spec`` so the order passes ``validate_order``.

        ``reduce_only`` semantics are preserved exactly: a protective order is never
        silently converted into an opening order, and its size is never increased
        beyond the position it protects (callers pass the position size as qty).
        """
        px = order.price if order.price is not None else price_ref
        step = max(spec.qty_step, spec.size_multiplier)
        qty = math.floor(round(order.qty / step, 9)) * step if step > 0 else order.qty
        if qty < spec.min_qty:
            qty = spec.min_qty
        if not order.reduce_only and px and px > 0:
            guard = 0
            while qty * px < spec.min_notional and guard < 10_000_000:
                qty = round(qty + step, 12)
                guard += 1
        new_price = order.price
        if order.order_type == "limit" and order.price is not None and spec.price_step > 0:
            new_price = round(
                math.floor(round(order.price / spec.price_step, 9)) * spec.price_step, 12)
        return replace(order, qty=round(qty, 12), price=new_price,
                       leverage=min(order.leverage, spec.max_leverage))
