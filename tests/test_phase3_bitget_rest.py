"""P3: BitgetAuth signing + BitgetAdapter parsing/rounding. Zero network.

The adapter is exercised against the REAL response shapes captured by
``scripts/bitget_api_probe.py`` (recorded as fixtures below), so a doc-vs-reality
drift shows up as a test failure rather than a live rejection.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from src.domain.model import Instrument, OrderRequest, Side
from src.exchanges.bitget import BitgetAdapter, BitgetError
from src.exchanges.bitget_auth import BitgetAuth

KEY, SECRET, PW = "bg_key", "bg_secret", "bg_pass"

# VERIFIED live shape from /api/v2/mix/market/contracts?productType=USDT-FUTURES&symbol=BTCUSDT
CONTRACT_BTC = {
    "symbol": "BTCUSDT", "baseCoin": "BTC", "quoteCoin": "USDT",
    "makerFeeRate": "0.0002", "takerFeeRate": "0.0006",
    "minTradeNum": "0.0001", "priceEndStep": "1", "volumePlace": "4", "pricePlace": "1",
    "sizeMultiplier": "0.0001", "symbolType": "perpetual", "minTradeUSDT": "5",
    "maxLever": "125", "symbolStatus": "normal",
}
# VERIFIED shape for a 1000x/high-multiplier contract
CONTRACT_PEPE = {
    "symbol": "PEPEUSDT", "makerFeeRate": "0.0002", "takerFeeRate": "0.0006",
    "minTradeNum": "1000", "volumePlace": "0", "pricePlace": "7",
    "sizeMultiplier": "1000", "minTradeUSDT": "5", "maxLever": "50",
}


# ----------------------------------------------------------------------- signing
def test_auth_rejects_missing_credentials():
    with pytest.raises(ValueError):
        BitgetAuth("", SECRET, PW)
    with pytest.raises(ValueError):
        BitgetAuth(KEY, SECRET, "")


def test_prehash_is_timestamp_method_path_body():
    auth = BitgetAuth(KEY, SECRET, PW)
    got = auth.prehash("1700000000000", "get", "/api/v2/mix/account/accounts", "")
    assert got == "1700000000000GET/api/v2/mix/account/accounts"


def test_signature_matches_the_algorithm_independently():
    """Recompute the HMAC by hand — the signature must be BASE64, not hex."""
    auth = BitgetAuth(KEY, SECRET, PW)
    ts = "1700000000000"
    path = "/api/v2/mix/order/place-order"
    body = json.dumps({"symbol": "BTCUSDT"}, separators=(",", ":"))
    sign, got_ts = auth.sign_request("POST", path, body, ts)
    expected = base64.b64encode(
        hmac.new(SECRET.encode(), f"{ts}POST{path}{body}".encode(), hashlib.sha256).digest()
    ).decode()
    assert sign == expected
    assert got_ts == ts
    assert sign != hmac.new(SECRET.encode(), f"{ts}POST{path}{body}".encode(),
                            hashlib.sha256).hexdigest()


def test_headers_use_verified_uppercase_names():
    """The analysis doc wrote 'ACcess-Key'; reality is ACCESS-*."""
    h = BitgetAuth(KEY, SECRET, PW).headers("GET", "/api/v2/mix/account/accounts")
    assert set(h) >= {"ACCESS-KEY", "ACCESS-SIGN", "ACCESS-TIMESTAMP", "ACCESS-PASSPHRASE"}
    assert "ACcess-Key" not in h
    assert h["Content-Type"] == "application/json"


def test_passphrase_is_sent_raw_not_prehashed():
    """VERIFIED: Bitget wants the RAW passphrase (pre-hashing is the OKX variant)."""
    auth = BitgetAuth(KEY, SECRET, PW)
    assert auth.passphrase == PW


def test_passphrase_prehash_escape_hatch_differs():
    auth = BitgetAuth(KEY, SECRET, PW, passphrase_prehashed=True)
    assert auth.passphrase != PW
    assert base64.b64decode(auth.passphrase)  # is valid base64


def test_timestamp_is_millisecond_string():
    ts = BitgetAuth(KEY, SECRET, PW).timestamp_ms()
    assert ts.isdigit()
    assert len(ts) == 13


def test_ws_login_arg_shape_uses_seconds_and_user_verify():
    arg = BitgetAuth(KEY, SECRET, PW).ws_login_arg("1700000000")
    assert set(arg) == {"apiKey", "passphrase", "timestamp", "sign"}
    assert len(arg["timestamp"]) == 10  # seconds, not ms
    expected = base64.b64encode(hmac.new(
        SECRET.encode(), b"1700000000GET/user/verify", hashlib.sha256).digest()).decode()
    assert arg["sign"] == expected


def test_repr_never_leaks_the_secret():
    auth = BitgetAuth(KEY, SECRET, PW)
    assert SECRET not in repr(auth)
    assert PW not in repr(auth)


# ------------------------------------------------------------------ spec parsing
def test_parse_spec_decodes_places_into_steps():
    """Bitget publishes DECIMAL PLACES; the adapter must convert to a step."""
    spec = BitgetAdapter._parse_spec(CONTRACT_BTC)
    assert spec.price_step == pytest.approx(0.1)     # pricePlace=1
    assert spec.qty_step == pytest.approx(0.0001)    # volumePlace=4
    assert spec.min_qty == 0.0001
    assert spec.min_notional == 5.0
    assert spec.max_leverage == 125
    assert spec.taker_fee == 0.0006


def test_parse_spec_handles_high_multiplier_contract():
    spec = BitgetAdapter._parse_spec(CONTRACT_PEPE)
    assert spec.qty_step == pytest.approx(1.0)       # volumePlace=0
    assert spec.size_multiplier == 1000.0
    assert spec.min_qty == 1000.0


def test_parse_spec_honours_effective_min_notional_override():
    """The caller may pass the computed effective floor; it must win over minTradeUSDT."""
    spec = BitgetAdapter._parse_spec(CONTRACT_BTC, min_notional=31.0)
    assert spec.min_notional == pytest.approx(31.0)
    # and without the override the published USDT floor is still used
    assert BitgetAdapter._parse_spec(CONTRACT_BTC).min_notional == pytest.approx(5.0)


CONTRACT_ETH = {
    "symbol": "ETHUSDT", "makerFeeRate": "0.0002", "takerFeeRate": "0.0006",
    "minTradeNum": "0.01", "volumePlace": "2", "pricePlace": "2",
    "sizeMultiplier": "0.01", "minTradeUSDT": "5", "maxLever": "100",
}


async def test_get_spec_computes_effective_min_notional_from_ticker(monkeypatch):
    """ETH: minTradeNum 0.01 * $3100 = $31 is the BINDING floor, not the $5 minTradeUSDT."""
    ad = BitgetAdapter(product_type="USDT-FUTURES")
    calls = []

    async def fake_public(path, params=None):
        calls.append(path)
        if "contracts" in path:
            return [dict(CONTRACT_ETH)]
        return [{"symbol": "ETHUSDT", "lastPr": "3100.0"}]

    monkeypatch.setattr(ad, "_public", fake_public)
    spec = await ad.get_spec(Instrument.from_token("ETH"))
    assert spec.min_notional == pytest.approx(31.0)
    assert any("ticker" in p for p in calls)


async def test_get_spec_falls_back_to_five_when_ticker_fails(monkeypatch):
    """A price hiccup must never block spec loading."""
    ad = BitgetAdapter(product_type="USDT-FUTURES")

    async def fake_public(path, params=None):
        if "contracts" in path:
            return [dict(CONTRACT_ETH)]
        raise RuntimeError("ticker down")

    monkeypatch.setattr(ad, "_public", fake_public)
    spec = await ad.get_spec(Instrument.from_token("ETH"))
    assert spec.min_notional == pytest.approx(5.0)


def test_step_from_places():
    assert BitgetAdapter._step_from_places(0) == 1
    assert BitgetAdapter._step_from_places(4) == pytest.approx(0.0001)


# ---------------------------------------------------------------------- rounding
def test_round_price_steps_down_to_tick():
    spec = BitgetAdapter._parse_spec(CONTRACT_BTC)
    assert BitgetAdapter.round_price(spec, 63_123.47) == pytest.approx(63_123.4)


def test_round_price_refuses_magnitude_flip():
    """A $0.16 coin must never be re-priced like $100 (MEXC Edge C guard)."""
    spec = BitgetAdapter._parse_spec(CONTRACT_PEPE)
    tiny = 0.0000016
    out = BitgetAdapter.round_price(spec, tiny)
    assert out == tiny  # refused, returned untouched for the validator to reject


def test_round_qty_respects_multiplier_and_min_notional():
    spec = BitgetAdapter._parse_spec(CONTRACT_BTC)
    qty = BitgetAdapter.round_qty(spec, 0.000_015, 60_000.0)
    assert qty >= spec.min_qty
    assert qty * 60_000.0 >= spec.min_notional


def test_round_qty_steps_to_multiplier_for_pepe():
    spec = BitgetAdapter._parse_spec(CONTRACT_PEPE)
    qty = BitgetAdapter.round_qty(spec, 4_321.0, 0.000_01)
    assert qty % 1000 == 0


def test_round_qty_never_returns_below_min_qty():
    spec = BitgetAdapter._parse_spec(CONTRACT_BTC)
    assert BitgetAdapter.round_qty(spec, 0.0, 60_000.0) >= spec.min_qty


# VERIFIED live shape for a sub-$1 coin (WLDUSDT): min_notional 5 USDT, qty step 1,
# min qty 1. A 2.0-contract position close at ~0.365 price is worth ~0.73 USDT — far
# below the 5 USDT min_notional. The min-notional inflation loop must be SKIPPED for
# reduce-only (close) orders, or the close qty balloons to 14 and Bitget rejects it
# with 40804 "number of closed positions cannot exceed the number held".
CONTRACT_WLD = {
    "symbol": "WLDUSDT", "baseCoin": "WLD", "quoteCoin": "USDT",
    "makerFeeRate": "0.0002", "takerFeeRate": "0.0006",
    "minTradeNum": "1", "priceEndStep": "1", "volumePlace": "0", "pricePlace": "4",
    "sizeMultiplier": "1", "symbolType": "perpetual", "minTradeUSDT": "5",
    "maxLever": "20", "symbolStatus": "normal",
}


def test_round_qty_reduce_only_never_inflates_past_requested_qty():
    """Regression for 40804: a close (reduce-only) must keep the exact qty."""
    spec = BitgetAdapter._parse_spec(CONTRACT_WLD)
    # 2.0 contracts is a valid close qty for a 2.0 position; must NOT balloon to 14.
    close_qty = BitgetAdapter.round_qty(spec, 2.0, 0.365, reduce_only=True)
    assert close_qty == 2.0
    # Even a tiny close must only ever floor to min_qty, never inflate for notional.
    assert BitgetAdapter.round_qty(spec, 0.5, 0.365, reduce_only=True) == spec.min_qty


def test_round_qty_open_still_inflates_to_min_notional():
    """Entry sizing is unchanged: opening still satisfies the 5 USDT min_notional."""
    spec = BitgetAdapter._parse_spec(CONTRACT_WLD)
    open_qty = BitgetAdapter.round_qty(spec, 2.0, 0.365, reduce_only=False)
    assert open_qty * 0.365 >= spec.min_notional
    assert open_qty > 2.0  # proves inflation still happens for opens


# ------------------------------------------------------------------- order body
def _adapter(paper=True):
    return BitgetAdapter(KEY, SECRET, PW, product_type="SUSDT-FUTURES", paper=paper)


def test_order_body_uses_verified_v2_side_not_v1_compound():
    ad = _adapter()
    spec = BitgetAdapter._parse_spec(CONTRACT_BTC)
    order = OrderRequest(instrument=Instrument.from_token("BTC"), side=Side.BUY,
                         qty=0.01, price=60_000.0, order_type="limit", sl=57_000.0)
    body = ad._order_body(order, spec)
    assert body["side"] == "buy"
    assert body["side"] not in ("open_long", "close_short")   # V1 legacy, rejected
    assert body["productType"] == "SUSDT-FUTURES"
    assert body["presetStopLossPrice"] == "57000.0"


def test_order_body_marks_reduce_only_and_skips_presets():
    ad = _adapter()
    spec = BitgetAdapter._parse_spec(CONTRACT_BTC)
    order = OrderRequest(instrument=Instrument.from_token("BTC"), side=Side.SELL,
                         qty=0.01, reduce_only=True, sl=1.0, tp=2.0)
    body = ad._order_body(order, spec)
    assert body["reduceOnly"] == "YES"
    assert "presetStopLossPrice" not in body
    assert "presetStopSurplusPrice" not in body


def test_order_body_limit_requires_price():
    ad = _adapter()
    spec = BitgetAdapter._parse_spec(CONTRACT_BTC)
    order = OrderRequest(instrument=Instrument.from_token("BTC"), side=Side.BUY,
                         qty=0.01, order_type="limit", price=None)
    with pytest.raises(ValueError):
        ad._order_body(order, spec)


def test_order_body_market_uses_ioc():
    ad = _adapter()
    spec = BitgetAdapter._parse_spec(CONTRACT_BTC)
    order = OrderRequest(instrument=Instrument.from_token("BTC"), side=Side.BUY, qty=0.01)
    assert ad._order_body(order, spec)["force"] == "ioc"


def test_build_tpsl_body_uses_verified_v2_holdSide_not_long_short():
    """BUG #1: Bitget V2 rejects holdSide 'long'/'short' with 43011.

    The accepted value is the POSITION side: 'buy' for a long (close via SELL
    order), 'sell' for a short (close via BUY order). Assert the REAL body the
    adapter posts (no network).
    """
    ad = _adapter()
    spec = BitgetAdapter._parse_spec(CONTRACT_BTC)
    # Long position: protective closes via SELL order -> holdSide must be 'buy'.
    long_body = ad._build_tpsl_body(spec, "BTCUSDT", "loss_plan", 54_000.0,
                                    "buy", "isolated", "0.01", None)
    assert long_body["holdSide"] == "buy"
    assert long_body["side"] == "buy"
    assert long_body["holdSide"] not in ("long", "short")
    # Short position: holdSide must be 'sell'.
    short_body = ad._build_tpsl_body(spec, "BTCUSDT", "loss_plan", 70_000.0,
                                     "sell", "isolated", "0.01", None)
    assert short_body["holdSide"] == "sell"
    assert short_body["side"] == "sell"


def test_build_tpsl_body_carries_required_persist_fields():
    """BUG #3: omitting these makes Bitget accept-but-silently-drop the order."""
    ad = _adapter()
    spec = BitgetAdapter._parse_spec(CONTRACT_BTC)
    body = ad._build_tpsl_body(spec, "BTCUSDT", "profit_plan", 70_000.0,
                               "buy", "isolated", "0.01", None)
    for required in ("side", "tradeSide", "orderType", "timeInForce", "reduceOnly",
                     "planType", "triggerPrice", "triggerType", "holdSide", "size"):
        assert required in body, f"missing required field {required}"
    assert body["tradeSide"] == "close"
    assert body["orderType"] == "market"
    assert body["timeInForce"] == "normal"
    assert body["reduceOnly"] == "true"
    assert body["planType"] == "profit_plan"
    assert body["clientOid"].startswith("bgp-")


def test_client_oid_is_unique_and_prefixed():
    a, b = BitgetAdapter.new_client_oid(), BitgetAdapter.new_client_oid()
    assert a != b
    assert a.startswith("bg-")


def test_margin_coin_switches_on_demo_product():
    assert _adapter().margin_coin == "SUSDT"          # SUSDT-FUTURES demo
    live = BitgetAdapter(KEY, SECRET, PW, product_type="USDT-FUTURES")
    assert live.margin_coin == "USDT"
    assert _adapter().is_demo is True
    assert live.is_demo is False


def test_query_string_builder_skips_none():
    assert BitgetAdapter._qs({"a": 1, "b": None}) == "?a=1"
    assert BitgetAdapter._qs({}) == ""


# ------------------------------------------------------------------ fill parsing
def test_parse_fill_normalizes_negative_fee_to_positive_cost():
    """BUG A: Bitget reports fees NEGATIVE; the ledger needs a positive cost."""
    fill = BitgetAdapter._parse_fill({
        "symbol": "BTCUSDT", "orderId": "1", "clientOid": "bg-1", "price": "60000",
        "baseVolume": "0.01", "side": "buy", "tradeSide": "open", "cTime": "1700",
        "feeDetail": [{"feeCoin": "USDT", "totalFee": "-0.36"}],
    })
    assert fill.fee == pytest.approx(0.36)
    assert fill.fee_asset == "USDT"
    assert fill.is_reduce is False
    assert fill.qty == 0.01


def test_parse_fill_handles_json_string_fee_detail():
    fill = BitgetAdapter._parse_fill({
        "symbol": "BTCUSDT", "price": "60000", "baseVolume": "0.01",
        "feeDetail": json.dumps([{"feeCoin": "USDT", "totalFee": "-0.12"}]),
    })
    assert fill.fee == pytest.approx(0.12)


def test_parse_fill_falls_back_to_flat_fee_field():
    fill = BitgetAdapter._parse_fill({"symbol": "BTCUSDT", "price": "1", "baseVolume": "1",
                                      "fee": "-0.05"})
    assert fill.fee == pytest.approx(0.05)


def test_parse_fill_marks_close_as_reduce():
    fill = BitgetAdapter._parse_fill({"symbol": "BTCUSDT", "price": "1", "baseVolume": "1",
                                      "tradeSide": "close", "fee": "-0.01"})
    assert fill.is_reduce is True


# -------------------------------------------------------------- position parsing
def test_parse_position_keeps_real_liq_price():
    pos = BitgetAdapter._parse_position({
        "symbol": "BTCUSDT", "holdSide": "long", "total": "0.01",
        "openPriceAvg": "60000", "leverage": "10", "liquidationPrice": "54321.5",
        "unrealizedPL": "1.5", "marginMode": "isolated", "uTime": "1700",
    })
    assert pos.liq_price == 54_321.5
    assert pos.side is Side.BUY
    assert pos.entry_price == 60_000.0


def test_parse_position_zero_liq_becomes_none_not_zero():
    """BUG B: '0' must NOT be recorded as a real liquidation price."""
    for zero in ("0", "0.0", "", None):
        pos = BitgetAdapter._parse_position({
            "symbol": "BTCUSDT", "holdSide": "short", "total": "1",
            "openPriceAvg": "1", "liquidationPrice": zero})
        assert pos.liq_price is None
        assert pos.side is Side.SELL


# ----------------------------------------------------------- BUG #3 protection read
def test_parse_position_reads_position_level_sl_tp():
    """BUG #3: position-level stopLoss/takeProfit must be captured (preset path)."""
    pos = BitgetAdapter._parse_position({
        "symbol": "PUMPUSDT", "holdSide": "long", "total": "2016",
        "openPriceAvg": "0.002976", "stopLoss": "0.002882", "takeProfit": "0.00337",
        "liquidationPrice": "-0.0014"})
    assert pos.sl_price == 0.002882
    assert pos.tp_price == 0.00337


def test_parse_position_empty_sl_tp_becomes_none():
    """Empty protective strings must not become a phantom zero stop."""
    pos = BitgetAdapter._parse_position({
        "symbol": "PUMPUSDT", "holdSide": "long", "total": "2016",
        "openPriceAvg": "0.002976", "stopLoss": "", "takeProfit": ""})
    assert pos.sl_price is None
    assert pos.tp_price is None


@pytest.mark.asyncio
async def test_get_protection_merges_plan_orders_over_position(monkeypatch):
    """BUG #3: live loss_plan/profit_plan pending orders are the authoritative SL/TP."""
    ad = _adapter(paper=False)

    class _Pos:
        instrument = Instrument.from_token("PUMPUSDT")
        sl_price = 0.0029    # stale position-level value
        tp_price = 0.0033

    async def fake_positions():
        return [_Pos()]

    async def fake_orders(instrument=None):
        return [
            {"symbol": "PUMPUSDT", "planType": "loss_plan", "triggerPrice": "0.002882"},
            {"symbol": "PUMPUSDT", "planType": "profit_plan", "triggerPrice": "0.00337"},
            # a non-protective entry order must be ignored
            {"symbol": "PUMPUSDT", "orderType": "limit", "triggerPrice": "0.001"},
        ]

    monkeypatch.setattr(ad, "get_positions", fake_positions)
    monkeypatch.setattr(ad, "get_open_orders", fake_orders)
    sl, tp = await ad.get_protection(Instrument.from_token("PUMPUSDT"))
    assert sl == 0.002882
    assert tp == 0.00337


@pytest.mark.asyncio
async def test_get_protection_returns_none_when_no_protection(monkeypatch):
    """BUG #3: when the exchange reports nothing, surface None (never a fake)."""
    ad = _adapter(paper=False)

    class _Pos:
        instrument = Instrument.from_token("PUMPUSDT")
        sl_price = None
        tp_price = None

    async def fake_positions():
        return [_Pos()]

    async def fake_orders(instrument=None):
        return []

    monkeypatch.setattr(ad, "get_positions", fake_positions)
    monkeypatch.setattr(ad, "get_open_orders", fake_orders)
    sl, tp = await ad.get_protection(Instrument.from_token("PUMPUSDT"))
    assert sl is None and tp is None


# ------------------------------------------------------- protective-order filter
@pytest.mark.parametrize("row", [
    {"reduceOnly": "YES"},
    {"reduceOnly": "true"},
    {"planType": "loss_plan"},
    {"planType": "profit_plan"},
    {"planType": "pos_loss"},
    {"tradeSide": "close"},
])
def test_is_protective_detects_every_protective_shape(row):
    assert BitgetAdapter.is_protective(row) is True


@pytest.mark.parametrize("row", [
    {"reduceOnly": "NO", "tradeSide": "open"},
    {"orderType": "limit"},
    {},
])
def test_is_protective_false_for_entry_orders(row):
    assert BitgetAdapter.is_protective(row) is False


# ------------------------------------------------------------------ error typing
def test_bitget_error_repairable_classification():
    assert BitgetError("45110", "too small").repairable is True
    assert BitgetError("40006", "Invalid ACCESS_KEY").repairable is False


def test_signed_request_without_credentials_raises():
    ad = BitgetAdapter(paper=False)   # no key/secret/passphrase
    with pytest.raises(BitgetError) as exc:
        import asyncio
        asyncio.run(ad._signed("GET", "/api/v2/mix/account/accounts"))
    assert exc.value.code == "NO_CREDS"


# ------------------------------------------------------------------- paper gate
@pytest.mark.asyncio
async def test_place_order_in_paper_mode_never_touches_network(monkeypatch):
    ad = _adapter(paper=True)
    ad._spec_cache["BTCUSDT"] = BitgetAdapter._parse_spec(CONTRACT_BTC)

    async def boom(*a, **k):
        raise AssertionError("paper mode must not issue a signed request")

    monkeypatch.setattr(ad, "_signed", boom)
    oid = await ad.place_order(OrderRequest(instrument=Instrument.from_token("BTC"),
                                            side=Side.BUY, qty=0.01, price=60_000.0))
    assert oid.startswith("paper-")


@pytest.mark.asyncio
async def test_paper_cancel_and_leverage_are_noops(monkeypatch):
    ad = _adapter(paper=True)
    ad._spec_cache["BTCUSDT"] = BitgetAdapter._parse_spec(CONTRACT_BTC)

    async def boom(*a, **k):
        raise AssertionError("paper mode must not issue a signed request")

    monkeypatch.setattr(ad, "_signed", boom)
    assert await ad.cancel_order(Instrument.from_token("BTC"), "bg-1") is True
    assert await ad.cancel_all(None) is True
    await ad.set_leverage(Instrument.from_token("BTC"), 10)


@pytest.mark.asyncio
async def test_set_leverage_is_capped_by_pair_max(monkeypatch):
    ad = _adapter(paper=False)
    ad._spec_cache["BTCUSDT"] = BitgetAdapter._parse_spec(CONTRACT_PEPE)
    seen = {}

    async def fake_signed(method, path, params=None, body=None):
        seen.update(body or {})
        return {}

    monkeypatch.setattr(ad, "_signed", fake_signed)
    await ad.set_leverage(Instrument.from_token("BTC"), 500)
    assert seen["leverage"] == "50"     # clamped to maxLever


# ------------------------------------------------------------------- BUG C guard
@pytest.mark.asyncio
async def test_cancel_all_never_cancels_protective_orders(monkeypatch):
    """The single most important safety test: stops survive a dead-man cancel-all."""
    ad = _adapter(paper=False)
    cancelled: list[str] = []

    async def fake_open_orders(instrument=None):
        return [
            {"orderId": "entry-1", "symbol": "BTCUSDT", "reduceOnly": "NO"},
            {"orderId": "sl-1", "symbol": "BTCUSDT", "planType": "loss_plan"},
            {"orderId": "tp-1", "symbol": "BTCUSDT", "reduceOnly": "YES"},
        ]

    async def fake_signed(method, path, params=None, body=None):
        cancelled.append(str((body or {}).get("orderId")))
        return {}

    monkeypatch.setattr(ad, "get_open_orders", fake_open_orders)
    monkeypatch.setattr(ad, "_signed", fake_signed)
    await ad.cancel_all(None)
    assert cancelled == ["entry-1"]
    assert "sl-1" not in cancelled
    assert "tp-1" not in cancelled


@pytest.mark.asyncio
async def test_place_order_repairs_once_on_repairable_reject(monkeypatch):
    ad = _adapter(paper=False)
    ad._spec_cache["BTCUSDT"] = BitgetAdapter._parse_spec(CONTRACT_BTC)
    calls = {"n": 0}

    async def fake_signed(method, path, params=None, body=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise BitgetError("45110", "less than the minimum amount", path)
        return {"orderId": "repaired-1"}

    async def fake_public(path, params=None):
        return [CONTRACT_BTC]

    monkeypatch.setattr(ad, "_signed", fake_signed)
    monkeypatch.setattr(ad, "_public", fake_public)
    oid = await ad.place_order(OrderRequest(instrument=Instrument.from_token("BTC"),
                                            side=Side.BUY, qty=0.01, price=60_000.0))
    assert oid == "repaired-1"
    assert calls["n"] == 2   # exactly one resubmit, never a loop


@pytest.mark.asyncio
async def test_place_order_does_not_repair_unrepairable_error(monkeypatch):
    ad = _adapter(paper=False)
    ad._spec_cache["BTCUSDT"] = BitgetAdapter._parse_spec(CONTRACT_BTC)

    async def fake_signed(method, path, params=None, body=None):
        raise BitgetError("40006", "Invalid ACCESS_KEY", path)

    monkeypatch.setattr(ad, "_signed", fake_signed)
    with pytest.raises(BitgetError):
        await ad.place_order(OrderRequest(instrument=Instrument.from_token("BTC"),
                                          side=Side.BUY, qty=0.01, price=60_000.0))
