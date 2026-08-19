"""P14: no good signal is silently dropped.

Two real failure modes are pinned here:

1. A tradable symbol that is simply absent from a hand-picked whitelist must
   NOT be reported as "not whitelisted" / "not on allow-list". The tradability
   check decides eligibility from live spec + balance, not a static name list.
   (This is exactly the PUMP incident.)

2. The venue-state preflight must catch a misconfigured account (hedge mode ->
   40774, isolated margin with liquidation inside the stop) and either repair it
   or report it loudly.

Read-only fakes; no network.
"""
from __future__ import annotations

import pytest

from src.domain.model import Instrument, InstrumentSpec
from src.services.preflight import PreflightResult, preflight_venue
from src.services.tradability import Tradability, TradabilityChecker


# ----------------------------------------------------------------- spec fakes
def _spec(symbol="PUMPUSDT", min_notional=5.0, price=0.003, status="normal",
          max_lev=75):
    return InstrumentSpec(
        symbol=symbol, price_step=1e-8, qty_step=1.0, min_qty=1.0,
        size_multiplier=1.0, min_notional=min_notional, max_leverage=max_lev,
        status=status, tradable=(status in ("normal", "live", "trading", "")))


class _SpecAdapter:
    """Adapter stub: returns canned specs and a fixed balance."""

    def __init__(self, specs, balance=8.88, margin_mode="crossed", pos_mode="one_way_mode"):
        self._specs = {(s.symbol if isinstance(s, InstrumentSpec) else s): s for s in specs}
        self.balance = balance
        self.margin_mode = margin_mode
        self.pos_mode = pos_mode
        self.calls = []

    product_type = "USDT-FUTURES"
    margin_coin = "USDT"

    def venue_symbol(self, symbol):
        return symbol

    async def get_spec_for_symbol(self, symbol):
        return self._specs[symbol]

    async def get_balance_usdt(self):
        return self.balance

    async def get_positions(self):
        return []

    async def _signed(self, method, path, params=None, body=None):
        self.calls.append((method, path, params, body))
        if "set-position-mode" in path:
            self.pos_mode = "one_way_mode"
            return {"posMode": "one_way_mode"}
        if "set-margin-mode" in path:
            self.margin_mode = body.get("marginMode")
            return {"marginMode": body.get("marginMode")}
        if "account" in path:
            return {"posMode": self.pos_mode, "marginMode": self.margin_mode}
        return {}


async def test_tradable_symbol_is_not_blocked_when_absent_from_whitelist():
    """The PUMP case: tradable, but had no business being on a static list."""
    adapter = _SpecAdapter([_spec("PUMPUSDT", min_notional=5.0, price=0.003)])
    checker = TradabilityChecker(adapter)
    t = await checker.check("PUMPUSDT")
    assert t.ok is True, t.detail
    # and crucially a whitelist-only gate would have dropped it:
    assert "not on allow-list" not in t.detail


async def test_expensive_symbol_rejected_as_unaffordable():
    """ETH: 0.01 x $3100 = $31 min notional, account only has $8.88."""
    spec = _spec("ETHUSDT", min_notional=31.0, price=3100.0)
    adapter = _SpecAdapter([spec], balance=8.88)
    checker = TradabilityChecker(adapter, max_leverage=1)
    t = await checker.check("ETHUSDT")
    assert t.ok is False
    assert "exceeds affordable" in t.detail


async def test_suspended_pair_is_not_tradable():
    adapter = _SpecAdapter([_spec("SCAMUSDT", status="suspend", min_notional=5.0)])
    checker = TradabilityChecker(adapter)
    t = await checker.check("SCAMUSDT")
    assert t.ok is False
    assert "not tradable" in t.detail


async def test_blocklist_overrides_everything():
    adapter = _SpecAdapter([_spec("SHITUSDT")])
    checker = TradabilityChecker(adapter, blocklist={"SHITUSDT"})
    assert (await checker.check("SHITUSDT")).ok is False


async def test_balance_hiccup_is_non_fatal():
    class _BadBal(_SpecAdapter):
        async def get_balance_usdt(self):
            raise RuntimeError("timeout")
    checker = TradabilityChecker(_BadBal([_spec("PUMPUSDT")]))
    # never drops a signal purely because balance read failed
    assert (await checker.check("PUMPUSDT")).ok is True


# --------------------------------------------------------------- preflight
async def test_preflight_repairs_hedge_mode_when_no_positions():
    adapter = _SpecAdapter([_spec()], pos_mode="hedge_mode")
    res = await preflight_venue(adapter, "PUMPUSDT", repair=True, want_margin_mode="crossed")
    assert res.ok
    assert res.pos_mode == "one_way_mode"
    assert any("posMode" in f for f in res.fixed)


async def test_preflight_repairs_isolated_to_crossed():
    adapter = _SpecAdapter([_spec()], margin_mode="isolated")
    res = await preflight_venue(adapter, "PUMPUSDT", repair=True, want_margin_mode="crossed")
    assert res.ok
    assert res.margin_mode == "crossed"
    assert any("marginMode" in f for f in res.fixed)


async def test_preflight_refuses_to_change_mode_with_open_position():
    class _Open(_SpecAdapter):
        async def get_positions(self):
            return ["a position"]
    adapter = _Open([_spec()], pos_mode="hedge_mode")
    res = await preflight_venue(adapter, "PUMPUSDT", repair=True)
    assert res.ok is False
    assert any("positions open" in p for p in res.problems)


async def test_preflight_clean_account_needs_no_repair():
    adapter = _SpecAdapter([_spec()], pos_mode="one_way_mode", margin_mode="crossed")
    res = await preflight_venue(adapter, "PUMPUSDT", repair=True)
    assert res.ok and not res.fixed
