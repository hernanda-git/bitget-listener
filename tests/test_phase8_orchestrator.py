"""P8/P10: orchestrator end-to-end (fakes only) + notifier/command router."""

from __future__ import annotations

import pytest

from src.config.db import InMemoryConfigStore
from src.eval.shadow_ledger import ShadowLedger
from src.exchanges.fake import FakeExchangeAdapter
from src.notifier.golive import GateConditions, GoLiveGate
from src.notifier.telegram import CommandRouter, TelegramNotifier
from src.services.deadman import DeadManSwitch
from src.services.orchestrator import Orchestrator
from src.services.risk import RiskLimits
from src.sources.parser import SymbolMap
from src.sources.telegram import FakeSignalSource
from src.store.ledger import TradeLedger

WHITELIST = {"BTCUSDT", "ETHUSDT"}
LONG = "BTCUSDT LONG entry 60000 sl 57000 tp 66000 lev 10"


def build(messages=None, paper=True, whitelist=WHITELIST, limits=None, deadman=False,
          conditions=None):
    fake = FakeExchangeAdapter()
    source = FakeSignalSource(messages or [], symbol_map=SymbolMap(WHITELIST))
    gate = GoLiveGate(conditions=conditions or GateConditions(),
                      config_store=InMemoryConfigStore())
    gate.paper = paper
    ledger = TradeLedger(":memory:")
    notifier = TelegramNotifier(token="", chat_id="")
    dms = None
    if deadman:
        class S:
            last_msg_ts = 0.0
        dms = DeadManSwitch(S(), fake, None, ttl_s=1.0)
    orch = Orchestrator(source, fake, gate, ledger, notifier=notifier, deadman=dms,
                        shadow=ShadowLedger(min_trades=2),
                        limits=limits or RiskLimits(max_concurrent=2, equity_usd=100.0),
                        symbol_map=SymbolMap(WHITELIST), whitelist=whitelist)
    return fake, orch, gate, ledger, notifier


# --------------------------------------------------------------------- guards
@pytest.mark.asyncio
async def test_signal_outside_whitelist_is_skipped():
    fake, orch, *_ = build()
    src = orch.source
    await src.start()
    await src.push("SOLUSDT LONG entry 150 sl 140")
    await orch.run_once()
    assert fake.placed == []
    assert "not on allow-list" in orch.skipped[-1]["reason"]


@pytest.mark.asyncio
async def test_chatter_is_skipped_without_a_symbol():
    fake, orch, *_ = build()
    await orch.source.start()
    await orch.source.push("gm frens")
    await orch.run_once()
    assert fake.placed == []


@pytest.mark.asyncio
async def test_max_concurrent_blocks_a_third_position():
    fake, orch, *_ = build(paper=False, limits=RiskLimits(max_concurrent=1))
    await orch.source.start()
    await orch.source.push(LONG, message_id=1)
    await orch.run_once()
    await orch.source.push("ETHUSDT LONG entry 2500 sl 2400", message_id=2)
    await orch.run_once()
    assert any("max concurrent" in s["reason"] for s in orch.skipped)


@pytest.mark.asyncio
async def test_duplicate_symbol_position_is_skipped():
    fake, orch, *_ = build(paper=False)
    orch.positions.upsert(fake.seed_position())
    await orch.source.start()
    await orch.source.push(LONG, message_id=9)
    await orch.run_once()
    assert any("already in a" in s["reason"] for s in orch.skipped)


@pytest.mark.asyncio
async def test_daily_loss_limit_halts_new_entries():
    fake, orch, *_ = build(paper=False, limits=RiskLimits(max_daily_loss_pct=10,
                                                          equity_usd=100.0))
    orch.daily_realized = -25.0
    assert orch.daily_loss_breached() is True
    await orch.source.start()
    await orch.source.push(LONG, message_id=3)
    await orch.run_once()
    assert fake.placed == []


@pytest.mark.asyncio
async def test_deadman_halt_blocks_entries():
    fake, orch, *_ = build(paper=False, deadman=True)
    await orch.deadman.trip("stale feed")
    await orch.source.start()
    await orch.source.push(LONG, message_id=4)
    await orch.run_once()
    assert fake.placed == []
    assert any("dead-man" in s["reason"] for s in orch.skipped)


# ---------------------------------------------------------------- paper / live
@pytest.mark.asyncio
async def test_paper_mode_records_a_trade_without_placing():
    fake, orch, gate, ledger, _ = build(paper=True)
    await orch.source.start()
    await orch.source.push(LONG, message_id=5)
    await orch.run_once()
    assert fake.placed == []
    rows = ledger.open_trades()
    assert len(rows) == 1
    assert rows[0]["paper"] == 1
    assert rows[0]["open_fee"] > 0        # BUG A: fee recorded even in paper


@pytest.mark.asyncio
async def test_live_mode_places_a_real_order():
    fake, orch, gate, ledger, _ = build(paper=False)
    await orch.source.start()
    await orch.source.push(LONG, message_id=6)
    await orch.run_once()
    assert len(fake.placed) == 1
    assert ledger.open_trades()[0]["paper"] == 0


@pytest.mark.asyncio
async def test_paper_flag_is_read_from_the_gate_per_order():
    """A mid-session /paper must take effect on the very next signal."""
    fake, orch, gate, ledger, _ = build(paper=False)
    await orch.source.start()
    await orch.source.push(LONG, message_id=7)
    await orch.run_once()
    assert len(fake.placed) == 1
    gate.to_paper("test")
    await orch.source.push("ETHUSDT LONG entry 2500 sl 2400", message_id=8)
    await orch.run_once()
    assert len(fake.placed) == 1          # no new live order


@pytest.mark.asyncio
async def test_signal_without_sl_is_rejected_end_to_end():
    fake, orch, *_ = build(paper=False)
    await orch.source.start()
    await orch.source.push("BTCUSDT LONG entry 60000", message_id=10)
    await orch.run_once()
    assert fake.placed == []


# ------------------------------------------------------------------ management
@pytest.mark.asyncio
async def test_management_sl_to_entry_uses_the_open_position():
    fake, orch, *_ = build(paper=False)
    pos = fake.seed_position(entry=60_000.0)
    pos.sl_price = 57_000.0
    orch.positions.upsert(pos)
    await orch.source.start()
    await orch.source.push("sl to entry")
    await orch.run_once()
    assert fake.stops[-1][1] == 60_000.0


@pytest.mark.asyncio
async def test_bare_management_is_ignored_when_two_positions_open():
    """Ambiguous management text must not act on an arbitrary position."""
    fake, orch, *_ = build(paper=False)
    orch.positions.upsert(fake.seed_position(symbol="BTCUSDT"))
    orch.positions.upsert(fake.seed_position(symbol="ETHUSDT"))
    res = await orch.handle_management(
        __import__("src.sources.parser", fromlist=["parse_message"]).parse_message("sl to entry"))
    assert res["status"] == "NO_POSITION"


@pytest.mark.asyncio
async def test_management_full_close_books_the_trade_and_updates_shadow():
    fake, orch, gate, ledger, _ = build(paper=False)
    await orch.source.start()
    await orch.source.push(LONG, message_id=11)
    await orch.run_once()
    fake.mark = 66_000.0
    await orch.source.push("close full")
    await orch.run_once()
    closed = ledger.closed_trades()
    assert len(closed) == 1
    assert closed[0]["close_fee"] > 0            # BUG A on the exit leg too
    assert closed[0]["net_usd"] == pytest.approx(
        closed[0]["gross_usd"] - (closed[0]["open_fee"] + closed[0]["close_fee"]))
    assert ledger.outcomes_for("BTCUSDT") == [True]


@pytest.mark.asyncio
async def test_management_partial_keeps_the_position_open():
    fake, orch, gate, ledger, _ = build(paper=False)
    await orch.source.start()
    await orch.source.push(LONG, message_id=12)
    await orch.run_once()
    await orch.source.push("tp1 booked")
    await orch.run_once()
    assert len(fake.placed) == 2
    assert fake.placed[-1].reduce_only is True


@pytest.mark.asyncio
async def test_management_never_reaches_the_entry_guards():
    """Management on an open position must work even when max_concurrent is full."""
    fake, orch, *_ = build(paper=False, limits=RiskLimits(max_concurrent=1))
    pos = fake.seed_position(entry=60_000.0)
    pos.sl_price = 57_000.0
    orch.positions.upsert(pos)
    await orch.source.start()
    await orch.source.push("sl to entry")
    await orch.run_once()
    assert fake.stops


# --------------------------------------------------------------- shadow ledger
@pytest.mark.asyncio
async def test_losing_trades_flag_the_pair_and_feed_expectancy():
    fake, orch, gate, ledger, _ = build(paper=False)
    await orch.source.start()
    for mid in (20, 21):
        await orch.source.push(LONG, message_id=mid)
        await orch.run_once()
        fake.mark = 57_500.0            # a loss
        await orch.source.push("close full")
        await orch.run_once()
        orch.positions.positions.clear()
    assert orch.shadow.rating("BTCUSDT").flag is True
    assert gate.conditions.expectancy_positive is False
    assert orch.daily_realized < 0


# ---------------------------------------------------------------- stream hooks
@pytest.mark.asyncio
async def test_on_fill_marks_the_real_fee_gate_condition():
    from src.domain.model import Fill
    _, orch, gate, *_ = build()
    await orch.on_fill(Fill(client_order_id="bg-1", price=1, qty=1, side="BUY", fee=0.3))
    assert gate.conditions.real_fee_observed is True


def test_on_position_marks_the_liq_price_condition_and_tracks():
    fake, orch, gate, *_ = build()
    orch.on_position(fake.seed_position(liq=54_000.0))
    assert gate.conditions.real_liq_price_observed is True
    assert orch.positions.get("BTCUSDT") is not None


@pytest.mark.asyncio
async def test_on_tick_enforces_the_stop():
    fake, orch, *_ = build(paper=False)
    pos = fake.seed_position(entry=60_000.0)
    pos.sl_price = 57_000.0
    orch.positions.upsert(pos)
    await orch.on_tick("BTCUSDT", 56_000.0)
    assert orch.positions.get("BTCUSDT") is None


# -------------------------------------------------------------------- snapshot
@pytest.mark.asyncio
async def test_snapshot_reports_mode_positions_and_fees():
    fake, orch, gate, ledger, _ = build(paper=True)
    await orch.source.start()
    await orch.source.push(LONG, message_id=30)
    await orch.run_once()
    snap = orch.snapshot()
    assert snap["mode"] == "PAPER"
    assert snap["gate"]["conditions_met"] is False
    assert snap["fees_usd"] >= 0
    assert "flagged_pairs" in snap


@pytest.mark.asyncio
async def test_lifecycle_start_and_stop():
    fake, orch, *_ = build([LONG], paper=True)
    await orch.start()
    import asyncio
    await asyncio.sleep(0.05)
    await orch.stop()
    assert len(orch.ledger.open_trades()) == 1


# -------------------------------------------------------------------- notifier
@pytest.mark.asyncio
async def test_notifier_without_credentials_does_not_send():
    n = TelegramNotifier(token="", chat_id="")
    assert await n.send("hello") is False
    assert n.sent == ["hello"]


@pytest.mark.asyncio
async def test_notifier_posts_to_telegram_with_a_fake_client():
    class FakeResp:
        status_code = 200

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def post(self, url, json=None):
            self.calls.append((url, json))
            return FakeResp()

    client = FakeClient()
    n = TelegramNotifier(token="t", chat_id="1", client=client)
    assert await n.send("hi") is True
    assert "sendMessage" in client.calls[0][0]


def test_format_entry_and_exit():
    from src.domain.model import Instrument, OrderRequest, Side
    order = OrderRequest(instrument=Instrument.from_token("BTC"), side=Side.BUY,
                         qty=0.01, price=60_000.0, sl=57_000.0, tp=66_000.0, leverage=10)
    text = TelegramNotifier.format_entry({"status": "PAPER", "order": order})
    assert "PAPER" in text and "BTCUSDT" in text and "SL=57000.0" in text

    class Row:
        symbol = "BTCUSDT"
        exit_price = 66_000.0
        net_usd = 9.87
        open_fee = 0.06
        close_fee = 0.07

    assert "net=+9.8700" in TelegramNotifier.format_exit(Row())


def test_format_entry_for_a_rejection():
    assert "REJECTED" in TelegramNotifier.format_entry(
        {"status": "REJECTED", "reason": "SL is mandatory", "order": None})


# --------------------------------------------------------------- command router
def router(paper=True, conditions=None):
    fake = FakeExchangeAdapter()
    from src.services.order import OrderService
    from src.services.position import PositionManager
    gate = GoLiveGate(conditions=conditions or GateConditions())
    gate.paper = paper
    mgr = PositionManager(fake, OrderService(fake, paper=paper))
    return fake, gate, mgr, CommandRouter(gate, mgr, ShadowLedger(min_trades=1),
                                          TradeLedger(":memory:"), operator_id="42")


@pytest.mark.asyncio
async def test_status_is_readable_without_authorization():
    """Monitoring is harmless and must not be gated."""
    *_, r = router()
    out = await r.handle("/status", sender_id="stranger")
    assert "PAPER" in out
    assert "Go-live checklist" in out


@pytest.mark.asyncio
async def test_positions_and_pairs_are_open_reads():
    fake, gate, mgr, r = router()
    assert "No open positions" in await r.handle("/positions", sender_id="x")
    mgr.upsert(fake.seed_position(liq=54_000.0))
    assert "BTCUSDT" in await r.handle("/positions", sender_id="x")
    assert "No flagged pairs" in await r.handle("/pairs", sender_id="x")


@pytest.mark.asyncio
async def test_state_changing_commands_require_the_operator():
    *_, r = router()
    for cmd in ("/toggle", "/paper", "/live abc", "/close BTCUSDT"):
        assert "Not authorized" in await r.handle(cmd, sender_id="intruder")


@pytest.mark.asyncio
async def test_router_fails_closed_without_an_operator_id():
    fake, gate, mgr, _ = router()
    r = CommandRouter(gate, mgr, ShadowLedger(), operator_id="")
    assert "Not authorized" in await r.handle("/toggle", sender_id="42")


@pytest.mark.asyncio
async def test_toggle_to_live_is_refused_when_conditions_unmet():
    *_, r = router()
    out = await r.handle("/toggle", sender_id="42")
    assert "cannot switch to live" in out


@pytest.mark.asyncio
async def test_live_command_requires_a_token():
    *_, r = router()
    assert "Usage" in await r.handle("/live", sender_id="42")


@pytest.mark.asyncio
async def test_live_command_flips_with_a_valid_token():
    met = GateConditions(probe_green=True, expectancy_positive=True, tests_green=True,
                         real_fee_observed=True, real_liq_price_observed=True)
    fake, gate, mgr, r = router(conditions=met)
    _, token = gate.mint_token()
    out = await r.handle(f"/live {token}", sender_id="42")
    assert "LIVE mode enabled" in out
    assert gate.paper is False


@pytest.mark.asyncio
async def test_paper_command_always_works_for_the_operator():
    fake, gate, mgr, r = router(paper=False)
    gate.paper = False
    out = await r.handle("/paper", sender_id="42")
    assert gate.paper is True and "paper mode restored" in out


@pytest.mark.asyncio
async def test_close_command_closes_a_position():
    fake, gate, mgr, r = router(paper=False)
    mgr.upsert(fake.seed_position())
    out = await r.handle("/close BTCUSDT", sender_id="42")
    assert "OK" in out or "PLACED" in out
    assert mgr.get("BTCUSDT") is None


@pytest.mark.asyncio
async def test_unknown_command():
    *_, r = router()
    assert "Unknown command" in await r.handle("/nope", sender_id="42")


@pytest.mark.asyncio
async def test_empty_command_is_ignored():
    *_, r = router()
    assert await r.handle("   ", sender_id="42") == ""
