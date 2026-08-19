"""P13: the operator command surface — menu, read-only reports, manual open/close.

Zero network. Every command is exercised against fakes so the reply text and the
authorization boundary are pinned. The bot runs on REAL money, so the tests that
matter most here are the refusals: unauthorized callers, a missing stop-loss, and
a stop on the wrong side of the market.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.domain.model import Instrument, Position, Side
from src.notifier import bot_poller, fmt
from src.notifier.telegram import CommandRouter

OPERATOR = "5894116684"
STRANGER = "999999"


def _pos(symbol="XRPUSDT", side=Side.BUY, size=2.0, entry=2.0,
         sl=1.9, tp=2.4, liq: float | None = 1.5, upnl=0.0):
    return Position(instrument=Instrument.from_token(symbol.replace("USDT", "")),
                    side=side, size=size, entry_price=entry,
                    sl_price=sl, tp_price=tp, liq_price=liq,
                    unrealized_pnl=upnl)


class _Adapter:
    product_type = "USDT-FUTURES"
    margin_coin = "USDT"

    def __init__(self, balance=8.879943, mark=2.1, fail_balance=False):
        self._balance = balance
        self._mark = mark
        self._fail_balance = fail_balance
        self.rate_limit = SimpleNamespace(is_open=lambda: False)

    async def get_balance_usdt(self):
        if self._fail_balance:
            raise RuntimeError("NO_CREDS")
        return self._balance

    async def get_mark_price(self, instrument):
        return self._mark


class _Positions:
    def __init__(self, positions=None, adapter=None):
        self.positions = positions or {}
        self.adapter = adapter or _Adapter()
        self.stream = None
        self.closed: list[tuple[str, str]] = []

    async def full_close(self, symbol, reason="full"):
        self.closed.append((symbol, reason))
        return {"status": "CLOSED"}

    async def sl_to_entry(self, symbol):
        return f"{symbol}: sl moved to entry"


class _Ledger:
    def __init__(self, trades=None):
        self._trades = trades or []

    def net_pnls(self):
        return [t["net_usd"] for t in self._trades if t.get("net_usd") is not None]

    def total_fees(self):
        return sum(t.get("fees_usd", 0.0) for t in self._trades)

    def closed_trades(self, limit=200):
        return self._trades[-limit:]


def _router(positions=None, ledger=None, orch=None, adapter=None):
    adapter = adapter or _Adapter()
    pm = positions if positions is not None else _Positions(adapter=adapter)
    gate = SimpleNamespace(
        status=lambda: {"mode": "LIVE", "paper": False, "unmet": [], "conditions_met": True},
        toggle=lambda who: "toggled",
        to_paper=lambda who: "forced PAPER",
        redeem=lambda tok: (True, "redeemed"),
    )
    return CommandRouter(gate=gate, position_manager=pm,
                         shadow_ledger=SimpleNamespace(weak_pairs=lambda: []),
                         ledger=ledger, operator_id=OPERATOR, orchestrator=orch)


def _orch(daily=0.0, params=None, orders=None):
    return SimpleNamespace(
        daily_realized=daily,
        params=params if params is not None else {"risk_per_trade_usd": 5.0,
                                                 "max_daily_loss_pct": 30.0},
        daily_loss_breached=lambda: False,
        deadman_halted=False,
        orders=orders,
    )


# ------------------------------------------------------------------ the "/" menu
async def test_bare_slash_lists_every_command():
    r = _router()
    out = await r.handle("/", OPERATOR)
    for name, _args, _desc, _sc, _g in CommandRouter.COMMANDS:
        assert f"/{name}" in out, f"{name} missing from the menu"


@pytest.mark.parametrize("alias", ["/help", "/start", "/commands", "/menu"])
async def test_help_aliases_all_show_the_menu(alias):
    out = await _router().handle(alias, OPERATOR)
    assert "/positions" in out and "/close" in out


async def test_menu_separates_readonly_from_operator_commands():
    out = await _router().handle("/help", OPERATOR)
    for group in ("Monitor", "Trade", "Control"):
        assert group in out


async def test_menu_warns_a_non_operator():
    out = await _router().handle("/help", STRANGER)
    assert "not the configured operator" in out.lower() or "not the configured" in out


async def test_group_style_mention_is_stripped():
    """Telegram sends '/status@mybot' in groups."""
    out = await _router().handle("/status@nibiru_valarion_bot", OPERATOR)
    assert "LIVE" in out and "Status" in out


async def test_unknown_command_points_at_help():
    out = await _router().handle("/frobnicate", OPERATOR)
    assert "Unknown command" in out and "/help" in out


# --------------------------------------------------------------------- read-only
async def test_balance_reports_available_and_product():
    out = await _router().handle("/balance", OPERATOR)
    assert "8.8799" in out and "USDT-FUTURES" in out


async def test_balance_degrades_gracefully_when_the_venue_errors():
    r = _router(adapter=_Adapter(fail_balance=True))
    r.positions.adapter = _Adapter(fail_balance=True)
    out = await r.handle("/balance", OPERATOR)
    assert "Balance unavailable" in out and "NO_CREDS" in out


async def test_positions_shows_unrealized_pnl_and_liquidation():
    pm = _Positions({"XRPUSDT": _pos(upnl=0.25)}, adapter=_Adapter())
    out = await _router(positions=pm).handle("/positions", OPERATOR)
    assert "XRPUSDT" in out and "+0.2500" in out and "1.5" in out


async def test_positions_derives_upnl_from_mark_when_venue_reports_none():
    """entry 2.0, mark 2.1, size 2 BUY -> +0.20. Never fabricated: derived."""
    pm = _Positions({"XRPUSDT": _pos(entry=2.0, size=2.0, upnl=0.0)},
                    adapter=_Adapter(mark=2.1))
    out = await _router(positions=pm).handle("/positions", OPERATOR)
    assert "+0.2000" in out


async def test_positions_never_prints_a_fake_zero_liquidation():
    """BUG B: liq must read n/a when absent, never 0.00."""
    pm = _Positions({"XRPUSDT": _pos(liq=None)}, adapter=_Adapter())
    out = await _router(positions=pm).handle("/positions", OPERATOR)
    assert fmt.DASH in out and "0.00" not in out.replace("+0.0000", "")


async def test_positions_percentage_never_contradicts_the_pnl_sign():
    """A green PnL must never be printed beside a negative percentage."""
    pm = _Positions({"XRPUSDT": _pos(entry=2.8412, size=17.0, upnl=0.3417),
                     "DOGEUSDT": _pos("DOGEUSDT", side=Side.SELL, entry=0.20841,
                                      size=240.0, upnl=-0.1128)},
                    adapter=_Adapter(mark=1.0))
    out = await _router(positions=pm).handle("/positions", OPERATOR)
    for card in out.split("🟢")[1:] + out.split("🔴")[1:]:
        line = [ln for ln in card.splitlines() if "PnL" in ln]
        if not line:
            continue
        pnl_neg = "-" in line[0].split("USDT")[0]
        pct_part = line[0].split("(")[-1]
        pct_neg = pct_part.startswith("-")
        assert pnl_neg == pct_neg, f"sign disagreement: {line[0]}"


async def test_positions_percentage_is_return_on_notional():
    """+0.3417 on 17 x 2.8412 = 48.30 notional -> +0.71%."""
    pm = _Positions({"XRPUSDT": _pos(entry=2.8412, size=17.0, upnl=0.3417)},
                    adapter=_Adapter(mark=2.86))
    out = await _router(positions=pm).handle("/positions", OPERATOR)
    assert "+0.71%" in out


async def test_positions_empty():
    assert "No open positions" in await _router().handle("/positions", OPERATOR)


async def test_pnl_reports_unrealized_daily_and_lifetime():
    led = _Ledger([{"net_usd": 1.5, "fees_usd": 0.01},
                   {"net_usd": -0.5, "fees_usd": 0.01}])
    pm = _Positions({"XRPUSDT": _pos(upnl=0.25)}, adapter=_Adapter())
    r = _router(positions=pm, ledger=led, orch=_orch(daily=-0.5))
    out = await r.handle("/pnl", OPERATOR)
    assert "+0.2500" in out            # unrealized
    assert "-0.5000" in out            # daily realized
    assert "+1.0000" in out            # lifetime net
    assert "Trades" in out and "2" in out


async def test_pnl_flags_a_breached_daily_cap():
    orch = _orch(daily=-3.0)
    orch.daily_loss_breached = lambda: True
    out = await _router(ledger=_Ledger(), orch=orch).handle("/pnl", OPERATOR)
    assert "Daily loss cap reached" in out


async def test_activity_lists_recent_trades_newest_first():
    led = _Ledger([{"symbol": "ADAUSDT", "net_usd": -0.2, "fees_usd": 0.01},
                   {"symbol": "XRPUSDT", "net_usd": 1.0, "fees_usd": 0.02}])
    out = await _router(ledger=led).handle("/activity", OPERATOR)
    assert out.index("XRPUSDT") < out.index("ADAUSDT")
    assert "🟢" in out and "🔴" in out


async def test_activity_respects_a_count_argument():
    led = _Ledger([{"symbol": f"C{i}USDT", "net_usd": 0.1} for i in range(10)])
    out = await _router(ledger=led).handle("/activity 3", OPERATOR)
    assert "Recent activity" in out and "3" in out


async def test_activity_empty_ledger():
    assert "No closed trades" in await _router(ledger=_Ledger()).handle(
        "/activity", OPERATOR)


async def test_health_reports_all_four_liveness_signals():
    bot_poller.tg_cmd_conflict = False
    bot_poller.tg_cmd_consecutive_errors = 0
    out = await _router(orch=_orch()).handle("/health", OPERATOR)
    assert "Healthy" in out
    for field in ("Mode", "Trading", "Rate limit", "Commands"):
        assert field in out


async def test_health_turns_degraded_when_the_command_channel_is_down():
    bot_poller.tg_cmd_conflict = True
    try:
        out = await _router(orch=_orch()).handle("/health", OPERATOR)
        assert "Degraded" in out
    finally:
        bot_poller.tg_cmd_conflict = False


async def test_status_includes_daily_realized():
    out = await _router(ledger=_Ledger(), orch=_orch(daily=-1.25)).handle(
        "/status", OPERATOR)
    assert "-1.2500" in out


# ------------------------------------------------------- authorization boundary
@pytest.mark.parametrize("cmd", [
    "/open XRPUSDT BUY 1.9", "/close XRPUSDT", "/close all",
    "/toggle", "/paper", "/live tok", "/sl_to_entry XRPUSDT",
])
async def test_state_changing_commands_refuse_a_stranger(cmd):
    out = await _router(orch=_orch()).handle(cmd, STRANGER)
    assert "Not authorized" in out


@pytest.mark.parametrize("cmd", ["/status", "/health", "/balance", "/positions",
                                 "/pnl", "/activity", "/pairs", "/help"])
async def test_read_only_commands_are_open(cmd):
    out = await _router(ledger=_Ledger(), orch=_orch()).handle(cmd, STRANGER)
    assert "Not authorized" not in out


async def test_router_fails_closed_with_no_operator_configured(monkeypatch):
    monkeypatch.delenv("TG_OPERATOR_ID", raising=False)
    r = _router()
    r.operator_id = ""
    assert r.authorized(OPERATOR) is False


# ------------------------------------------------------------------ manual close
async def test_close_all_closes_every_open_position():
    pm = _Positions({"XRPUSDT": _pos(), "ADAUSDT": _pos("ADAUSDT")},
                    adapter=_Adapter())
    out = await _router(positions=pm).handle("/close all", OPERATOR)
    assert {s for s, _ in pm.closed} == {"XRPUSDT", "ADAUSDT"}
    assert out.count("CLOSED") == 2 and "2/2" in out


async def test_close_all_with_nothing_open():
    out = await _router().handle("/close all", OPERATOR)
    assert "Nothing to close" in out


async def test_close_one_symbol():
    pm = _Positions({"XRPUSDT": _pos()}, adapter=_Adapter())
    out = await _router(positions=pm).handle("/close xrpusdt", OPERATOR)
    assert pm.closed == [("XRPUSDT", "telegram")]
    assert "CLOSED" in out


async def test_close_reports_a_failure_without_aborting_the_rest():
    pm = _Positions({"A1USDT": _pos("A1USDT"), "B2USDT": _pos("B2USDT")},
                    adapter=_Adapter())

    async def flaky(symbol, reason="full"):
        if symbol == "A1USDT":
            raise RuntimeError("venue rejected")
        return {"status": "CLOSED"}

    pm.full_close = flaky
    out = await _router(positions=pm).handle("/close all", OPERATOR)
    assert "venue rejected" in out and "1/2" in out
    assert "CLOSED" in out          # the second one still went through


# ------------------------------------------------------------------- manual open
class _Orders:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"status": "FILLED", "size": 3.0, "order_id": "oid1"}

    async def open_from_signal(self, signal, mark_price=None, size_multiplier=1.0):
        self.calls.append(signal)
        return self.result


async def test_open_places_an_order_with_the_parsed_arguments():
    orders = _Orders()
    r = _router(orch=_orch(orders=orders), adapter=_Adapter(mark=2.1))
    r.positions.adapter = _Adapter(mark=2.1)
    out = await r.handle("/open XRPUSDT BUY 1.90 2.50 6", OPERATOR)
    assert len(orders.calls) == 1
    sig = orders.calls[0]
    assert sig.instrument.symbol == "XRPUSDT"
    assert sig.side is Side.BUY
    assert sig.sl == 1.90 and sig.tp == 2.50
    assert sig.notional_usd == 6.0
    assert sig.source == "telegram-manual"
    assert "Position opened" in out


async def test_open_defaults_notional_to_the_configured_risk():
    orders = _Orders()
    r = _router(orch=_orch(params={"risk_per_trade_usd": 5.0}, orders=orders))
    r.positions.adapter = _Adapter(mark=2.1)
    await r.handle("/open XRPUSDT BUY 1.90", OPERATOR)
    assert orders.calls[0].notional_usd == 5.0


async def test_open_appends_usdt_to_a_bare_token():
    orders = _Orders()
    r = _router(orch=_orch(orders=orders))
    r.positions.adapter = _Adapter(mark=2.1)
    await r.handle("/open xrp BUY 1.90", OPERATOR)
    assert orders.calls[0].instrument.symbol == "XRPUSDT"


async def test_open_without_a_stop_loss_shows_usage_and_places_nothing():
    orders = _Orders()
    r = _router(orch=_orch(orders=orders))
    out = await r.handle("/open XRPUSDT BUY", OPERATOR)
    assert "Usage" in out and "mandatory" in out
    assert orders.calls == []


async def test_open_refuses_a_buy_stop_above_the_mark():
    """A BUY with sl >= mark is an instant-liquidation, not a trade."""
    orders = _Orders()
    r = _router(orch=_orch(orders=orders))
    r.positions.adapter = _Adapter(mark=2.0)
    out = await r.handle("/open XRPUSDT BUY 2.50", OPERATOR)
    assert "Entry refused" in out
    assert orders.calls == []


async def test_open_refuses_a_sell_stop_below_the_mark():
    orders = _Orders()
    r = _router(orch=_orch(orders=orders))
    r.positions.adapter = _Adapter(mark=2.0)
    out = await r.handle("/open XRPUSDT SELL 1.50", OPERATOR)
    assert "Entry refused" in out
    assert orders.calls == []


async def test_open_accepts_long_short_aliases():
    orders = _Orders()
    r = _router(orch=_orch(orders=orders))
    r.positions.adapter = _Adapter(mark=2.0)
    await r.handle("/open XRPUSDT SHORT 2.50", OPERATOR)
    assert orders.calls[0].side is Side.SELL


async def test_open_rejects_a_bad_side():
    out = await _router(orch=_orch(orders=_Orders())).handle(
        "/open XRPUSDT SIDEWAYS 1.9", OPERATOR)
    assert "Invalid side" in out


async def test_open_rejects_non_numeric_prices():
    r = _router(orch=_orch(orders=_Orders()))
    out = await r.handle("/open XRPUSDT BUY abc", OPERATOR)
    assert "Invalid number" in out


async def test_open_surfaces_an_exchange_exception():
    class _Boom:
        async def open_from_signal(self, *a, **k):
            raise RuntimeError("40762 size limit")

    r = _router(orch=_orch(orders=_Boom()))
    r.positions.adapter = _Adapter(mark=2.1)
    out = await r.handle("/open XRPUSDT BUY 1.9", OPERATOR)
    assert "Order failed" in out and "40762" in out


async def test_open_without_an_orchestrator_is_refused_not_crashed():
    r = _router(orch=None)
    out = await r.handle("/open XRPUSDT BUY 1.9", OPERATOR)
    assert "unavailable" in out.lower()


# ------------------------------------------------- setMyCommands registration
async def test_register_bot_commands_posts_every_command(monkeypatch):
    monkeypatch.setenv("TG_BOT_TOKEN", "123:abc")
    sent = {}

    class _C:
        async def post(self, url, json=None, timeout=None):
            sent["url"] = url
            sent["body"] = json
            return SimpleNamespace(json=lambda: {"ok": True})

    async def _get_client():
        return _C()

    monkeypatch.setattr(bot_poller, "get_client", _get_client)
    ok = await bot_poller.register_bot_commands(_router())
    assert ok is True
    assert "setMyCommands" in sent["url"]
    names = {c["command"] for c in sent["body"]["commands"]}
    assert names == {c[0] for c in CommandRouter.COMMANDS}
    # Telegram rejects names that are not lowercase a-z/0-9/_
    for n in names:
        assert n.replace("_", "").isalnum() and n.islower()


async def test_register_bot_commands_never_raises_on_failure(monkeypatch):
    monkeypatch.setenv("TG_BOT_TOKEN", "123:abc")

    class _C:
        async def post(self, url, json=None, timeout=None):
            raise RuntimeError("network down")

    async def _get_client():
        return _C()

    monkeypatch.setattr(bot_poller, "get_client", _get_client)
    assert await bot_poller.register_bot_commands(_router()) is False


async def test_register_bot_commands_noop_without_a_token(monkeypatch):
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
    assert await bot_poller.register_bot_commands(_router()) is False
