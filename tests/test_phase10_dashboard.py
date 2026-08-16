"""P10: dashboard API — open monitoring, gated controls, Jakarta timestamps."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.config.db import InMemoryConfigStore
from src.eval.shadow_ledger import ShadowLedger
from src.exchanges.fake import FakeExchangeAdapter
from src.notifier.golive import GateConditions, GoLiveGate
from src.services.orchestrator import Orchestrator
from src.sources.parser import SymbolMap
from src.sources.telegram import FakeSignalSource
from src.store.ledger import TradeLedger
from src.web.dashboard import create_app, jakarta

PASSWORD = "s3cret"
WHITELIST = {"BTCUSDT", "ETHUSDT"}


def build(paper=True, conditions=None, password=PASSWORD):
    fake = FakeExchangeAdapter()
    gate = GoLiveGate(conditions=conditions or GateConditions(),
                      config_store=InMemoryConfigStore())
    gate.paper = paper
    ledger = TradeLedger(":memory:")
    orch = Orchestrator(FakeSignalSource([], symbol_map=SymbolMap(WHITELIST)), fake, gate,
                        ledger, shadow=ShadowLedger(min_trades=2),
                        symbol_map=SymbolMap(WHITELIST), whitelist=WHITELIST)
    return fake, orch, gate, ledger, TestClient(create_app(orch, password=password))


# ---------------------------------------------------------------- Jakarta time
def test_jakarta_formats_utc_plus_7():
    # 2026-08-17T00:00:00Z -> 07:00 WIB the same day
    assert jakarta(1786060800000).endswith("WIB")
    assert "07:00:00" in jakarta(1786060800000)


def test_jakarta_handles_missing_timestamp():
    assert jakarta(None) == "-"
    assert jakarta(0) == "-"


# ------------------------------------------------------- open monitoring (no gate)
def test_state_endpoint_needs_no_password():
    """Monitoring is read-only and harmless: it must load without any auth."""
    *_, client = build()
    r = client.get("/api/state")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "PAPER"
    assert "net_usd" in body and "fees_usd" in body
    assert body["server_time"].endswith("WIB")


def test_index_page_loads_open():
    *_, client = build()
    r = client.get("/")
    assert r.status_code == 200
    assert "bitget-listener" in r.text
    # mobile-first contract
    assert 'name="viewport"' in r.text
    assert "overflow-x:hidden" in r.text
    assert "Asia/Jakarta" in r.text


def test_health_endpoint_is_open():
    *_, client = build()
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["mode"] == "PAPER"


def test_state_reports_trades_with_jakarta_timestamps():
    fake, orch, gate, ledger, client = build()
    tid = ledger.record_open("BTCUSDT", "BUY", 0.01, 60_000.0, open_fee=0.36)
    ledger.record_close(tid, exit_price=66_000.0, close_fee=0.39, exit_reason="tp")
    body = client.get("/api/state").json()
    assert body["closed_trades"] == 1
    row = body["trades"][0]
    assert row["closed_at"].endswith("WIB")
    assert row["net_usd"] == pytest.approx(60.0 - 0.75)


def test_state_shows_open_positions_and_real_liq():
    fake, orch, *_, client = build()
    orch.positions.upsert(fake.seed_position(liq=54_000.0))
    body = client.get("/api/state").json()
    assert body["positions"][0]["liq"] == 54_000.0
    assert body["positions"][0]["symbol"] == "BTCUSDT"


def test_state_exposes_unmet_gate_conditions():
    *_, client = build()
    gate = client.get("/api/state").json()["gate"]
    assert gate["conditions_met"] is False
    assert len(gate["unmet"]) == 5


def test_state_lists_skipped_signals():
    fake, orch, *_, client = build()
    orch.skipped.append({"symbol": "DOGEUSDT", "reason": "not whitelisted"})
    assert client.get("/api/state").json()["skipped"][0]["symbol"] == "DOGEUSDT"


# ----------------------------------------------------------- gated controls
def test_close_requires_the_password():
    *_, client = build()
    r = client.post("/api/close", json={"symbol": "BTCUSDT"})
    assert r.status_code == 401


def test_close_with_wrong_password_is_rejected():
    *_, client = build()
    r = client.post("/api/close", json={"symbol": "BTCUSDT"},
                    headers={"x-dash-password": "wrong"})
    assert r.status_code == 401


def test_close_with_password_closes_the_position():
    fake, orch, gate, ledger, client = build(paper=False)
    gate.paper = False
    orch.orders.paper = False
    orch.positions.upsert(fake.seed_position())
    r = client.post("/api/close", json={"symbol": "BTCUSDT"},
                    headers={"x-dash-password": PASSWORD})
    assert r.status_code == 200
    assert orch.positions.get("BTCUSDT") is None


def test_paper_toggle_requires_the_password():
    assert build()[-1].post("/api/paper").status_code == 401


def test_paper_toggle_with_password_restores_paper():
    fake, orch, gate, ledger, client = build(paper=False)
    gate.paper = False
    r = client.post("/api/paper", headers={"x-dash-password": PASSWORD})
    assert r.status_code == 200
    assert gate.paper is True


def test_go_live_requires_the_password():
    assert build()[-1].post("/api/live", json={"token": "x"}).status_code == 401


def test_go_live_rejects_an_invalid_token_even_with_the_password():
    met = GateConditions(probe_green=True, expectancy_positive=True, tests_green=True,
                         real_fee_observed=True, real_liq_price_observed=True)
    fake, orch, gate, ledger, client = build(conditions=met)
    gate.mint_token()
    r = client.post("/api/live", json={"token": "deadbeef"},
                    headers={"x-dash-password": PASSWORD})
    assert r.status_code == 400
    assert gate.paper is True


def test_go_live_refused_when_conditions_unmet():
    fake, orch, gate, ledger, client = build()
    r = client.post("/api/live", json={"token": "whatever"},
                    headers={"x-dash-password": PASSWORD})
    assert r.status_code == 400
    assert gate.paper is True


def test_go_live_succeeds_with_a_valid_token():
    met = GateConditions(probe_green=True, expectancy_positive=True, tests_green=True,
                         real_fee_observed=True, real_liq_price_observed=True)
    fake, orch, gate, ledger, client = build(conditions=met)
    _, token = gate.mint_token()
    r = client.post("/api/live", json={"token": token},
                    headers={"x-dash-password": PASSWORD})
    assert r.status_code == 200
    assert gate.paper is False


def test_controls_are_disabled_when_no_password_is_configured():
    """Fail closed: an unset DASH_PASSWORD must not mean 'no auth required'."""
    fake, orch, gate, ledger, client = build(password="")
    r = client.post("/api/paper", headers={"x-dash-password": ""})
    assert r.status_code == 503
    assert client.get("/api/state").status_code == 200   # monitoring still open


def test_control_actions_are_logged_at_error(caplog):
    import logging
    fake, orch, gate, ledger, client = build(paper=False)
    with caplog.at_level(logging.ERROR):
        client.post("/api/paper", headers={"x-dash-password": PASSWORD})
        client.post("/api/paper", headers={"x-dash-password": "nope"})
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "AUTHORIZED" in messages
    assert "REFUSED" in messages
