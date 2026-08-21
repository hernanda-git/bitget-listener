"""P15: Telegram realtime event cards.

Every operator-visible lifecycle moment must render a distinct, data-backed card:
signal triggered, position opened, TP hit, SL hit, manual close, liquidation.
None of these may raise, and each must carry the actual symbol / price / PnL — not
a placeholder. This locks in the "bot must be legit: realtime pushes with real
data" requirement.
"""

from __future__ import annotations

from src.domain.model import Instrument, OrderRequest, Position, Side
from src.notifier.telegram import TelegramNotifier


def _signal():
    return type("S", (), {
        "side": Side.BUY,
        "instrument": Instrument.from_token("WLD"),
        "entry": 0.365, "sl": 0.3538, "tp": 0.4175, "notional_usd": 5.0,
    })()


def _order():
    return OrderRequest(instrument=Instrument.from_token("WLD"), side=Side.BUY,
                        qty=2.0, price=0.365, sl=0.3538, tp=0.4175,
                        leverage=10, margin_mode="cross",
                        client_order_id="x")


def _closed_row(reason: str, net: float):
    return type("R", (), {
        "symbol": "WLDUSDT", "exit_price": 0.4175, "net_usd": net,
        "open_fee": 0.001, "close_fee": 0.001, "exit_reason": reason,
    })()


def test_signal_card_has_real_data():
    txt = TelegramNotifier.format_signal(_signal())
    assert "WLDUSDT" in txt and "signal received" in txt
    assert "0.365" in txt and "0.3538" in txt and "0.4175" in txt


def test_open_card_has_real_data():
    res = {"status": "PLACED", "order": _order()}
    txt = TelegramNotifier.format_open(res)
    assert "WLDUSDT" in txt and "position opened" in txt
    assert "0.365" in txt and "2" in txt and "10x" in txt


def test_tp_hit_card():
    txt = TelegramNotifier.format_tp_hit(_closed_row("target_hit", 0.05))
    assert "TP hit" in txt and "WLDUSDT" in txt and "0.05" in txt


def test_sl_hit_card():
    txt = TelegramNotifier.format_sl_hit(_closed_row("stop_breached", -0.03))
    assert "SL hit" in txt and "WLDUSDT" in txt and "-0.03" in txt


def test_manual_close_card():
    txt = TelegramNotifier.format_manual_tp(_closed_row("telegram", 0.01))
    assert "Manual close" in txt and "WLDUSDT" in txt


def test_liquidation_card():
    txt = TelegramNotifier.format_liquidation(_closed_row("liquidation", -0.07))
    assert "Liquidation" in txt and "WLDUSDT" in txt and "-0.07" in txt


def test_open_card_none_order_is_empty():
    assert TelegramNotifier.format_open({"status": "PAPER"}) == ""


def _closed_row_full(reason, net):
    return type("R", (), {"symbol": "WLDUSDT", "exit_price": 0.39, "net_usd": net,
                          "open_fee": 0.001, "close_fee": 0.001, "exit_reason": reason})()


def test_partial_card():
    txt = TelegramNotifier.format_partial(_closed_row_full("tp1_booked", 0.025), 0.5)
    assert "TP1 booked" in txt and "50%" in txt and "0.025" in txt


def test_breakeven_card():
    txt = TelegramNotifier.format_breakeven("WLDUSDT", 0.365)
    assert "Break-even" in txt and "WLDUSDT" in txt and "0.365" in txt


def test_stop_modify_card():
    txt = TelegramNotifier.format_stop_modify("WLDUSDT", 0.34)
    assert "Stop updated" in txt and "0.34" in txt


def test_tp_modify_card():
    txt = TelegramNotifier.format_tp_modify("WLDUSDT", 0.42)
    assert "Target updated" in txt and "0.42" in txt


def test_skipped_card():
    txt = TelegramNotifier.format_skipped("WLDUSDT", "max concurrent positions (5) reached")
    assert "Signal skipped" in txt and "WLDUSDT" in txt and "max concurrent" in txt


def test_mode_change_card():
    txt = TelegramNotifier.format_mode_change("LIVE", "token redeemed")
    assert "Mode change" in txt and "LIVE" in txt


def test_rate_limit_parked_card():
    txt = TelegramNotifier.format_rate_limit_parked(42.0)
    assert "Rate-limit park" in txt and "42" in txt


def test_catchup_card():
    txt = TelegramNotifier.format_catchup("WLDUSDT", "pending orders cancelled")
    assert "Catch-up" in txt and "WLDUSDT" in txt
