from contextlib import contextmanager

import pytest

from src.domain.model import Instrument, Signal, Side
from src.exchanges.fake import FakeExchangeAdapter
from src.services.order import OrderService
from src.services.orchestrator import Orchestrator
from src.sources.parser import parse_message


def test_conditional_parser_fail_closed_and_preserves_limit_trigger():
    hold = parse_message("$BTW hold above 0.38 invalidation below 0.35")
    assert hold.raw["pending_setup"]["condition"] == "hold_above"
    assert hold.raw["pending_setup"]["trigger_price"] == 0.38
    assert hold.raw["pending_setup"]["direction"] == "LONG"

    pullback = parse_message("$WIF LONG pullback to 0.174 SL 0.165")
    assert pullback.raw["entry_order_type"] == "LIMIT"
    assert pullback.entry == 0.174
    assert "pending_setup" not in pullback.raw


@pytest.mark.asyncio
async def test_direction_conflict_is_explicit_and_orchestrator_rejects():
    rs = parse_message("$BTC LONG and SHORT entry 60000 SL 59000")
    assert rs.raw["direction_conflict"] is True
    orch = Orchestrator.__new__(Orchestrator)
    orch.deadman = None
    orch.whitelist = set()
    orch.positions = type("P", (), {"positions": {}})()
    orch.limits = type("L", (), {"max_concurrent": 2})()
    orch.daily_realized = 0.0
    orch.tradability = None
    orch.gate = type("G", (), {"paper": True})()
    orch.adapter = None
    assert await orch.guard(rs) == "DIRECTION_CONFLICT"


@pytest.mark.asyncio
async def test_limit_conditional_and_entry_margin_mode_are_explicit():
    adapter = FakeExchangeAdapter(mark=0.1996)
    service = OrderService(adapter, paper=False)
    signal = Signal(instrument=Instrument.from_token("WIF"), side=Side.BUY,
                    entry=0.174, sl=0.165, tp=None, notional_usd=0, leverage=1,
                    raw={"entry_order_type": "LIMIT"})
    result = await service.open_from_signal(signal)
    assert result["status"] == "PLACED"
    assert adapter.placed[0].order_type == "limit"
    assert adapter.placed[0].price < 0.1996
    assert adapter.placed[0].price <= 0.174
    assert adapter.placed[0].margin_mode == "crossed"


@pytest.mark.asyncio
async def test_favorable_pullback_uses_live_mark_for_market_entry():
    adapter = FakeExchangeAdapter(mark=0.16)
    service = OrderService(adapter, paper=False)
    signal = Signal(instrument=Instrument.from_token("WIF"), side=Side.BUY,
                    entry=0.174, sl=0.15, tp=None, notional_usd=0, leverage=1)
    result = await service.open_from_signal(signal)
    assert result["status"] == "PLACED"
    order = adapter.placed[0]
    assert order.order_type == "market"
    assert order.price is None
    assert result["sizing"].notional_usd == pytest.approx(order.qty * 0.16)


class _Cursor:
    def __init__(self, inserted=True, row=None):
        self.inserted = inserted
        self.row = row

    def fetchone(self):
        if self.row is not None:
            return self.row
        return {"id": 1} if self.inserted else None

    def fetchall(self):
        return []


class _Conn:
    def __init__(self):
        self.sql = []
        self.params = []
        self.inserted = True

    def execute(self, sql, params=()):
        self.sql.append(sql)
        self.params.append(params)
        if "SELECT trace_id FROM bc_signal_receipts" in sql:
            return _Cursor(row={"trace_id": "fattyfatclub:7"})
        if "bc_signal_receipts" in sql:
            cur = _Cursor(self.inserted)
            self.inserted = False
            return cur
        return _Cursor(True)



def test_postgres_trace_repository_is_idempotent_and_has_readback(monkeypatch):
    from bot_common import repos

    conn = _Conn()

    @contextmanager
    def fake_conn():
        yield conn

    monkeypatch.setattr(repos, "get_conn", fake_conn)
    assert repos.Traces.record_received(
        "bitget-listener", "fattyfatclub:7", 7, "fattyfatclub",
        raw_text="BTC LONG entry 60000 SL 59000", has_media=True,
        reply_context={"reply_to_message_id": 3}, service_identity="test") is True
    assert repos.Traces.record_received(
        "bitget-listener", "fattyfatclub:7", 7, "fattyfatclub",
        raw_text="BTC LONG entry 60000 SL 59000", has_media=True,
        service_identity="test") is False
    repos.Traces.record_stage("bitget-listener", "fattyfatclub:7", 7,
                              "GATE_EVALUATED", "REJECTED", "DIRECTION_CONFLICT")
    assert any("bc_signal_receipts" in sql for sql in conn.sql)
    assert any("bc_trace_events" in sql for sql in conn.sql)
    received_stage_params = [p for sql, p in zip(conn.sql, conn.params)
                             if "bc_trace_events" in sql and "RECEIVED" in p]
    assert received_stage_params[-1][0] == "fattyfatclub:7"


def test_postgres_schema_contains_trace_tables():
    from bot_common.db import DDL
    assert "CREATE TABLE IF NOT EXISTS bc_signal_receipts" in DDL
    assert "CREATE TABLE IF NOT EXISTS bc_trace_events" in DDL
    for field in ("payload_hash", "reply_context", "source_process", "schema_version"):
        assert field in DDL
