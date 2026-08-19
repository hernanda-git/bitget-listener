"""Regression tests for bugs the FAKES-ONLY suite could not catch.

Both were found by running the real entrypoint against the live Bitget demo
product, not by unit tests, so they are pinned here permanently.

1. **Demo symbol translation.** ``productType=SUSDT-FUTURES`` lists only
   ``SBTCSUSDT`` / ``SETHSUSDT`` / ``SXRPSUSDT``. Sending ``BTCUSDT`` there fails
   with NO_SPEC, so every demo order died before this fix.
2. **Paper mode called a signed endpoint.** ``_after_open`` ran
   ``positions.sync()`` unconditionally, which hits ``/mix/position/all-position``
   and raises NO_CREDS — meaning a paper run was impossible without real API keys,
   defeating the entire point of paper mode.
"""

from __future__ import annotations

import pytest

from src.config.db import InMemoryConfigStore
from src.domain.model import Instrument, Side
from src.exchanges.bitget import BitgetAdapter, BitgetError
from src.notifier.golive import GateConditions, GoLiveGate
from src.services.orchestrator import Orchestrator
from src.sources.parser import SymbolMap, parse_message
from src.sources.telegram import FakeSignalSource
from src.store.ledger import TradeLedger
from src.streaming.bitget_ws import BitgetWS

LIVE = "USDT-FUTURES"
DEMO = "SUSDT-FUTURES"


def adapter(product_type=DEMO, **kw):
    return BitgetAdapter(product_type=product_type, **kw)


# ------------------------------------------------------- demo symbol translation
@pytest.mark.parametrize("canonical,venue", [
    ("BTCUSDT", "SBTCSUSDT"),
    ("ETHUSDT", "SETHSUSDT"),
    ("XRPUSDT", "SXRPSUSDT"),
])
def test_demo_symbols_get_the_s_prefix_and_susdt_quote(canonical, venue):
    assert adapter().venue_symbol(canonical) == venue


def test_live_product_leaves_symbols_untouched():
    ad = adapter(LIVE)
    assert ad.venue_symbol("BTCUSDT") == "BTCUSDT"
    assert ad.canonical_symbol("BTCUSDT") == "BTCUSDT"
    assert ad.is_demo is False


def test_demo_translation_round_trips():
    ad = adapter()
    for sym in ("BTCUSDT", "ETHUSDT", "XRPUSDT"):
        assert ad.canonical_symbol(ad.venue_symbol(sym)) == sym


def test_demo_translation_is_idempotent():
    ad = adapter()
    assert ad.venue_symbol("SBTCSUSDT") == "SBTCSUSDT"


def test_demo_margin_coin_is_susdt():
    assert adapter().margin_coin == "SUSDT"
    assert adapter(LIVE).margin_coin == "USDT"


@pytest.mark.asyncio
async def test_get_spec_requests_the_demo_symbol(monkeypatch):
    ad = adapter()
    seen = {}

    async def fake_public(path, params=None):
        seen.update(params or {})
        return [{"symbol": "SBTCSUSDT", "makerFeeRate": "0.0002", "takerFeeRate": "0.0006",
                 "minTradeNum": "0.001", "volumePlace": "3", "pricePlace": "1",
                 "sizeMultiplier": "0.001", "minTradeUSDT": "5", "maxLever": "125"}]

    monkeypatch.setattr(ad, "_public", fake_public)
    spec = await ad.get_spec(Instrument.from_token("BTC"))
    assert seen["symbol"] == "SBTCSUSDT"        # the request used the demo symbol
    assert seen["productType"] == DEMO
    assert spec.symbol == "BTCUSDT"             # the domain still sees canonical


@pytest.mark.asyncio
async def test_order_body_carries_the_demo_symbol(monkeypatch):
    ad = adapter(paper=False)
    ad._spec_cache["BTCUSDT"] = BitgetAdapter._parse_spec(
        {"symbol": "BTCUSDT", "makerFeeRate": "0.0002", "takerFeeRate": "0.0006",
         "minTradeNum": "0.001", "volumePlace": "3", "pricePlace": "1",
         "sizeMultiplier": "0.001", "minTradeUSDT": "5", "maxLever": "125"})
    sent = {}

    async def fake_signed(method, path, params=None, body=None):
        sent.update(body or {})
        return {"orderId": "1"}

    monkeypatch.setattr(ad, "_signed", fake_signed)
    from src.domain.model import OrderRequest
    await ad.place_order(OrderRequest(instrument=Instrument.from_token("BTC"),
                                      side=Side.BUY, qty=0.01, price=60_000.0))
    assert sent["symbol"] == "SBTCSUSDT"


@pytest.mark.asyncio
async def test_positions_are_mapped_back_to_canonical(monkeypatch):
    ad = adapter(paper=False)

    async def fake_signed(method, path, params=None, body=None):
        return [{"symbol": "SBTCSUSDT", "holdSide": "long", "total": "0.01",
                 "openPriceAvg": "60000", "liquidationPrice": "54000"}]

    monkeypatch.setattr(ad, "_signed", fake_signed)
    positions = await ad.get_positions()
    assert positions[0].instrument.symbol == "BTCUSDT"     # never SBTCSUSDT
    assert positions[0].liq_price == 54_000.0


@pytest.mark.asyncio
async def test_fills_are_mapped_back_to_canonical(monkeypatch):
    ad = adapter(paper=False)

    async def fake_signed(method, path, params=None, body=None):
        return {"fillList": [{"symbol": "SBTCSUSDT", "price": "60000",
                              "baseVolume": "0.01", "fee": "-0.36"}]}

    monkeypatch.setattr(ad, "_signed", fake_signed)
    fills = await ad.get_fills()
    assert fills[0].symbol == "BTCUSDT"
    assert fills[0].fee == pytest.approx(0.36)


def test_ws_subscribes_with_demo_symbols_but_reports_canonical():
    ws = BitgetWS(symbols=["BTCUSDT"], product_type=DEMO)
    assert ws.subscribe_frame()["args"][0]["instId"] == "SBTCSUSDT"
    assert ws.canonical_symbol("SBTCSUSDT") == "BTCUSDT"


@pytest.mark.asyncio
async def test_ws_tick_is_cached_under_the_canonical_symbol():
    import json
    ws = BitgetWS(symbols=["BTCUSDT"], product_type=DEMO)
    await ws._dispatch(json.dumps({"arg": {"channel": "ticker"},
                                   "data": [{"instId": "SBTCSUSDT",
                                             "markPrice": "60500"}]}))
    assert ws.mark_price("BTCUSDT") == 60_500.0


def test_ws_live_product_does_not_translate():
    ws = BitgetWS(symbols=["BTCUSDT"], product_type=LIVE)
    assert ws.subscribe_frame()["args"][0]["instId"] == "BTCUSDT"


# ------------------------------------------- paper mode needs zero credentials
@pytest.mark.asyncio
async def test_paper_run_never_calls_a_signed_endpoint():
    """The bug: _after_open called positions.sync() -> NO_CREDS in paper mode."""
    calls = []

    class NoCredsAdapter(BitgetAdapter):
        async def _signed(self, method, path, params=None, body=None):
            calls.append(path)
            raise BitgetError("NO_CREDS", "no credentials", path)

        async def get_spec(self, instrument):
            from src.exchanges.fake import DEFAULT_SPEC
            return DEFAULT_SPEC

        async def get_mark_price(self, instrument):
            return 60_000.0

    ad = NoCredsAdapter(product_type=DEMO, paper=True)
    gate = GoLiveGate(conditions=GateConditions(), config_store=InMemoryConfigStore())
    gate.paper = True
    ledger = TradeLedger(":memory:")
    whitelist = {"BTCUSDT"}
    orch = Orchestrator(FakeSignalSource([], symbol_map=SymbolMap(whitelist)), ad, gate,
                        ledger, symbol_map=SymbolMap(whitelist), whitelist=whitelist)

    res = await orch.handle(parse_message(
        "BTCUSDT LONG entry 60000 sl 57000 tp 66000 lev 10", message_id=1))
    assert res["status"] == "PAPER"
    assert calls == [], f"paper mode hit signed endpoints: {calls}"
    # the position must still be tracked locally
    assert orch.positions.get("BTCUSDT") is not None
    assert len(ledger.open_trades()) == 1

    # and the paper position can be closed without credentials either
    close = await orch.handle(parse_message("close full"))
    assert close["status"] != "NO_POSITION"
    assert calls == []
    assert len(ledger.closed_trades()) == 1


@pytest.mark.asyncio
async def test_failed_live_sync_does_not_lose_the_open_position():
    """If sync() throws in LIVE mode we must keep our local view, not forget it."""
    class FlakyAdapter(BitgetAdapter):
        async def get_spec(self, instrument):
            from src.exchanges.fake import DEFAULT_SPEC
            return DEFAULT_SPEC

        async def get_mark_price(self, instrument):
            return 60_000.0

        async def set_leverage(self, instrument, leverage, hold_side=None, cap=None):
            return None

        async def place_order(self, order):
            return "oid-1"

        async def set_trading_stop(self, instrument, sl, tp, size=None):
            return None

        async def get_positions(self):
            raise BitgetError("50001", "transient venue error", "/positions")

    ad = FlakyAdapter(product_type=DEMO, paper=False)
    gate = GoLiveGate(conditions=GateConditions(), config_store=InMemoryConfigStore())
    gate.paper = False
    ledger = TradeLedger(":memory:")
    whitelist = {"BTCUSDT"}
    orch = Orchestrator(FakeSignalSource([], symbol_map=SymbolMap(whitelist)), ad, gate,
                        ledger, symbol_map=SymbolMap(whitelist), whitelist=whitelist)
    res = await orch.handle(parse_message(
        "BTCUSDT LONG entry 60000 sl 57000 tp 66000 lev 10", message_id=2))
    assert res["status"] == "PLACED"
    assert orch.positions.get("BTCUSDT") is not None
