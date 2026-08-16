"""FixedRiskSizer — fee-aware position sizing.

The contract inherited from all four siblings: **net = gross − (open_fee + close_fee)**.
Sizing that ignores fees systematically over-sizes and turns a break-even edge
negative, which is exactly how BUG A hurt the Binance ancestor.

Bitget VIP0 taker is 0.0006 per side (VERIFIED from the live contract response), so a
round trip costs ~0.12% of notional before any price move.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.domain.model import InstrumentSpec
from src.interfaces.ports import RiskPolicy


@dataclass(frozen=True)
class RiskLimits:
    """Safety bounds from analysis/06 §4. ``max_daily_loss_pct`` is an immutable kill switch."""

    risk_per_trade_usd: float = 5.0
    max_position_usd: float = 100.0
    max_concurrent: int = 2
    max_daily_loss_pct: float = 30.0
    max_leverage: int = 20
    min_notional_usd: float = 5.0
    cooldown_s: int = 30
    whitelist_only: bool = True
    equity_usd: float = 100.0   # denominator for the daily-loss kill switch


@dataclass(frozen=True)
class SizeResult:
    qty: float
    leverage: int
    notional_usd: float
    open_fee: float
    close_fee: float
    reason: str = ""

    @property
    def total_fees(self) -> float:
        return self.open_fee + self.close_fee

    @property
    def ok(self) -> bool:
        return self.qty > 0


class FixedRiskSizer(RiskPolicy):
    """Risk a fixed USD amount per trade, sized off the SL distance, net of fees."""

    def __init__(self, limits: RiskLimits | None = None) -> None:
        self.limits = limits or RiskLimits()

    def compute_size(self, notional_usd: float, mark_price: float,
                     max_leverage: int) -> tuple[float, int]:
        """Port method: plain notional -> (qty, leverage)."""
        if mark_price <= 0:
            return 0.0, 1
        notional = min(notional_usd, self.limits.max_position_usd)
        lev = max(1, min(int(max_leverage), self.limits.max_leverage))
        return notional / mark_price, lev

    def estimate_open_fee(self, qty: float, price: float,
                          spec: InstrumentSpec | None = None) -> float:
        """Taker fee for the opening leg. Never returns 0 for a real qty (BUG A)."""
        rate = spec.taker_fee if spec is not None else 0.0006
        return abs(qty) * abs(price) * rate

    def estimate_close_fee(self, qty: float, price: float,
                           spec: InstrumentSpec | None = None) -> float:
        """Taker fee for the closing leg."""
        rate = spec.taker_fee if spec is not None else 0.0006
        return abs(qty) * abs(price) * rate

    def size_for_signal(self, entry: float, sl: float | None, spec: InstrumentSpec,
                        requested_leverage: int = 0,
                        risk_usd: float | None = None) -> SizeResult:
        """Size so that a stop-out loses ~``risk_usd`` INCLUDING both fees.

        With ``R`` the per-unit loss to SL and ``f`` the round-trip fee rate:
            qty * (R + entry * f) = risk_usd
        Ignoring the fee term is what makes a "1R" loss actually cost more than 1R.
        """
        if entry <= 0:
            return SizeResult(0, 1, 0, 0, 0, "invalid entry price")
        risk = risk_usd if risk_usd is not None else self.limits.risk_per_trade_usd
        lev = max(1, min(int(requested_leverage or self.limits.max_leverage),
                         self.limits.max_leverage, spec.max_leverage))
        fee_rate = spec.taker_fee * 2  # assume taker in and taker out (conservative)

        if sl is None or sl <= 0 or abs(entry - sl) <= 0:
            notional = min(self.limits.risk_per_trade_usd * lev,
                           self.limits.max_position_usd)
            qty = notional / entry
            reason = "no SL distance — fell back to notional cap"
        else:
            per_unit_loss = abs(entry - sl) + entry * fee_rate
            qty = risk / per_unit_loss
            notional = qty * entry
            reason = ""
            if notional > self.limits.max_position_usd:
                qty = self.limits.max_position_usd / entry
                notional = qty * entry
                reason = "clamped to max_position_usd"

        notional = qty * entry
        if notional < max(spec.min_notional, self.limits.min_notional_usd):
            need = max(spec.min_notional, self.limits.min_notional_usd)
            if need > self.limits.max_position_usd:
                return SizeResult(0, lev, 0, 0, 0,
                                  f"min notional {need} exceeds max_position_usd")
            qty = need / entry
            notional = qty * entry
            reason = (reason + "; " if reason else "") + "raised to min notional"

        open_fee = notional * spec.taker_fee
        close_fee = notional * spec.taker_fee
        return SizeResult(qty=qty, leverage=lev, notional_usd=notional,
                          open_fee=open_fee, close_fee=close_fee, reason=reason)

    @staticmethod
    def net_pnl(gross_usd: float, open_fee: float, close_fee: float) -> float:
        """The single definition of net PnL used by the ledger and the go-live gate."""
        return gross_usd - (open_fee + close_fee)

    def expectancy_ok(self, net_pnls: list[float]) -> bool:
        """Go-live condition (c): fee-adjusted expectancy must be strictly positive."""
        if not net_pnls:
            return False
        return (sum(net_pnls) / len(net_pnls)) > 0
