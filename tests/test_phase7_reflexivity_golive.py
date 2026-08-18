"""P7/P8/P9: ShadowLedger reflexivity, dead-man's switch, go-live gate, config store."""

from __future__ import annotations

import asyncio

import pytest

from src.config.db import BotConfig, InMemoryConfigStore
from src.domain.model import Fill, Instrument, Position, Side
from src.eval.shadow_ledger import (
    InMemoryEvalStore,
    LedgerEvalStore,
    ShadowLedger,
)
from src.exchanges.fake import FakeExchangeAdapter
from src.notifier.golive import GateConditions, GoLiveGate
from src.services.deadman import DeadManSwitch
from src.services.order import OrderService
from src.services.position import PositionManager
from src.store.ledger import TradeLedger

BTC = Instrument.from_token("BTC")


# ------------------------------------------------------------------ ShadowLedger
def test_rating_is_untrusted_below_min_trades():
    sl = ShadowLedger(min_trades=30)
    for _ in range(5):
        sl.record("BTCUSDT", False, -1.0)
    r = sl.rating("BTCUSDT")
    assert r.trades == 5
    assert r.trusted is False
    assert r.flag is False       # 5 losses must NOT switch off a pair


def test_rating_flags_a_weak_pair_once_trusted():
    sl = ShadowLedger(min_trades=10, poor_winrate=0.5)
    for i in range(10):
        sl.record("BTCUSDT", i < 3, 1.0 if i < 3 else -1.0)
    r = sl.rating("BTCUSDT")
    assert r.trusted is True
    assert r.winrate == pytest.approx(0.3)
    assert r.flag is True


def test_rating_does_not_flag_a_strong_pair():
    sl = ShadowLedger(min_trades=10)
    for i in range(10):
        sl.record("ETHUSDT", i < 8, 1.0)
    assert sl.rating("ETHUSDT").flag is False


def test_size_multiplier_is_one_when_auto_downsize_off():
    sl = ShadowLedger(min_trades=2, auto_downsize=False)
    sl.record("BTCUSDT", False, -1.0)
    sl.record("BTCUSDT", False, -1.0)
    assert sl.rating("BTCUSDT").flag is True
    assert sl.size_multiplier("BTCUSDT") == 1.0     # opt-in only


def test_size_multiplier_downsizes_when_enabled():
    sl = ShadowLedger(min_trades=2, auto_downsize=True, downsize_factor=0.5)
    sl.record("BTCUSDT", False, -1.0)
    sl.record("BTCUSDT", False, -1.0)
    assert sl.size_multiplier("BTCUSDT") == 0.5


def test_size_multiplier_never_exceeds_one():
    sl = ShadowLedger(min_trades=1, auto_downsize=True, downsize_factor=2.0)
    sl.record("BTCUSDT", True, 5.0)
    assert sl.size_multiplier("BTCUSDT") <= 1.0


def test_expectancy_is_fee_aware_mean_of_net():
    sl = ShadowLedger()
    sl.record("BTCUSDT", True, 2.0)
    sl.record("BTCUSDT", False, -1.0)
    assert sl.expectancy("BTCUSDT") == pytest.approx(0.5)


def test_expectancy_positive_requires_samples():
    sl = ShadowLedger()
    assert sl.expectancy_positive(None, min_samples=3) is False
    for _ in range(3):
        sl.record("BTCUSDT", True, 1.0)
    assert sl.expectancy_positive(None, min_samples=3) is True


def test_expectancy_positive_false_when_negative():
    sl = ShadowLedger()
    sl.record("BTCUSDT", False, -5.0)
    sl.record("BTCUSDT", True, 1.0)
    assert sl.expectancy_positive(None, min_samples=1) is False


def test_weak_pairs_listing():
    sl = ShadowLedger(min_trades=2)
    sl.record("BADUSDT", False, -1.0)
    sl.record("BADUSDT", False, -1.0)
    sl.record("GOODUSDT", True, 1.0)
    sl.record("GOODUSDT", True, 1.0)
    assert sl.weak_pairs() == ["BADUSDT"]


def test_shadow_ledger_reads_real_trades_through_the_ledger():
    led = TradeLedger(":memory:")
    tid = led.record_open("BTCUSDT", "BUY", 1.0, 100.0, open_fee=0.06)
    led.record_close(tid, exit_price=110.0, close_fee=0.06)
    sl = ShadowLedger(LedgerEvalStore(led), min_trades=1)
    r = sl.rating("BTCUSDT")
    assert r.trades == 1 and r.wins == 1
    assert sl.expectancy("BTCUSDT") == pytest.approx(9.88)


def test_in_memory_eval_store_nets_aggregate():
    store = InMemoryEvalStore()
    store.record("A", True, 1.0)
    store.record("B", False, -2.0)
    assert sorted(store.nets()) == [-2.0, 1.0]
    assert store.nets("A") == [1.0]


# ---------------------------------------------------------------------- deadman
def build_deadman(**kw):
    fake = FakeExchangeAdapter()
    mgr = PositionManager(fake, OrderService(fake, paper=False))

    class Stream:
        last_msg_ts = 0.0

    return fake, mgr, DeadManSwitch(Stream(), fake, mgr, ttl_s=1.0, **kw)


def test_deadman_is_stale_without_any_message():
    _, _, dms = build_deadman()
    assert dms.is_stale() is True


def test_deadman_not_stale_with_fresh_message():
    import time
    _, _, dms = build_deadman()
    dms.stream.last_msg_ts = time.time()
    assert dms.is_stale() is False


def test_deadman_trading_allowed_flag():
    _, _, dms = build_deadman()
    assert dms.trading_allowed is True
    dms.halted = True
    assert dms.trading_allowed is False


@pytest.mark.asyncio
async def test_deadman_trip_preserves_protective_orders():
    """The most important deadman property: stops survive the halt (BUG C)."""
    fake, _, dms = build_deadman()
    fake.open_orders = [
        {"orderId": "e1", "symbol": "BTCUSDT", "reduceOnly": "NO"},
        {"orderId": "sl1", "symbol": "BTCUSDT", "planType": "loss_plan"},
    ]
    res = await dms.trip("feed stale")
    assert dms.halted is True
    assert res["protective_preserved"] == 1
    assert "sl1" not in fake.cancelled
    assert "e1" in fake.cancelled


@pytest.mark.asyncio
async def test_deadman_trip_does_not_flatten_by_default():
    fake, mgr, dms = build_deadman()
    mgr.upsert(fake.seed_position())
    res = await dms.trip("stale")
    assert res["flattened"] == []
    assert mgr.get("BTCUSDT") is not None


@pytest.mark.asyncio
async def test_deadman_trip_flattens_when_configured():
    fake, mgr, dms = build_deadman(flatten=True)
    mgr.upsert(fake.seed_position())
    res = await dms.trip("stale")
    assert res["flattened"] == ["BTCUSDT"]


@pytest.mark.asyncio
async def test_deadman_notifies_on_trip():
    fake, mgr, dms = build_deadman()
    sent = []

    class N:
        async def send(self, text):
            sent.append(text)

    dms.notifier = N()
    await dms.trip("feed stale")
    assert sent and "DEAD-MAN" in sent[0]


@pytest.mark.asyncio
async def test_deadman_recovers_when_feed_returns():
    _, _, dms = build_deadman()
    await dms.trip("stale")
    await dms.recover()
    assert dms.halted is False


@pytest.mark.asyncio
async def test_deadman_loop_trips_then_recovers():
    import time
    fake, _, dms = build_deadman()
    dms.check_interval_s = 0.01
    dms.ttl_s = 0.05
    await dms.start()
    await asyncio.sleep(0.08)
    assert dms.halted is True
    dms.stream.last_msg_ts = time.time()
    await asyncio.sleep(0.05)
    assert dms.halted is False
    await dms.stop()


# -------------------------------------------------------------------- go-live
def test_gate_starts_in_paper_mode():
    assert GoLiveGate().paper is True


def test_gate_refuses_to_mint_with_unmet_conditions():
    gate = GoLiveGate()
    ok, msg = gate.mint_token()
    assert ok is False
    assert "REFUSED" in msg
    assert "bitget_api_probe" in msg


def test_gate_lists_every_unmet_condition():
    unmet = GateConditions().unmet()
    assert len(unmet) == 5
    assert any("BUG A" in u for u in unmet)
    assert any("BUG B" in u for u in unmet)


def all_met_conditions():
    return GateConditions(probe_green=True, expectancy_positive=True, tests_green=True,
                          real_fee_observed=True, real_liq_price_observed=True)


def test_gate_mints_when_all_conditions_met():
    gate = GoLiveGate(conditions=all_met_conditions())
    ok, token = gate.mint_token()
    assert ok is True and len(token) == 8


def test_gate_redeem_flips_paper_off():
    gate = GoLiveGate(conditions=all_met_conditions())
    _, token = gate.mint_token()
    ok, msg = gate.redeem(token)
    assert ok is True and gate.paper is False
    assert "LIVE" in msg


def test_gate_redeem_is_single_use():
    gate = GoLiveGate(conditions=all_met_conditions())
    _, token = gate.mint_token()
    gate.redeem(token)
    gate.paper = True
    ok, _ = gate.redeem(token)
    assert ok is False


def test_gate_rejects_wrong_token():
    gate = GoLiveGate(conditions=all_met_conditions())
    gate.mint_token()
    ok, msg = gate.redeem("deadbeef")
    assert ok is False and "invalid" in msg
    assert gate.paper is True


def test_gate_rejects_redeem_without_minting():
    ok, msg = GoLiveGate(conditions=all_met_conditions()).redeem("abcd1234")
    assert ok is False and "no active" in msg


def test_gate_rejects_expired_token():
    import time
    gate = GoLiveGate(conditions=all_met_conditions())
    _, token = gate.mint_token()
    gate._token_ts = time.time() - 10_000
    ok, msg = gate.redeem(token)
    assert ok is False and "expired" in msg
    assert gate.paper is True


def test_gate_rejects_if_conditions_regress_after_minting():
    gate = GoLiveGate(conditions=all_met_conditions())
    _, token = gate.mint_token()
    gate.conditions.probe_green = False
    ok, msg = gate.redeem(token)
    assert ok is False and "conditions changed" in msg
    assert gate.paper is True


def test_gate_return_to_paper_is_always_allowed():
    gate = GoLiveGate(conditions=all_met_conditions())
    _, token = gate.mint_token()
    gate.redeem(token)
    gate.to_paper("operator")
    assert gate.paper is True


def test_gate_toggle_live_to_paper_is_free():
    gate = GoLiveGate(conditions=all_met_conditions())
    _, token = gate.mint_token()
    gate.redeem(token)
    msg = gate.toggle()
    assert gate.paper is True and "paper mode restored" in msg


def test_gate_toggle_paper_to_live_is_gated():
    gate = GoLiveGate()
    msg = gate.toggle()
    assert "cannot switch to live" in msg
    assert gate.paper is True


def test_gate_persists_paper_flag_to_the_config_store():
    store = InMemoryConfigStore()
    gate = GoLiveGate(conditions=all_met_conditions(), config_store=store)
    _, token = gate.mint_token()
    gate.redeem(token)
    assert store.get_paper() is False


def test_gate_override_mints_despite_unmet_conditions(monkeypatch):
    """Operator-authorized bypass: GO_LIVE_OVERRIDE=1 mints even with all
    conditions unmet. The bypass is the ONLY way to mint without real fills."""
    monkeypatch.setenv("GO_LIVE_OVERRIDE", "1")
    gate = GoLiveGate()  # all conditions False
    ok, token = gate.mint_token()
    assert ok is True
    assert len(token) == 8
    # and redemption succeeds
    ok2, _ = gate.redeem(token)
    assert ok2 is True and gate.paper is False


def test_gate_override_inactive_without_env(monkeypatch):
    monkeypatch.delenv("GO_LIVE_OVERRIDE", raising=False)
    gate = GoLiveGate()
    ok, msg = gate.mint_token()
    assert ok is False and "REFUSED" in msg


def test_gate_stays_paper_when_persistence_fails():
    """Fail closed: if we cannot persist paper=0, we must not go live."""
    class Broken:
        def set_paper(self, paper):
            raise RuntimeError("db down")

    gate = GoLiveGate(conditions=all_met_conditions(), config_store=Broken())
    _, token = gate.mint_token()
    gate.redeem(token)
    assert gate.paper is True


def test_gate_observes_real_fee_condition():
    gate = GoLiveGate()
    gate.observe_fill(Fill(client_order_id="paper-1", price=1, qty=1, side="BUY", fee=0.5))
    assert gate.conditions.real_fee_observed is False    # paper does not count
    gate.observe_fill(Fill(client_order_id="bg-1", price=1, qty=1, side="BUY", fee=0.5))
    assert gate.conditions.real_fee_observed is True


def test_gate_ignores_zero_fee_fill():
    gate = GoLiveGate()
    gate.observe_fill(Fill(client_order_id="bg-1", price=1, qty=1, side="BUY", fee=0.0))
    assert gate.conditions.real_fee_observed is False


def test_gate_observes_real_liq_price_condition():
    gate = GoLiveGate()
    gate.observe_position(Position(instrument=BTC, side=Side.BUY, size=1, entry_price=1,
                                   liq_price=None))
    assert gate.conditions.real_liq_price_observed is False
    gate.observe_position(Position(instrument=BTC, side=Side.BUY, size=1, entry_price=1,
                                   liq_price=0.0))
    assert gate.conditions.real_liq_price_observed is False
    gate.observe_position(Position(instrument=BTC, side=Side.BUY, size=1, entry_price=1,
                                   liq_price=54_000.0))
    assert gate.conditions.real_liq_price_observed is True


def test_gate_observes_expectancy_from_shadow_ledger():
    gate = GoLiveGate()
    sl = ShadowLedger()
    for _ in range(10):
        sl.record("BTCUSDT", True, 1.0)
    gate.observe_expectancy(sl, min_samples=10)
    assert gate.conditions.expectancy_positive is True


def test_gate_status_reports_mode_and_unmet():
    st = GoLiveGate().status()
    assert st["mode"] == "PAPER"
    assert st["conditions_met"] is False
    assert len(st["unmet"]) == 5


# ---------------------------------------------------------------- config store
def test_config_defaults_are_paper_and_safe():
    cfg = BotConfig()
    assert cfg.paper is True
    assert cfg.params["max_daily_loss_pct"] == 30.0
    assert cfg.params["whitelist_only"] is True
    assert cfg.params["product_type"] == "SUSDT-FUTURES"   # demo by default


def test_config_store_persists_paper_flag():
    store = InMemoryConfigStore()
    store.set_paper(False)
    assert store.get_paper() is False
    assert store.writes == [False]


def test_config_secret_resolution_uses_env_names_only(monkeypatch):
    """The DB stores env-var NAMES; values live only in the environment."""
    monkeypatch.setenv("BITGET_API_KEY", "secret-value")
    cfg = BotConfig(secret_env={"api_key": "BITGET_API_KEY"})
    assert cfg.resolve_secret("api_key") == "secret-value"
    assert "secret-value" not in str(cfg.secret_env)


def test_config_secret_missing_returns_empty():
    assert BotConfig().resolve_secret("api_key") == ""


def test_config_set_params_merges():
    store = InMemoryConfigStore()
    store.set_params({"max_leverage": 5})
    assert store.load().params["max_leverage"] == 5
    assert store.load().params["max_position_usd"] == 100.0
