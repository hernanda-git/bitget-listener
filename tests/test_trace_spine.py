from dataclasses import asdict

import pytest

from src.domain.model import RawSignal
from src.services.orchestrator import Orchestrator
from src.services.risk import RiskLimits
from src.store.ledger import TradeLedger
from chart_signal_vision import PendingSetup


def test_trace_receipt_and_pending_setup_survive_ledger_reopen(tmp_path):
    path = str(tmp_path / "ledger.db")
    led = TradeLedger(path)
    assert led.record_received("fattyfatclub:11", 11, "fattyfatclub", "#WIF hold above 0.2", True)
    led.record_stage("fattyfatclub:11", 11, "PARSED", payload={"symbol": "WIFUSDT"})
    setup = PendingSetup("wif:0.2", "WIFUSDT", "LONG", 0.2)
    led.save_pending_setup(setup.setup_id, asdict(setup))
    led.close()

    reopened = TradeLedger(path)
    assert reopened.get_trace("fattyfatclub:11")[1]["stage"] == "PARSED"
    assert reopened.pending_setups()[0]["setup_id"] == setup.setup_id
    reopened.finish_pending_setup(setup.setup_id, "CONFIRMED")
    assert reopened.pending_setups() == []


@pytest.mark.asyncio
async def test_chart_execution_requires_independent_validation():
    orch = Orchestrator.__new__(Orchestrator)
    orch.deadman = None
    orch.limits = RiskLimits()
    orch.whitelist = set()
    orch.positions = type("Positions", (), {"positions": {}})()
    orch.daily_realized = 0.0
    orch.tradability = None
    orch.gate = type("Gate", (), {"paper": True})()
    orch.adapter = None

    rs = RawSignal(
        symbol_usdt="BTCUSDT", side="BUY", entry=100.0, sl=99.0,
        chart={"movement_direction": "BULLISH"},
        raw={"chart_executable": True},
    )
    assert await orch.guard(rs) == "chart failed independent executable validation"

    rs.chart["has_chart_data"] = True
    rs.chart["movement_direction"] = "BULLISH"
    rs.raw["chart_executable"] = True
    rs.has_media = True
    assert await orch.guard(rs) is None
