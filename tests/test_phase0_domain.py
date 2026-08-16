"""P0/P1: scaffold, domain types, ports, fakes. Zero network."""

from __future__ import annotations

import inspect

import pytest

from src.domain.model import (
    Fill,
    Instrument,
    InstrumentSpec,
    OrderRequest,
    Position,
    RawSignal,
    Side,
    Signal,
    TradeDecision,
)
from src.exchanges.fake import DEFAULT_SPEC, FakeExchangeAdapter
from src.interfaces.ports import ExchangeAdapter, MarketStream, Notifier, SignalSource


def test_instrument_from_bare_token():
    inst = Instrument.from_token("btc")
    assert inst.symbol == "BTCUSDT"
    assert inst.base == "BTC"
    assert inst.quote == "USDT"
    assert inst.venue == "bitget"


def test_instrument_from_full_symbol_is_idempotent():
    assert Instrument.from_token("BTCUSDT").symbol == "BTCUSDT"
    assert Instrument.from_token("BTCUSDT").base == "BTC"


def test_instrument_default_product_type_is_verified_v2_value():
    # 'umcbl' is DEAD per the probe; the default must be the V2 value.
    assert Instrument.from_token("BTC").product_type == "USDT-FUTURES"
    assert "umcbl" not in Instrument.from_token("BTC").product_type.lower()


def test_instrument_is_frozen():
    inst = Instrument.from_token("BTC")
    with pytest.raises(Exception):
        inst.symbol = "ETHUSDT"  # type: ignore[misc]


def test_instrument_multiplier_contract_base():
    inst = Instrument.from_token("1000BONKUSDT")
    assert inst.symbol == "1000BONKUSDT"
    assert inst.base == "1000BONK"


def test_side_opposite():
    assert Side.BUY.opposite is Side.SELL
    assert Side.SELL.opposite is Side.BUY


def test_fill_requires_real_fee_field():
    # BUG A: the domain type must carry a fee; it is not optional.
    assert "fee" in inspect.signature(Fill).parameters
    f = Fill(client_order_id="x", price=100.0, qty=1.0, side="BUY", fee=0.06)
    assert f.fee == 0.06


def test_position_liq_price_defaults_to_none_not_zero():
    # BUG B: absent liquidation price must be None, never a falsy 0.0 masquerading as real.
    pos = Position(instrument=Instrument.from_token("BTC"), side=Side.BUY,
                   size=1.0, entry_price=100.0)
    assert pos.liq_price is None


def test_order_request_defaults_are_safe():
    o = OrderRequest(instrument=Instrument.from_token("BTC"), side=Side.BUY)
    assert o.reduce_only is False
    assert o.margin_mode == "isolated"
    assert o.order_type == "market"
    assert o.leverage == 1


def test_signal_and_rawsignal_construct():
    rs = RawSignal(symbol_usdt="BTCUSDT", base="BTC", side="BUY", entry=100.0)
    assert rs.source == "fattyfatclub"
    sig = Signal(instrument=Instrument.from_token("BTC"), side=Side.BUY, entry=100.0,
                 sl=95.0, tp=110.0, notional_usd=50.0, leverage=5, confidence=0.8)
    assert sig.peg_rate == 1.0


def test_trade_decision_defaults():
    d = TradeDecision(action="IGNORE", signal=None)
    assert d.size_coin == 0.0
    assert d.reflexivity_tilt == 0.0


def test_instrument_spec_fields_match_verified_bitget_shape():
    spec = InstrumentSpec(symbol="BTCUSDT", price_step=0.1, qty_step=0.0001,
                          min_qty=0.0001, size_multiplier=0.0001, min_notional=5.0)
    assert spec.maker_fee == 0.0002    # VERIFIED VIP0
    assert spec.taker_fee == 0.0006


def test_ports_are_abstract():
    for port in (ExchangeAdapter, MarketStream, Notifier, SignalSource):
        with pytest.raises(TypeError):
            port()  # type: ignore[abstract]


def test_fake_adapter_satisfies_the_port():
    fake = FakeExchangeAdapter()
    assert isinstance(fake, ExchangeAdapter)


@pytest.mark.asyncio
async def test_fake_adapter_records_nonzero_fee_on_fill():
    """A fake that reports fee=0 would let a BUG A regression pass silently."""
    fake = FakeExchangeAdapter()
    await fake.place_order(OrderRequest(instrument=Instrument.from_token("BTC"),
                                        side=Side.BUY, qty=0.01, price=60_000.0))
    fills = await fake.get_fills()
    assert len(fills) == 1
    assert fills[0].fee > 0


@pytest.mark.asyncio
async def test_fake_adapter_seeds_real_liq_price():
    fake = FakeExchangeAdapter()
    fake.seed_position(liq=54_000.0)
    positions = await fake.get_positions()
    assert positions[0].liq_price == 54_000.0


@pytest.mark.asyncio
async def test_fake_adapter_spec_and_balance():
    fake = FakeExchangeAdapter()
    spec = await fake.get_spec(Instrument.from_token("BTC"))
    assert spec is DEFAULT_SPEC
    assert await fake.get_balance_usdt() == 1_000.0
    assert await fake.get_mark_price(Instrument.from_token("BTC")) == 60_000.0
