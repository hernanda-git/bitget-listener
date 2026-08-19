"""P13b: the presentation layer — fmt.

Pins the rules that keep Telegram output readable and, more importantly, honest:
a missing number renders as a dash, never as a fabricated zero, and every
timestamp lands in Asia/Jakarta.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.notifier import fmt


# ------------------------------------------------------------------- money/sign
def test_money_always_carries_an_explicit_sign():
    assert fmt.money(1.5) == "+1.5000"
    assert fmt.money(-1.5) == "-1.5000"
    assert fmt.money(0) == "+0.0000"


def test_money_can_suppress_the_sign_for_absolute_quantities():
    assert fmt.money(1.5, signed=False) == "1.5000"


def test_money_renders_none_as_a_dash_not_a_zero():
    """A missing figure must never look like a real zero."""
    assert fmt.money(None) == fmt.DASH
    assert "0" not in fmt.money(None)


def test_money_honours_precision_and_suffix():
    assert fmt.money(2.5, 2, suffix=" USDT") == "+2.50 USDT"


# ---------------------------------------------------------------------- prices
def test_price_strips_trailing_zero_noise():
    assert fmt.price(2.1000) == "2.1"
    assert fmt.price(0.50000000) == "0.5"


def test_price_uses_thousands_separators_for_large_values():
    assert fmt.price(64321.5) == "64,321.50"


def test_price_keeps_precision_for_sub_cent_coins():
    assert fmt.price(0.00001234) == "0.00001234"


def test_price_renders_none_and_zero_as_a_dash():
    """BUG B: an absent liquidation price is not a zero one."""
    assert fmt.price(None) == fmt.DASH
    assert fmt.price(0) == fmt.DASH


def test_qty_and_pct():
    assert fmt.qty(3.0) == "3"
    assert fmt.qty(None) == fmt.DASH
    assert fmt.pct(30.0) == "30.0%"
    assert fmt.pct(None) == fmt.DASH


# ---------------------------------------------------------------------- glyphs
def test_signed_glyph_is_tri_state():
    assert fmt.signed_glyph(1) == "🟢"
    assert fmt.signed_glyph(-1) == "🔴"
    assert fmt.signed_glyph(0) == "▪️"
    assert fmt.signed_glyph(None) == "▪️"


def test_side_glyph_covers_aliases():
    assert fmt.side_glyph("BUY") == fmt.side_glyph("LONG") == "▲"
    assert fmt.side_glyph("SELL") == fmt.side_glyph("SHORT") == "▼"


def test_ok_glyph_distinguishes_unknown_from_bad():
    assert fmt.ok_glyph(True) == "✅"
    assert fmt.ok_glyph(False) == "⚠️"
    assert fmt.ok_glyph(None) == "❔"


# ------------------------------------------------------------------ timestamps
def test_when_converts_utc_to_jakarta():
    """10:00 UTC is 17:00 in Jakarta (UTC+7, no DST)."""
    out = fmt.when("2026-08-19T10:00:00Z")
    assert "17:00" in out
    assert "19 Aug" in out


def test_when_treats_a_naive_timestamp_as_utc():
    assert "17:00" in fmt.when("2026-08-19T10:00:00")


def test_when_accepts_an_aware_datetime():
    dt = datetime(2026, 8, 19, 10, 0, tzinfo=timezone.utc)
    assert "17:00" in fmt.when(dt)


def test_when_respects_an_existing_offset():
    """Already Jakarta-local: must not shift a second time."""
    dt = datetime(2026, 8, 19, 17, 0, tzinfo=timezone(timedelta(hours=7)))
    assert "17:00" in fmt.when(dt)


def test_when_handles_epoch_seconds_and_milliseconds():
    secs = 1787135400          # 2026-08-19T10:30:00Z
    assert fmt.when(secs) == fmt.when(secs * 1000)


def test_when_returns_empty_for_missing_values():
    assert fmt.when(None) == ""
    assert fmt.when("") == ""


def test_when_echoes_an_unparseable_string_rather_than_lying():
    assert fmt.when("last tuesday") == "last tuesday"


# ------------------------------------------------------------------- structure
def test_join_drops_empty_blocks_and_separates_with_a_blank_line():
    assert fmt.join("a", "", None, "b") == "a\n\nb"


def test_lines_drops_empties_without_adding_blank_lines():
    assert fmt.lines("a", None, "", "b") == "a\nb"


def test_title_and_rows_emit_valid_telegram_html():
    assert fmt.title("Wallet", "💰") == "<b>💰 Wallet</b>"
    assert fmt.row("Entry", "2.1") == "Entry  <code>2.1</code>"
    assert fmt.plain_row("PnL", "+1.0") == "PnL  +1.0"


def test_indentation_is_two_spaces_per_level():
    assert fmt.row("A", "b", indent=1).startswith("  ")


def test_hint_is_italic():
    assert fmt.hint("note") == "<i>note</i>"
