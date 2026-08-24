from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

from chart_signal_vision import PendingSetup
from src.config.db import InMemoryConfigStore
from src.domain.model import RawSignal
from src.eval.shadow_ledger import ShadowLedger
from src.exchanges.fake import FakeExchangeAdapter
from src.notifier.golive import GateConditions, GoLiveGate
from src.services.orchestrator import Orchestrator
from src.services.order import OrderService
from src.services.risk import RiskLimits
from src.sources.llm_fallback import SignalLLM
from src.sources.parser import SymbolMap, parse_message
from src.sources.telegram import FakeSignalSource, TelethonSource
from src.store.ledger import TradeLedger
from src.web.dashboard import create_app


@pytest.mark.asyncio
async def test_catchup_records_receipt_before_parsing():
    import datetime as dt
    led = TradeLedger(":memory:")
    src = TelethonSource(channel="fattyfatclub", session="s", api_id=1,
                         api_hash="h", trace_store=led)

    class Msg:
        id = 101
        message = "$BTC tp1 booked"
        text = message
        media = None
        date = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=10)

    class Client:
        async def get_entity(self, ch): return "entity"
        async def iter_messages(self, entity, limit=None): yield Msg()

    src._client = Client()
    src.symbol_map = SymbolMap()
    await src._catch_up(limit=1)
    events = led.get_trace("fattyfatclub:101")
    assert events and events[0]["stage"] == "RECEIVED"


def test_llm_entry_without_direction_fails_closed():
    llm = SignalLLM(api_key="x")
    assert llm._to_rawsignal(
        {"symbol": "BTC", "side": "", "entry": 60000, "sl": 59000,
         "tp": 61000, "action": "entry"}, 1, 1, "x", "x", None) is None


@pytest.mark.asyncio
async def test_invalid_pending_setup_is_terminal_and_never_enters():
    fake = FakeExchangeAdapter(mark=60000)
    ledger = TradeLedger(":memory:")
    gate = GoLiveGate(conditions=GateConditions(), config_store=InMemoryConfigStore())
    gate.paper = False
    orch = Orchestrator(
        FakeSignalSource(symbol_map=SymbolMap({"BTCUSDT"}), trace_store=ledger),
        fake, gate, ledger, limits=RiskLimits(max_concurrent=2),
        symbol_map=SymbolMap({"BTCUSDT"}), whitelist={"BTCUSDT"})
    rs = RawSignal(symbol_usdt="BTCUSDT", base="BTC", side="BUY", entry=60000,
                   sl=59000, message_id=77, channel="fake",
                   raw={"pending_setup": {"setup_id": "bad", "symbol": "BTCUSDT",
                         "direction": "LONG", "trigger_price": 60000,
                         "condition": "hold_below"}})
    result = await orch.handle(rs)
    assert result["status"] == "REJECTED"
    assert result["reason"] == "INVALID_PENDING_SETUP"
    assert fake.placed == []


@pytest.mark.asyncio
async def test_close_rejects_quantity_that_rounds_to_zero():
    fake = FakeExchangeAdapter()
    pos = fake.seed_position(size=0.00001)
    result = await OrderService(fake, paper=False).close_position(pos, 0.5)
    assert result["status"] == "REJECTED"
    assert "zero" in result["reason"]
    assert fake.placed == []


def test_partial_close_keeps_remaining_trade_open():
    led = TradeLedger(":memory:")
    tid = led.record_open("BTCUSDT", "BUY", 1.0, 100.0, 1.0, paper=False)
    leg = led.record_partial_close(tid, 0.4, 110.0, 0.4, "tp1_booked")
    assert leg.qty == pytest.approx(0.4)
    assert led.open_trades()[0]["qty"] == pytest.approx(0.6)
    assert led.closed_trades()[0]["qty"] == pytest.approx(0.4)


def test_trace_endpoint_requires_dashboard_password():
    class Ledger:
        def get_trace(self, trace_id): return [{"stage": "RECEIVED", "payload": {"raw": "secret"}}]
        def closed_trades(self, limit): return []
        def open_trades(self): return []
    class Gate: paper = True
    class Orch:
        ledger = Ledger(); gate = Gate(); deadman = None
        def snapshot(self): return {"gate": {"paper": True}, "mode": "PAPER"}
    client = TestClient(create_app(Orch(), password="pw"))
    assert client.get("/api/trace/x").status_code == 401
    assert client.get("/api/trace/x", headers={"x-dash-password": "pw"}).status_code == 200
