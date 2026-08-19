"""P5/P6: order service, position manager, real-time stops, fee-aware ledger."""

from __future__ import annotations

import pytest

from src.domain.model import Fill, Instrument, OrderRequest, Position, Side, Signal
from src.exchanges.fake import DEFAULT_SPEC, FakeExchangeAdapter
from src.services.order import OrderService
from src.services.position import LiquidationPriceUnknown, PositionManager
from src.services.repair import (
    IdempotencyGuard,
    RepairOrder,
    validate_order,
)
from src.services.risk import FixedRiskSizer, RiskLimits
from src.store.ledger import LiquidationBookingError, TradeLedger

BTC = Instrument.from_token("BTC")


def signal(side=Side.BUY, entry=60_000.0, sl=57_000.0, tp=66_000.0, mid=1):
    return Signal(instrument=BTC, side=side, entry=entry, sl=sl, tp=tp,
                  notional_usd=50.0, leverage=10, confidence=0.9, message_id=mid)


# ------------------------------------------------------------------- validation
def test_validate_order_accepts_a_clean_order():
    o = OrderRequest(instrument=BTC, side=Side.BUY, qty=0.001, price=60_000.0,
                     order_type="limit")
    assert validate_order(o, DEFAULT_SPEC, 60_000.0)


def test_validate_order_rejects_zero_qty():
    o = OrderRequest(instrument=BTC, side=Side.BUY, qty=0.0)
    assert validate_order(o, DEFAULT_SPEC, 60_000.0).code == "QTY_NONPOSITIVE"


def test_validate_order_rejects_below_min_qty():
    o = OrderRequest(instrument=BTC, side=Side.BUY, qty=0.00001)
    assert validate_order(o, DEFAULT_SPEC, 60_000.0).code == "QTY_TOO_SMALL"


def test_validate_order_rejects_off_step_qty():
    o = OrderRequest(instrument=BTC, side=Side.BUY, qty=0.000155)
    assert validate_order(o, DEFAULT_SPEC, 60_000.0).code == "QTY_STEP"


def test_validate_order_rejects_missing_limit_price():
    o = OrderRequest(instrument=BTC, side=Side.BUY, qty=0.001, order_type="limit")
    assert validate_order(o, DEFAULT_SPEC).code == "PRICE_MISSING"


def test_validate_order_rejects_min_notional():
    o = OrderRequest(instrument=BTC, side=Side.BUY, qty=0.0001, price=1.0,
                     order_type="limit")
    assert validate_order(o, DEFAULT_SPEC, 1.0).code == "MIN_NOTIONAL"


def test_validate_order_rejects_magnitude_flip():
    """The $0.16-coin-priced-like-$100 guard."""
    o = OrderRequest(instrument=BTC, side=Side.BUY, qty=1.0, price=100.0,
                     order_type="limit")
    assert validate_order(o, DEFAULT_SPEC, 0.16).code == "MAGNITUDE_FLIP"


def test_validate_order_rejects_excess_leverage():
    o = OrderRequest(instrument=BTC, side=Side.BUY, qty=0.001, price=60_000.0,
                     leverage=999)
    assert validate_order(o, DEFAULT_SPEC, 60_000.0).code == "LEVERAGE"


def test_validation_result_is_falsy_on_failure():
    assert not validate_order(OrderRequest(instrument=BTC, side=Side.BUY, qty=0),
                              DEFAULT_SPEC)


# ------------------------------------------------------------------ idempotency
def test_idempotency_guard_blocks_the_second_identical_decision():
    guard = IdempotencyGuard()
    assert guard.check("msg-1", Side.BUY, "oid-1").skipped is False
    dup = guard.check("msg-1", Side.BUY, "oid-2")
    assert dup.skipped is True
    assert dup.status == "DUPLICATE_SKIPPED"
    assert dup.existing_client_order_id == "oid-1"


def test_idempotency_guard_allows_the_opposite_side():
    guard = IdempotencyGuard()
    guard.check("msg-1", Side.BUY, "oid-1")
    assert guard.check("msg-1", Side.SELL, "oid-2").skipped is False


def test_idempotency_guard_allows_a_different_decision():
    guard = IdempotencyGuard()
    guard.check("msg-1", Side.BUY, "oid-1")
    assert guard.check("msg-2", Side.BUY, "oid-2").skipped is False


# ----------------------------------------------------------------------- repair
def test_repair_raises_qty_to_meet_min_notional():
    o = OrderRequest(instrument=BTC, side=Side.BUY, qty=0.00001, price=60_000.0,
                     order_type="limit")
    fixed = RepairOrder().repair(o, DEFAULT_SPEC, 60_000.0)
    assert validate_order(fixed, DEFAULT_SPEC, 60_000.0)
    assert fixed.qty * 60_000.0 >= DEFAULT_SPEC.min_notional


def test_repair_preserves_reduce_only_and_does_not_upsize_it():
    """A protective order must never be inflated to satisfy min notional."""
    o = OrderRequest(instrument=BTC, side=Side.SELL, qty=0.0002, price=60_000.0,
                     order_type="limit", reduce_only=True)
    fixed = RepairOrder().repair(o, DEFAULT_SPEC, 60_000.0)
    assert fixed.reduce_only is True
    assert fixed.qty <= o.qty + DEFAULT_SPEC.qty_step


def test_repair_clamps_leverage_to_pair_max():
    o = OrderRequest(instrument=BTC, side=Side.BUY, qty=0.001, price=60_000.0, leverage=999)
    assert RepairOrder().repair(o, DEFAULT_SPEC, 60_000.0).leverage == DEFAULT_SPEC.max_leverage


def test_repair_rounds_limit_price_to_tick():
    o = OrderRequest(instrument=BTC, side=Side.BUY, qty=0.001, price=60_000.17,
                     order_type="limit")
    assert RepairOrder().repair(o, DEFAULT_SPEC, 60_000.0).price == pytest.approx(60_000.1)


def test_repair_is_single_shot_by_contract():
    assert RepairOrder.MAX_ATTEMPTS == 1


def test_repair_never_imports_an_llm():
    """Assert on CODE, not prose: no llm import/call in the repair module's AST."""
    import ast
    import inspect

    import src.services.repair as mod
    tree = ast.parse(inspect.getsource(mod))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.lower() for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").lower())
        elif isinstance(node, ast.Attribute):
            names.add(node.attr.lower())
        elif isinstance(node, ast.Name):
            names.add(node.id.lower())
    assert not any("llm" in n or "openai" in n or "anthropic" in n for n in names)


# -------------------------------------------------------------------- risk sizer
def test_sizer_accounts_for_both_fees():
    sizer = FixedRiskSizer(RiskLimits(risk_per_trade_usd=5.0, max_position_usd=10_000))
    res = sizer.size_for_signal(60_000.0, 57_000.0, DEFAULT_SPEC, 10)
    assert res.ok
    assert res.open_fee > 0 and res.close_fee > 0
    # a stop-out should cost about the risk budget INCLUDING fees
    loss = res.qty * abs(60_000.0 - 57_000.0) + res.total_fees
    assert loss == pytest.approx(5.0, rel=0.02)


def test_sizer_ignoring_fees_would_oversize():
    """Sanity: the fee term genuinely reduces size (guards against a silent revert)."""
    sizer = FixedRiskSizer(RiskLimits(risk_per_trade_usd=5.0, max_position_usd=10_000))
    with_fees = sizer.size_for_signal(60_000.0, 57_000.0, DEFAULT_SPEC, 10).qty
    naive = 5.0 / abs(60_000.0 - 57_000.0)
    assert with_fees < naive


def test_sizer_clamps_to_max_position_usd():
    sizer = FixedRiskSizer(RiskLimits(risk_per_trade_usd=500, max_position_usd=100))
    res = sizer.size_for_signal(60_000.0, 59_999.0, DEFAULT_SPEC, 10)
    assert res.notional_usd <= 100.0 + 1e-6


def test_sizer_raises_to_min_notional():
    sizer = FixedRiskSizer(RiskLimits(risk_per_trade_usd=0.01, max_position_usd=100))
    res = sizer.size_for_signal(60_000.0, 30_000.0, DEFAULT_SPEC, 10)
    assert res.notional_usd >= DEFAULT_SPEC.min_notional


def test_sizer_without_sl_falls_back_to_notional_cap():
    sizer = FixedRiskSizer(RiskLimits(risk_per_trade_usd=5, max_position_usd=50))
    res = sizer.size_for_signal(60_000.0, None, DEFAULT_SPEC, 10)
    assert res.notional_usd <= 50.0 + 1e-6
    assert "no SL" in res.reason


def test_sizer_leverage_is_double_capped():
    sizer = FixedRiskSizer(RiskLimits(max_leverage=20))
    assert sizer.size_for_signal(60_000.0, 57_000.0, DEFAULT_SPEC, 999).leverage == 20


def test_sizer_rejects_invalid_entry():
    assert not FixedRiskSizer().size_for_signal(0.0, 1.0, DEFAULT_SPEC).ok


def test_net_pnl_definition():
    assert FixedRiskSizer.net_pnl(10.0, 0.5, 0.5) == 9.0


def test_expectancy_requires_positive_mean():
    sizer = FixedRiskSizer()
    assert sizer.expectancy_ok([1.0, -0.5, 0.8]) is True
    assert sizer.expectancy_ok([-1.0, 0.5]) is False
    assert sizer.expectancy_ok([]) is False


def test_compute_size_port_method():
    qty, lev = FixedRiskSizer().compute_size(100.0, 50.0, 10)
    assert qty == 2.0 and lev == 10


# -------------------------------------------------------------------- order flow
@pytest.mark.asyncio
async def test_open_from_signal_refuses_without_sl():
    svc = OrderService(FakeExchangeAdapter(), paper=True)
    res = await svc.open_from_signal(signal(sl=None))
    assert res["status"] == "REJECTED"
    assert "SL is mandatory" in res["reason"]


@pytest.mark.asyncio
async def test_open_from_signal_rejects_long_with_sl_above_entry():
    svc = OrderService(FakeExchangeAdapter(), paper=True)
    res = await svc.open_from_signal(signal(side=Side.BUY, entry=60_000, sl=61_000))
    assert res["status"] == "REJECTED"


@pytest.mark.asyncio
async def test_open_from_signal_rejects_short_with_sl_below_entry():
    svc = OrderService(FakeExchangeAdapter(), paper=True)
    res = await svc.open_from_signal(signal(side=Side.SELL, entry=60_000, sl=59_000))
    assert res["status"] == "REJECTED"


@pytest.mark.asyncio
async def test_open_from_signal_paper_does_not_place():
    fake = FakeExchangeAdapter()
    svc = OrderService(fake, paper=True)
    res = await svc.open_from_signal(signal())
    assert res["status"] == "PAPER"
    assert fake.placed == []


@pytest.mark.asyncio
async def test_open_from_signal_live_places_and_sets_leverage():
    fake = FakeExchangeAdapter()
    svc = OrderService(fake, paper=False)
    res = await svc.open_from_signal(signal())
    assert res["status"] == "PLACED"
    assert len(fake.placed) == 1
    assert fake.leverage_calls           # leverage set before the order
    assert fake.placed[0].sl == 57_000.0


@pytest.mark.asyncio
async def test_open_from_signal_dedupes_the_same_message():
    fake = FakeExchangeAdapter()
    svc = OrderService(fake, paper=False)
    await svc.open_from_signal(signal(mid=42))
    second = await svc.open_from_signal(signal(mid=42))
    assert second["status"] == "DUPLICATE_SKIPPED"
    assert len(fake.placed) == 1


@pytest.mark.asyncio
async def test_open_from_signal_applies_size_multiplier():
    fake = FakeExchangeAdapter()
    svc = OrderService(fake, paper=False)
    full = await svc.open_from_signal(signal(mid=1))
    half = await svc.open_from_signal(signal(mid=2), size_multiplier=0.5)
    assert half["order"].qty < full["order"].qty


@pytest.mark.asyncio
async def test_close_position_is_reduce_only_and_opposite_side():
    fake = FakeExchangeAdapter()
    svc = OrderService(fake, paper=False)
    pos = fake.seed_position(side=Side.BUY, size=0.01)
    res = await svc.close_position(pos)
    assert res["order"].reduce_only is True
    assert res["order"].side is Side.SELL


@pytest.mark.asyncio
async def test_close_position_partial_fraction():
    fake = FakeExchangeAdapter()
    svc = OrderService(fake, paper=False)
    pos = fake.seed_position(size=0.01)
    res = await svc.close_position(pos, 0.5)
    assert res["order"].qty == pytest.approx(0.005, rel=0.02)


@pytest.mark.asyncio
async def test_close_position_zero_size_is_noop():
    fake = FakeExchangeAdapter()
    svc = OrderService(fake, paper=False)
    pos = fake.seed_position(size=0.01)
    assert (await svc.close_position(pos, 0.0))["status"] == "NOOP"


# --------------------------------------------------------------- position manager
def manager(paper=False):
    fake = FakeExchangeAdapter()
    return fake, PositionManager(fake, OrderService(fake, paper=paper))


@pytest.mark.asyncio
async def test_sl_to_entry_moves_stop_to_breakeven():
    fake, mgr = manager()
    pos = fake.seed_position(side=Side.BUY, entry=60_000.0)
    pos.sl_price = 57_000.0
    mgr.upsert(pos)
    res = await mgr.sl_to_entry("BTCUSDT")
    assert res["sl"] == 60_000.0
    assert fake.stops[-1][1] == 60_000.0


@pytest.mark.asyncio
async def test_sl_to_entry_refuses_to_widen_the_stop():
    fake, mgr = manager()
    pos = fake.seed_position(side=Side.BUY, entry=60_000.0)
    pos.sl_price = 61_000.0     # already tighter than entry
    mgr.upsert(pos)
    res = await mgr.sl_to_entry("BTCUSDT")
    assert res["status"] == "REJECTED"


@pytest.mark.asyncio
async def test_management_on_missing_position():
    _, mgr = manager()
    for coro in (mgr.sl_to_entry("BTCUSDT"), mgr.book_partial("BTCUSDT"),
                 mgr.full_close("BTCUSDT"), mgr.modify_stop("BTCUSDT", 1.0)):
        assert (await coro)["status"] == "NO_POSITION"


@pytest.mark.asyncio
async def test_book_partial_keeps_the_remainder():
    fake, mgr = manager()
    mgr.upsert(fake.seed_position(size=0.01))
    res = await mgr.book_partial("BTCUSDT", 0.5)
    assert res["remaining"] == pytest.approx(0.005)
    assert mgr.get("BTCUSDT") is not None


@pytest.mark.asyncio
async def test_book_partial_full_fraction_clears_position():
    fake, mgr = manager()
    mgr.upsert(fake.seed_position(size=0.01))
    await mgr.book_partial("BTCUSDT", 1.0)
    assert mgr.get("BTCUSDT") is None


@pytest.mark.asyncio
async def test_full_close_clears_tracking():
    fake, mgr = manager()
    mgr.upsert(fake.seed_position())
    await mgr.full_close("BTCUSDT")
    assert mgr.get("BTCUSDT") is None


@pytest.mark.asyncio
async def test_modify_stop_updates_tracked_levels():
    fake, mgr = manager()
    mgr.upsert(fake.seed_position())
    await mgr.modify_stop("BTCUSDT", sl=58_000.0, tp=70_000.0)
    pos = mgr.get("BTCUSDT")
    assert pos.sl_price == 58_000.0 and pos.tp_price == 70_000.0


@pytest.mark.asyncio
async def test_cancel_pending_preserves_protective_orders():
    """BUG C at the management layer."""
    fake, mgr = manager()
    mgr.upsert(fake.seed_position())
    fake.open_orders = [
        {"orderId": "e1", "symbol": "BTCUSDT", "clientOid": "c1", "reduceOnly": "NO"},
        {"orderId": "sl1", "symbol": "BTCUSDT", "clientOid": "c2", "planType": "loss_plan"},
    ]
    res = await mgr.cancel_pending("BTCUSDT")
    assert res["cancelled"] == ["e1"]
    assert res["preserved_protective"] == ["sl1"]


@pytest.mark.asyncio
async def test_upsert_removes_a_flat_position():
    _, mgr = manager()
    pos = Position(instrument=BTC, side=Side.BUY, size=0.0, entry_price=1.0)
    mgr.upsert(pos)
    assert mgr.get("BTCUSDT") is None


@pytest.mark.asyncio
async def test_sync_reads_positions_from_the_adapter():
    fake, mgr = manager()
    fake.seed_position()
    await mgr.sync()
    assert mgr.get("BTCUSDT") is not None


# ------------------------------------------------------------- real-time stops
def test_stop_breached_long_and_short():
    _, mgr = manager()
    long_pos = Position(instrument=BTC, side=Side.BUY, size=1, entry_price=60_000,
                        sl_price=57_000)
    short_pos = Position(instrument=BTC, side=Side.SELL, size=1, entry_price=60_000,
                         sl_price=63_000)
    assert mgr.stop_breached(long_pos, 56_999) is True
    assert mgr.stop_breached(long_pos, 57_001) is False
    assert mgr.stop_breached(short_pos, 63_001) is True
    assert mgr.stop_breached(short_pos, 62_999) is False


def test_stop_breached_is_false_without_a_stop():
    _, mgr = manager()
    assert mgr.stop_breached(Position(instrument=BTC, side=Side.BUY, size=1,
                                      entry_price=1.0), 0.0001) is False


@pytest.mark.asyncio
async def test_on_mark_closes_a_breached_position():
    fake, mgr = manager()
    pos = fake.seed_position(side=Side.BUY, entry=60_000.0)
    pos.sl_price = 57_000.0
    mgr.upsert(pos)
    res = await mgr.on_mark("BTCUSDT", 56_500.0)
    assert res is not None
    assert mgr.get("BTCUSDT") is None


@pytest.mark.asyncio
async def test_on_mark_noop_when_stop_intact():
    fake, mgr = manager()
    pos = fake.seed_position(side=Side.BUY, entry=60_000.0)
    pos.sl_price = 57_000.0
    mgr.upsert(pos)
    assert await mgr.on_mark("BTCUSDT", 59_000.0) is None


@pytest.mark.asyncio
async def test_on_mark_closes_when_tp_hit_long():
    """TP monitoring: a long is closed when mark rises to/above its target."""
    fake, mgr = manager()
    pos = fake.seed_position(side=Side.BUY, entry=60_000.0)
    pos.tp_price = 66_000.0
    mgr.upsert(pos)
    res = await mgr.on_mark("BTCUSDT", 66_100.0)
    assert res is not None and res.get("action") == "full_close"
    assert mgr.get("BTCUSDT") is None


@pytest.mark.asyncio
async def test_on_mark_noop_when_tp_not_hit():
    fake, mgr = manager()
    pos = fake.seed_position(side=Side.BUY, entry=60_000.0)
    pos.tp_price = 66_000.0
    mgr.upsert(pos)
    assert await mgr.on_mark("BTCUSDT", 65_000.0) is None


@pytest.mark.asyncio
async def test_modify_stop_arms_monitor_even_if_venue_attach_fails():
    """When the exchange tpsl endpoint is unavailable the bot-side SL/TP must
    still be set so the WS monitor can enforce the close."""
    fake, mgr = manager()
    mgr.upsert(fake.seed_position())
    fake.fail_next_code = "43011"   # simulate broken place-tpsl-order
    res = await mgr.modify_stop("BTCUSDT", sl=58_000.0, tp=70_000.0)
    pos = mgr.get("BTCUSDT")
    assert pos.sl_price == 58_000.0 and pos.tp_price == 70_000.0
    assert res["status"] is not None


# ------------------------------------------------------------- liquidation (B)
def test_book_liquidation_uses_the_real_exchange_price():
    _, mgr = manager()
    pos = Position(instrument=BTC, side=Side.BUY, size=1, entry_price=60_000,
                   liq_price=54_321.0)
    booked = mgr.book_liquidation(pos)
    assert booked["exit_price"] == 54_321.0
    assert booked["estimated"] is False


def test_book_liquidation_falls_back_to_mark_and_flags_estimate():
    _, mgr = manager()
    pos = Position(instrument=BTC, side=Side.BUY, size=1, entry_price=60_000)
    booked = mgr.book_liquidation(pos, last_mark=54_000.0)
    assert booked["exit_price"] == 54_000.0
    assert booked["estimated"] is True


def test_book_liquidation_refuses_to_invent_zero():
    """BUG B: never book 0.00."""
    _, mgr = manager()
    pos = Position(instrument=BTC, side=Side.BUY, size=1, entry_price=60_000, liq_price=0.0)
    with pytest.raises(LiquidationPriceUnknown):
        mgr.book_liquidation(pos, last_mark=None)


def test_assert_fee_recorded_flags_zero_fee(caplog):
    """BUG A tripwire."""
    _, mgr = manager()
    mgr.assert_fee_recorded(Fill(client_order_id="real-1", price=1, qty=1, side="BUY", fee=0.0))
    assert any("BUG A" in r.message for r in caplog.records)


def test_assert_fee_recorded_silent_for_paper():
    _, mgr = manager()
    mgr.assert_fee_recorded(Fill(client_order_id="paper-1", price=1, qty=1,
                                 side="BUY", fee=0.0))


# ---------------------------------------------------------------------- ledger
def ledger():
    return TradeLedger(":memory:")


def test_ledger_records_open_with_fee():
    led = ledger()
    tid = led.record_open("BTCUSDT", "BUY", 0.01, 60_000.0, open_fee=0.36, leverage=10)
    row = led.get(tid)
    assert row["open_fee"] == 0.36
    assert row["paper"] == 1


def test_ledger_rejects_missing_open_fee():
    with pytest.raises(ValueError):
        ledger().record_open("BTCUSDT", "BUY", 0.01, 60_000.0, open_fee=None)


def test_ledger_net_is_gross_minus_both_fees():
    led = ledger()
    tid = led.record_open("BTCUSDT", "BUY", 1.0, 100.0, open_fee=0.06)
    row = led.record_close(tid, exit_price=110.0, close_fee=0.066, exit_reason="tp")
    assert row.net_usd == pytest.approx(10.0 - (0.06 + 0.066))
    stored = led.get(tid)
    assert stored["gross_usd"] == pytest.approx(10.0)
    assert stored["net_usd"] == pytest.approx(9.874)


def test_ledger_short_pnl_direction():
    led = ledger()
    tid = led.record_open("BTCUSDT", "SELL", 1.0, 100.0, open_fee=0.0)
    row = led.record_close(tid, exit_price=90.0, close_fee=0.0)
    assert row.net_usd == pytest.approx(10.0)


def test_ledger_rejects_missing_close_fee():
    led = ledger()
    tid = led.record_open("BTCUSDT", "BUY", 1.0, 100.0, open_fee=0.0)
    with pytest.raises(ValueError):
        led.record_close(tid, exit_price=110.0, close_fee=None)


def test_ledger_refuses_zero_price_liquidation():
    """BUG B at the storage boundary."""
    led = ledger()
    tid = led.record_open("BTCUSDT", "BUY", 1.0, 100.0, open_fee=0.06)
    with pytest.raises(LiquidationBookingError):
        led.record_close(tid, exit_price=0.0, close_fee=0.06, exit_reason="liquidation")


def test_ledger_accepts_real_liquidation_price():
    led = ledger()
    tid = led.record_open("BTCUSDT", "BUY", 1.0, 100.0, open_fee=0.06)
    row = led.record_close(tid, exit_price=54.0, close_fee=0.06, exit_reason="liquidation")
    assert row.exit_price == 54.0


def test_ledger_open_and_closed_views():
    led = ledger()
    t1 = led.record_open("BTCUSDT", "BUY", 1.0, 100.0, open_fee=0.0)
    led.record_open("ETHUSDT", "BUY", 1.0, 100.0, open_fee=0.0)
    led.record_close(t1, exit_price=110.0, close_fee=0.0)
    assert len(led.open_trades()) == 1
    assert len(led.closed_trades()) == 1


def test_ledger_total_fees_and_net_pnls():
    led = ledger()
    tid = led.record_open("BTCUSDT", "BUY", 1.0, 100.0, open_fee=0.5)
    led.record_close(tid, exit_price=110.0, close_fee=0.25)
    assert led.total_fees() == pytest.approx(0.75)
    assert led.net_pnls() == [pytest.approx(9.25)]


def test_ledger_records_pair_outcome():
    led = ledger()
    tid = led.record_open("BTCUSDT", "BUY", 1.0, 100.0, open_fee=0.0)
    led.record_close(tid, exit_price=110.0, close_fee=0.0)
    assert led.outcomes_for("BTCUSDT") == [True]


def test_ledger_unknown_trade_raises():
    with pytest.raises(KeyError):
        ledger().record_close(999, exit_price=1.0, close_fee=0.0)


def test_ledger_client_order_id_is_unique():
    import sqlite3
    led = ledger()
    led.record_open("BTCUSDT", "BUY", 1.0, 100.0, open_fee=0.0, client_order_id="bg-1")
    with pytest.raises(sqlite3.IntegrityError):
        led.record_open("BTCUSDT", "BUY", 1.0, 100.0, open_fee=0.0, client_order_id="bg-1")
