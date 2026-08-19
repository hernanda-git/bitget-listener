"""P4: BitgetWS frames, parsing, heartbeat, reconnect — with a fake connector (zero network)."""

from __future__ import annotations

import asyncio
import json

import pytest

from src.domain.model import Side
from src.exchanges.bitget_auth import BitgetAuth
from src.streaming.bitget_ws import BitgetWS


class FakeWS:
    """Minimal async-iterable websocket double."""

    def __init__(self, inbound: list, fail_after: bool = False) -> None:
        self.inbound = list(inbound)
        self.sent: list[str] = []
        self.closed = False
        self.fail_after = fail_after

    async def send(self, payload):
        self.sent.append(payload)

    async def recv(self):
        if self.inbound:
            return self.inbound.pop(0)
        await asyncio.sleep(3600)

    async def close(self):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.inbound:
            return self.inbound.pop(0)
        if self.fail_after:
            raise ConnectionError("socket dropped")
        raise StopAsyncIteration


def connector_for(ws: FakeWS):
    class _Ctx:
        async def __aenter__(self_inner):
            return ws

        async def __aexit__(self_inner, *a):
            return False

    def _connect(url, **kw):
        return _Ctx()

    return _connect


TICK = json.dumps({"action": "snapshot",
                   "arg": {"instType": "USDT-FUTURES", "channel": "ticker", "instId": "BTCUSDT"},
                   "data": [{"instId": "BTCUSDT", "lastPr": "63100", "markPrice": "63111.5"}]})


# ------------------------------------------------------------------------ frames
def test_public_subscribe_frame_uses_verified_object_args():
    ws = BitgetWS(symbols=["BTCUSDT", "ETHUSDT"], product_type="USDT-FUTURES")
    frame = ws.subscribe_frame()
    assert frame["op"] == "subscribe"
    assert frame["args"][0] == {"instType": "USDT-FUTURES", "channel": "ticker",
                                "instId": "BTCUSDT"}
    # the analysis doc's "tickers.BTCUSDT" string form is wrong and must not appear
    assert all(isinstance(a, dict) for a in frame["args"])


def test_public_channel_name_is_singular_ticker():
    frame = BitgetWS(symbols=["BTCUSDT"]).subscribe_frame()
    assert frame["args"][0]["channel"] == "ticker"


def test_private_subscribe_frame_covers_orders_positions_account():
    ws = BitgetWS(private=True, auth=BitgetAuth("k", "s", "p"))
    channels = {a["channel"] for a in ws.subscribe_frame()["args"]}
    assert channels == {"orders", "positions", "account"}


def test_default_urls_are_the_verified_v2_hosts():
    assert BitgetWS().url == "wss://ws.bitget.com/v2/ws/public"
    assert BitgetWS(private=True, auth=BitgetAuth("k", "s", "p")).url == \
        "wss://ws.bitget.com/v2/ws/private"
    # the dead host must not be the default anywhere
    assert "mix/v1/stream" not in BitgetWS().url


def test_login_frame_requires_auth():
    with pytest.raises(ValueError):
        BitgetWS(private=True).login_frame()


def test_login_frame_shape():
    frame = BitgetWS(private=True, auth=BitgetAuth("k", "s", "p")).login_frame()
    assert frame["op"] == "login"
    assert set(frame["args"][0]) == {"apiKey", "passphrase", "timestamp", "sign"}


# ----------------------------------------------------------------------- parsing
def test_loads_treats_plain_pong_as_event():
    assert BitgetWS._loads("pong") == {"event": "pong"}
    assert BitgetWS._loads(b"pong") == {"event": "pong"}


def test_loads_survives_garbage():
    assert BitgetWS._loads("<html>")["event"] == "unparsed"


def test_parse_order_event_emits_fill_with_real_fee():
    fill = BitgetWS._parse_order_event({
        "instId": "BTCUSDT", "orderId": "1", "clientOid": "bg-1", "status": "filled",
        "baseVolume": "0.01", "priceAvg": "63000", "side": "buy", "tradeSide": "open",
        "feeDetail": [{"feeCoin": "USDT", "totalFee": "-0.378"}], "uTime": "1700",
    })
    assert fill is not None
    assert fill.fee == pytest.approx(0.378)
    assert fill.qty == 0.01
    assert fill.is_reduce is False


def test_parse_order_event_ignores_unfilled_states():
    for status in ("live", "new", "cancelled", "rejected"):
        assert BitgetWS._parse_order_event({"status": status, "baseVolume": "0"}) is None


def test_parse_order_event_ignores_zero_volume_fill():
    assert BitgetWS._parse_order_event({"status": "filled", "baseVolume": "0"}) is None


def test_parse_order_event_flags_reduce_only_close():
    fill = BitgetWS._parse_order_event({
        "instId": "BTCUSDT", "status": "filled", "baseVolume": "1", "priceAvg": "1",
        "tradeSide": "close", "fee": "-0.01"})
    assert fill is not None and fill.is_reduce is True


def test_parse_position_event_keeps_real_liq_price():
    pos = BitgetWS._parse_position_event({
        "instId": "BTCUSDT", "holdSide": "short", "total": "0.5", "openPriceAvg": "63000",
        "leverage": "10", "liquidationPrice": "69000", "marginMode": "isolated"})
    assert pos.liq_price == 69_000.0
    assert pos.side is Side.SELL


def test_parse_position_event_zero_liq_is_none():
    pos = BitgetWS._parse_position_event({
        "instId": "BTCUSDT", "holdSide": "long", "total": "1", "openPriceAvg": "1",
        "liquidationPrice": "0"})
    assert pos.liq_price is None


# -------------------------------------------------------------------- dispatch
@pytest.mark.asyncio
async def test_dispatch_ticker_updates_mark_cache_and_callback():
    ws = BitgetWS(symbols=["BTCUSDT"])
    seen = []
    ws.on_tick = lambda sym, px: seen.append((sym, px))
    await ws._dispatch(TICK)
    assert ws.mark_price("BTCUSDT") == 63_111.5   # prefers markPrice over lastPr
    assert seen == [("BTCUSDT", 63_111.5)]


@pytest.mark.asyncio
async def test_dispatch_falls_back_to_last_price():
    ws = BitgetWS(symbols=["BTCUSDT"])
    await ws._dispatch(json.dumps({"arg": {"channel": "ticker"},
                                   "data": [{"instId": "BTCUSDT", "lastPr": "100"}]}))
    assert ws.mark_price("btcusdt") == 100.0


@pytest.mark.asyncio
async def test_dispatch_orders_invokes_on_fill_async_callback():
    ws = BitgetWS(private=True, auth=BitgetAuth("k", "s", "p"))
    got = []

    async def on_fill(f):
        got.append(f)

    ws.on_fill = on_fill
    await ws._dispatch(json.dumps({"arg": {"channel": "orders"}, "data": [{
        "instId": "BTCUSDT", "status": "filled", "baseVolume": "0.01", "priceAvg": "63000",
        "feeDetail": [{"totalFee": "-0.37"}]}]}))
    assert len(got) == 1 and got[0].fee == pytest.approx(0.37)


@pytest.mark.asyncio
async def test_dispatch_positions_and_account():
    ws = BitgetWS(private=True, auth=BitgetAuth("k", "s", "p"))
    positions, wallet = [], []
    ws.on_position = lambda p: positions.append(p)
    ws.on_wallet = lambda coin, avail: wallet.append((coin, avail))
    await ws._dispatch(json.dumps({"arg": {"channel": "positions"}, "data": [
        {"instId": "BTCUSDT", "holdSide": "long", "total": "1", "openPriceAvg": "63000",
         "liquidationPrice": "50000"}]}))
    await ws._dispatch(json.dumps({"arg": {"channel": "account"}, "data": [
        {"marginCoin": "USDT", "available": "912.5"}]}))
    assert positions[0].liq_price == 50_000.0
    assert wallet == [("USDT", 912.5)]


@pytest.mark.asyncio
async def test_dispatch_ignores_control_frames():
    ws = BitgetWS(symbols=["BTCUSDT"])
    ws.on_tick = lambda *a: pytest.fail("control frame must not reach a callback")
    for frame in ('{"event":"subscribe"}', '{"event":"login"}', "pong", '{"event":"error"}'):
        await ws._dispatch(frame)


# -------------------------------------------------------------------- lifecycle
@pytest.mark.asyncio
async def test_session_sends_subscribe_and_marks_connected():
    fake = FakeWS([TICK])
    ws = BitgetWS(symbols=["BTCUSDT"], connector=connector_for(fake))
    ready = []
    ws.on_ready = lambda: ready.append(True)
    await ws._session()
    assert json.loads(fake.sent[0])["op"] == "subscribe"
    assert ready == [True]
    assert ws.mark_price("BTCUSDT") == 63_111.5


@pytest.mark.asyncio
async def test_private_session_logs_in_before_subscribing():
    fake = FakeWS([json.dumps({"event": "login", "code": "0"}), TICK])
    ws = BitgetWS(private=True, auth=BitgetAuth("k", "s", "p"),
                  connector=connector_for(fake))
    await ws._session()
    assert json.loads(fake.sent[0])["op"] == "login"
    assert json.loads(fake.sent[1])["op"] == "subscribe"


@pytest.mark.asyncio
async def test_private_session_raises_on_login_error():
    fake = FakeWS([json.dumps({"event": "error", "code": "30005", "msg": "login failed"})])
    ws = BitgetWS(private=True, auth=BitgetAuth("k", "s", "p"),
                  connector=connector_for(fake))
    with pytest.raises(RuntimeError):
        await ws._session()


@pytest.mark.asyncio
async def test_heartbeat_sends_literal_ping_not_json():
    """VERIFIED: Bitget expects the literal text 'ping'."""
    fake = FakeWS([])
    ws = BitgetWS(symbols=["BTCUSDT"], ping_interval=0.01,
                  connector=connector_for(fake))
    task = asyncio.create_task(ws._heartbeat(fake))
    await asyncio.sleep(0.05)
    task.cancel()
    assert "ping" in fake.sent
    assert '{"op": "ping"}' not in fake.sent


@pytest.mark.asyncio
async def test_reconnect_backoff_emits_on_lost_and_retries():
    lost = []
    attempts = {"n": 0}

    def flaky_connector(url, **kw):
        attempts["n"] += 1

        class _Ctx:
            async def __aenter__(self_inner):
                raise ConnectionError("refused")

            async def __aexit__(self_inner, *a):
                return False

        return _Ctx()

    ws = BitgetWS(symbols=["BTCUSDT"], connector=flaky_connector, max_backoff=0.01)
    ws.on_lost = lambda reason: lost.append(reason)
    await ws.start()
    await asyncio.sleep(0.08)
    await ws.stop()
    assert attempts["n"] >= 2      # it retried
    assert lost                     # and reported the loss
    assert ws.reconnects >= 1


@pytest.mark.asyncio
async def test_is_stale_before_any_message_and_after_tick():
    ws = BitgetWS(symbols=["BTCUSDT"])
    assert ws.is_stale() is True         # never received anything -> stale
    await ws._dispatch(TICK)
    ws.last_msg_ts = __import__("time").time()
    assert ws.is_stale(ttl=60) is False


@pytest.mark.asyncio
async def test_stop_is_idempotent_and_marks_disconnected():
    ws = BitgetWS(symbols=["BTCUSDT"], connector=connector_for(FakeWS([])))
    await ws.stop()
    await ws.stop()
    assert ws.connected is False


@pytest.mark.asyncio
async def test_mark_price_unknown_symbol_is_none():
    assert BitgetWS(symbols=["BTCUSDT"]).mark_price("DOGEUSDT") is None


# -------------------------------------------------------------------- BUG #1 regression
def test_empty_whitelist_public_subscribe_uses_liveness_sentinel():
    """BUG #1 REGRESSION: empty whitelist must NOT produce `args: []`.

    An empty args list makes Bitget reject the subscribe with 30002, so no
    ticker frames arrive and the dead-man's-switch starves. A real symbol
    (the liveness sentinel) must be subscribed instead.
    """
    ws = BitgetWS(symbols=[], product_type="USDT-FUTURES")
    frame = ws.subscribe_frame()
    assert frame["op"] == "subscribe"
    assert frame["args"], "empty subscribe args would be rejected by Bitget (30002)"
    assert frame["args"][0] == {
        "instType": "USDT-FUTURES", "channel": "ticker",
        "instId": BitgetWS.LIVENESS_SENTINEL,
    }


def test_empty_whitelist_sentinel_is_the_verified_default_listing():
    """The sentinel must be a symbol Bitget actually lists on USDT-FUTURES."""
    ws = BitgetWS(symbols=[], product_type="USDT-FUTURES")
    sent = ws.subscribe_frame()["args"][0]["instId"]
    assert sent == "BTCUSDT"


def test_whitelist_present_does_not_inject_sentinel():
    """When the whitelist is non-empty the sentinel must not appear."""
    ws = BitgetWS(symbols=["PUMPUSDT", "DOGEUSDT"], product_type="USDT-FUTURES")
    inst_ids = [a["instId"] for a in ws.subscribe_frame()["args"]]
    assert inst_ids == ["PUMPUSDT", "DOGEUSDT"]
    assert BitgetWS.LIVENESS_SENTINEL not in inst_ids


def test_private_stream_unaffected_by_sentinel_fix():
    """Private subscribe frames are a fixed channel set and must not change."""
    ws = BitgetWS(symbols=[], private=True, auth=BitgetAuth("k", "s", "p"))
    channels = {a["channel"] for a in ws.subscribe_frame()["args"]}
    assert channels == {"orders", "positions", "account"}


@pytest.mark.asyncio
async def test_empty_whitelist_session_subscribes_sentinel_and_feeds_deadman():
    """End-to-end: empty whitelist -> WS subscribes BTCUSDT and a ticker frame
    refreshes last_msg_ts so the dead-man's-switch stays healthy (not stale)."""
    fake = FakeWS([TICK])
    ws = BitgetWS(symbols=[], product_type="USDT-FUTURES", connector=connector_for(fake))
    ready = []
    ws.on_ready = lambda: ready.append(True)
    await ws._session()
    sent = json.loads(fake.sent[0])
    assert sent["op"] == "subscribe"
    assert sent["args"][0]["instId"] == "BTCUSDT"
    # a ticker frame for the sentinel arrived and updated last_msg_ts
    assert ws.mark_price("BTCUSDT") == 63_111.5
    assert ws.is_stale(ttl=60) is False
    assert ready == [True]


@pytest.mark.asyncio
async def test_add_symbols_resubscribes_without_dropping_existing():
    """Runtime add: an open position's coin is tracked live, the subscribe frame
    carries it, and the liveness sentinel is (re)used as the dead-man feed when
    no real symbol is tracked but is dropped in favour of the real symbol (which
    also feeds the watchdog)."""
    fake = FakeWS([TICK])
    ws = BitgetWS(symbols=[], product_type="USDT-FUTURES", connector=connector_for(fake))
    ws._loop = asyncio.get_event_loop()
    await ws.start()
    await asyncio.sleep(0.05)  # let the session subscribe once (sentinel only)
    assert ws.tracked_symbols() == []  # only the sentinel so far
    ws.add_symbols(["PUMPUSDT"])
    await asyncio.sleep(0.05)
    assert "PUMPUSDT" in ws.symbols
    # the latest subscribe frame must carry PUMPUSDT (dead-man fed by it now)
    sub_frames = [json.loads(s) for s in fake.sent if json.loads(s).get("op") == "subscribe"]
    last = sub_frames[-1]["args"]
    inst_ids = {a.get("instId") for a in last}
    assert "PUMPUSDT" in inst_ids
    await ws.stop()


@pytest.mark.asyncio
async def test_remove_symbols_returns_to_sentinel_feed():
    fake = FakeWS([TICK])
    ws = BitgetWS(symbols=[], product_type="USDT-FUTURES", connector=connector_for(fake))
    ws._loop = asyncio.get_event_loop()
    await ws.start()
    await asyncio.sleep(0.05)
    ws.add_symbols(["PUMPUSDT"])
    await asyncio.sleep(0.05)
    assert "PUMPUSDT" in ws.symbols
    ws.remove_symbols(["PUMPUSDT"])
    await asyncio.sleep(0.05)
    assert "PUMPUSDT" not in ws.symbols
    # with no real symbols left, the liveness sentinel is re-subscribed
    sub_frames = [json.loads(s) for s in fake.sent if json.loads(s).get("op") == "subscribe"]
    assert BitgetWS.LIVENESS_SENTINEL in {a.get("instId") for a in sub_frames[-1]["args"]}
    await ws.stop()
