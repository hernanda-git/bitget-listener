"""Tradability check — decide per signal whether a symbol can actually be traded.

Replaces a hand-picked SYMBOL_WHITELIST, which is what silently dropped the
PUMP signal: PUMPUSDT was tradable at the $5.00 floor but simply was not on a
list chosen from a 10-coin probe.

The real constraint is not a name, it is arithmetic:

    effective min notional = max(minTradeUSDT, minTradeNum x price)

A symbol is tradable when that figure is listed, active, and affordable
against available margin. ETH fails on an $8.88 account (0.01 x $3100 = $31),
PUMP passes (1 x $0.003 = well under the $5 floor, so $5.00 governs). This is
measured per signal from live venue data rather than assumed in advance.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class Tradability:
    ok: bool
    symbol: str
    reason: str = ""
    min_notional: float | None = None
    price: float | None = None
    affordable_notional: float | None = None

    @property
    def detail(self) -> str:
        bits = [self.reason] if self.reason else []
        if self.min_notional is not None:
            bits.append(f"min notional ${self.min_notional:.2f}")
        if self.affordable_notional is not None:
            bits.append(f"affordable ${self.affordable_notional:.2f}")
        return "; ".join(bits)


class TradabilityChecker:
    """Answers 'can we actually place this?' from live spec + balance.

    ``margin_headroom`` is the fraction of available margin a single position's
    required margin may consume. ``max_leverage`` caps the leverage used when
    computing what is affordable.
    """

    def __init__(self, adapter, margin_headroom: float = 0.95,
                 max_leverage: int = 1, blocklist: set[str] | None = None,
                 high_risk: bool = False):
        self.adapter = adapter
        self.margin_headroom = margin_headroom
        self.max_leverage = max(1, int(max_leverage))
        self.high_risk = bool(high_risk)
        self.blocklist = {s.upper() for s in (blocklist or set())}

    async def check(self, symbol: str) -> Tradability:
        sym = symbol.upper()
        if sym in self.blocklist:
            return Tradability(False, sym, "explicitly blocklisted")

        # 1. Is it listed and active on this product type?
        try:
            spec = await self.adapter.get_spec_for_symbol(sym)
        except Exception as exc:
            return Tradability(False, sym,
                               f"no contract spec: {type(exc).__name__}: {exc}")
        if spec is None:
            return Tradability(False, sym, "not listed on this product type")
        if not spec.tradable:
            return Tradability(False, sym, f"symbol not tradable (status={spec.status})")

        min_notional = float(spec.min_notional or 0.0)

        # 2. Can we afford that minimum with the margin we actually have?
        try:
            available = float(await self.adapter.get_balance_usdt())
        except Exception as exc:
            # Never drop a signal because a balance read hiccuped; let the
            # sizer and the venue be the authority instead.
            log.warning("[tradability] balance unavailable for %s: %s", sym, exc)
            return Tradability(True, sym, "balance unavailable — deferring to sizer",
                               min_notional=min_notional)

        lev = int(spec.max_leverage or 1) if self.high_risk else min(
            self.max_leverage, int(spec.max_leverage or 1) or 1)
        # At the configured max leverage, what notional can our available margin
        # actually support? A $5 min-notional position needs $5 notional (or $0.25
        # margin at 20x) — trivially affordable on $8.88. A coin whose min notional
        # exceeds this is the one we must refuse, not a cheap one we wrongly drop.
        affordable = available * self.margin_headroom * lev
        if min_notional > affordable:
            return Tradability(
                False, sym,
                f"min notional ${min_notional:.2f} exceeds affordable "
                f"${affordable:.2f} (available ${available:.2f} x {lev}x)",
                min_notional=min_notional, affordable_notional=affordable)

        return Tradability(True, sym, "", min_notional=min_notional,
                           affordable_notional=affordable)
