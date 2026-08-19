"""P2: signal parsing, SymbolMap (Bitget multiplier contracts), fake source."""

from __future__ import annotations

import pytest

from src.sources.parser import SymbolMap, parse_message, parse_messages, parse_signals
from src.sources.telegram import FakeSignalSource, TelethonSource

# The live Bitget USDT-FUTURES set contains BOTH plain and multiplier contracts (VERIFIED).
TRADABLE = {"BTCUSDT", "ETHUSDT", "PEPEUSDT", "1000BONKUSDT", "1000SATSUSDT",
            "1000000MOGUSDT", "SOLUSDT"}


def test_parse_basic_long_signal():
    rs = parse_message("BTCUSDT LONG entry 63000 sl 61000 tp 66000 lev 10")
    assert rs.symbol_usdt == "BTCUSDT"
    assert rs.side == "BUY"
    assert rs.entry == 63000
    assert rs.sl == 61000
    assert rs.tp == 66000
    assert rs.leverage == 10
    assert rs.is_management is False


def test_parse_short_signal():
    rs = parse_message("$ETH SHORT entry: 2500 sl: 2600 tp: 2300")
    assert rs.symbol_usdt == "ETHUSDT"
    assert rs.side == "SELL"
    assert rs.entry == 2500


def test_parse_hash_mention():
    rs = parse_message("#SOL long entry 150 sl 140")
    assert rs.symbol_usdt == "SOLUSDT"


def test_entry_signal_with_sl_is_not_misrouted_to_management():
    """A real signal mentions SL/TP; it must still be an ENTRY, not management."""
    rs = parse_message("BTC long entry 63000 sl 61000 tp 65000")
    assert rs.is_management is False
    assert rs.management_action is None


@pytest.mark.parametrize("text,action", [
    ("sl to entry", "sl_to_entry"),
    ("move sl to be", "sl_to_entry"),
    ("breakeven now", "sl_to_entry"),
    ("tp1 booked", "partial"),
    ("book partial", "partial"),
    ("close full", "full_close"),
    ("closed at entry", "full_close"),
    ("cutting", "full_close"),
    ("cancel", "cancel_pending"),
    ("invalidated", "cancel_pending"),
])
def test_management_fast_path_actions(text, action):
    rs = parse_message(text)
    assert rs.is_management is True
    assert rs.management_action == action


def test_management_tp_modify_extracts_price():
    rs = parse_message("tp1 3350")
    assert rs.is_management is True
    assert rs.management_action == "tp_modify"
    assert rs.raw["mgmt_price"] == 3350.0


def test_management_sl_modify_extracts_price():
    rs = parse_message("sl 61250")
    assert rs.is_management is True
    assert rs.management_action == "sl_modify"
    assert rs.raw["mgmt_price"] == 61250.0


def test_multi_symbol_line_splits_into_per_symbol_signals():
    """$INJ 2R up and $PUMP 6R full tp up booking tp1 on both => 2 RawSignals."""
    sigs = parse_signals("$INJ 2R up and $PUMP 6R full tp up booking tp1 on both")
    assert len(sigs) == 2
    syms = {s.symbol_usdt for s in sigs}
    assert syms == {"INJUSDT", "PUMPUSDT"}


def test_r_dialect_inj_2r_up_is_sl_to_entry():
    sigs = parse_signals("$INJ 2R up")
    assert len(sigs) == 1
    assert sigs[0].symbol_usdt == "INJUSDT"
    assert sigs[0].management_action == "sl_to_entry"


def test_r_dialect_pump_6r_tp1_is_partial():
    sigs = parse_signals("$PUMP 6R full tp up booking tp1 on both")
    assert len(sigs) == 1
    assert sigs[0].symbol_usdt == "PUMPUSDT"
    assert sigs[0].management_action == "partial"


def test_r_dialect_requires_r_multiple_not_bare_tp():
    """An explicit numeric tp modify must NOT be swallowed by the R-dialect."""
    rs = parse_message("$BTCUSDT tp 0.5")
    assert rs.management_action == "tp_modify"
    assert rs.raw["mgmt_price"] == 0.5


def test_chatter_does_not_invent_a_symbol():
    rs = parse_message("gm everyone, good luck today")
    assert rs.symbol_usdt == ""
    assert rs.is_management is False


def test_confidence_is_clamped():
    assert parse_message("BTC long entry 1 conf 5").confidence == 1.0
    assert parse_message("BTC long entry 1 conf -3").confidence == 0.0


def test_leverage_defaults_to_one():
    assert parse_message("BTC long entry 1 sl 0.5").leverage == 1


def test_parse_messages_batch():
    out = parse_messages(["BTC long entry 1 sl 0.5", "sl to entry"])
    assert len(out) == 2
    assert out[1].is_management


def test_raw_text_and_message_id_preserved():
    rs = parse_message("BTC long entry 1 sl 0.5", message_id=99, ts_ms=1700)
    assert rs.message_id == 99
    assert rs.ts_ms == 1700
    assert "BTC" in rs.raw_text


# --------------------------------------------------------------------- SymbolMap
def test_symbol_map_plain_resolution_without_tradable_set():
    assert SymbolMap().resolve("btc").symbol == "BTCUSDT"


def test_symbol_map_resolves_bare_token_to_multiplier_contract():
    """BONK is listed only as 1000BONKUSDT — resolving to BONKUSDT would be untradable."""
    smap = SymbolMap(TRADABLE)
    assert smap.resolve("BONK").symbol == "1000BONKUSDT"
    assert smap.resolve("SATS").symbol == "1000SATSUSDT"
    assert smap.resolve("MOG").symbol == "1000000MOGUSDT"


def test_symbol_map_prefers_exact_plain_contract_when_it_exists():
    """PEPE IS listed plain, so no 1000 prefix may be invented."""
    assert SymbolMap(TRADABLE).resolve("PEPE").symbol == "PEPEUSDT"


def test_symbol_map_passes_through_unknown_symbol():
    assert SymbolMap(TRADABLE).resolve("NOTLISTED").symbol == "NOTLISTEDUSDT"


def test_symbol_map_is_tradable():
    smap = SymbolMap(TRADABLE)
    assert smap.is_tradable(smap.resolve("BTC")) is True
    assert smap.is_tradable(smap.resolve("NOTLISTED")) is False


def test_symbol_map_empty_set_treats_everything_as_tradable():
    smap = SymbolMap()
    assert smap.is_tradable(smap.resolve("WHATEVER")) is True


def test_looks_like_symbol():
    assert SymbolMap.looks_like_symbol("$BTC") is True
    assert SymbolMap.looks_like_symbol("1000BONKUSDT") is True


def test_parser_uses_injected_symbol_map():
    rs = parse_message("BONK long entry 0.00002 sl 0.000018",
                       symbol_map=SymbolMap(TRADABLE))
    assert rs.symbol_usdt == "1000BONKUSDT"


# ------------------------------------------------------------------- fake source
@pytest.mark.asyncio
async def test_fake_signal_source_replays_messages():
    src = FakeSignalSource(["BTC long entry 63000 sl 61000", "sl to entry"])
    await src.start()
    q = src.subscribe()
    first = await q.get()
    second = await q.get()
    assert first.symbol_usdt == "BTCUSDT"
    assert second.is_management is True
    await src.stop()
    assert src.started is False


@pytest.mark.asyncio
async def test_fake_signal_source_push():
    src = FakeSignalSource()
    await src.start()
    rs = (await src.push("ETH short entry 2500 sl 2600", message_id=7))[0]
    assert rs.side == "SELL"
    assert (await src.subscribe().get()).message_id == 7


@pytest.mark.asyncio
async def test_telethon_source_refuses_to_start_without_credentials():
    """Missing session must be an explicit blocker, never a silent no-op."""
    src = TelethonSource(channel="fattyfatclub", session="", api_id=0, api_hash="")
    with pytest.raises(RuntimeError) as exc:
        await src.start()
    assert "BLOCKED.md" in str(exc.value)


class _FakeMsg:
    def __init__(self, text, mid, date):
        self.message = text
        self.text = text
        self.id = mid
        self.date = date
        self.media = None


@pytest.mark.asyncio
async def test_catch_up_enqueues_management_only_not_entries():
    """History catch-up must recover missed management calls but never replay
    old ENTRY signals (that would open stale positions)."""
    import datetime as _dt

    src = TelethonSource(channel="fattyfatclub", session="s", api_id=1, api_hash="h")

    class _FakeClient:
        async def get_entity(self, ch):
            return "entity"

        async def iter_messages(self, entity, limit=None):
            past = _dt.datetime.now(_dt.timezone.utc).timestamp() - 100
            for args in [
                ("$PUMP 6R full tp up booking tp1 on both", 101),
                ("BTC long entry 63000 sl 61000", 102),
                ("sl to entry", 103),
            ]:
                yield _FakeMsg(args[0], args[1],
                              _dt.datetime.fromtimestamp(past, _dt.timezone.utc))

    src._client = _FakeClient()
    captured = []
    async def _put(rs):
        captured.append(rs)
    src.queue.put = _put
    await src._catch_up(limit=10)
    syms = {(s.symbol_usdt, s.management_action) for s in captured}
    assert ("PUMPUSDT", "partial") in syms
    assert ("", "sl_to_entry") in syms
    assert all(s.is_management for s in captured), "entry replayed!"
    assert len([s for s in captured if s.symbol_usdt == "BTCUSDT"]) == 0
