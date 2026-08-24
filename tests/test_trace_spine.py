from dataclasses import asdict

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
