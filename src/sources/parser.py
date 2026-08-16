"""Free-text signal parser for @fattyfatclub, retargeted to Bitget symbols.

Adapted from bybit-listener's parser (the cleanest management/entry disambiguation
of the four) with two Bitget-specific additions grounded in probe findings:

- ``SymbolMap`` knows that Bitget lists BOTH plain ``PEPEUSDT`` **and** multiplier
  contracts like ``1000BONKUSDT`` / ``1000000MOGUSDT`` (VERIFIED: 754 symbols).
  It therefore never *invents* a ``1000`` prefix; it resolves against the live
  tradable set when one is supplied, else falls back to the plain form.
- Management fast-path depth copied from the Binance sibling (``tp1 booked``,
  ``sl to entry``, ``full``, stale-LIMIT cancel) so management never reaches the LLM.
"""

from __future__ import annotations

import re
from typing import Iterable, Literal

from src.domain.model import Instrument, RawSignal

_LONG = {"long", "buy", "l", "b"}
_SHORT = {"short", "sell", "s", "sh"}

# Management action patterns (lowercased). First match wins. Ordered most-specific first.
_MANAGEMENT = [
    (r"\b(sl to entry|sl be|move sl to be|breakeven|break even)\b", "sl_to_entry"),
    (r"\b(tp1 booked|tp booked|book partial|partial|partials)\b", "partial"),
    (r"\b(close full|exit full|full close|closed at entry|cutting|full|closed)\b", "full_close"),
    (r"\b(cancel|void|invalidated|no longer valid)\b", "cancel_pending"),
    (r"\btp[123]?\s*[:<=>]?\s*[\$]?([\d.]+)\b", "tp_modify"),
    (r"\bsl\s*[:<=>]?\s*[\$]?([\d.]+)\b", "sl_modify"),
]

_NUM_KEY_RE = re.compile(
    r"(entry|entry price|ep|sl|stoploss|stop loss|tp|takeprofit|take profit|"
    r"lev|leverage|conf|confidence|size|risk)\s*[:=]?\s*"
    r"[\$]?(-?\d+(?:\.\d+)?)"
)
_NUM_KEY_AT_ALL = re.compile(r"(entry|sl|tp|lev|leverage|long|short|buy|sell)", re.IGNORECASE)
_BARE_TICKER = re.compile(r"\b([A-Z0-9]{2,12})\b")
_STOPWORDS = {"USDT", "THE", "AND", "FOR", "YOU", "ALL", "NOW", "TP", "SL", "BUY",
              "SELL", "LONG", "SHORT", "LEV", "ENTRY", "BE"}


class SymbolMap:
    """Centralized TOKEN -> Bitget ``TOKENUSDT`` resolution.

    Bitget's live USDT-FUTURES list contains multiplier contracts (``1000BONKUSDT``,
    ``1000SATSUSDT``, ``1000000MOGUSDT``, ``10000NEXUSDT``) alongside plain ones
    (``PEPEUSDT``). Passing ``tradable`` (the live symbol set from
    ``/api/v2/mix/market/contracts``) lets a bare ``BONK`` resolve to the real
    ``1000BONKUSDT`` contract instead of a symbol that does not exist.
    """

    MULTIPLIER_PREFIXES = ("1000", "10000", "100000", "1000000")

    def __init__(self, tradable: Iterable[str] | None = None) -> None:
        self.tradable = {s.upper() for s in (tradable or [])}

    def resolve(self, text_token: str) -> Instrument:
        t = text_token.strip().lstrip("$#").upper()
        base_symbol = t if t.endswith("USDT") else f"{t}USDT"
        if not self.tradable or base_symbol in self.tradable:
            return Instrument.from_token(t)
        for pref in self.MULTIPLIER_PREFIXES:
            cand = f"{pref}{base_symbol}"
            if cand in self.tradable:
                return Instrument.from_token(cand)
        return Instrument.from_token(t)

    def is_tradable(self, instrument: Instrument) -> bool:
        return (not self.tradable) or instrument.symbol.upper() in self.tradable

    @staticmethod
    def looks_like_symbol(word: str) -> bool:
        w = word.lstrip("$#").upper()
        return bool(re.fullmatch(r"[A-Z0-9]{2,14}USDT", w)) or bool(
            re.fullmatch(r"[A-Z0-9]{2,12}", w))


_DEFAULT_MAP = SymbolMap()


def _find_symbol(text: str) -> str | None:
    m = re.search(r"\b([A-Za-z0-9]{2,14}USDT)\b", text, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"[$#]([A-Za-z0-9]{2,14})", text)
    if m:
        return m.group(1)
    if _NUM_KEY_AT_ALL.search(text):
        for tok in _BARE_TICKER.findall(text):
            if tok.upper() not in _STOPWORDS and not tok.isdigit():
                return tok
    return None


def _side(text: str) -> str | None:
    low = text.lower()
    if any(re.search(rf"\b{k}\b", low) for k in _LONG):
        return "BUY"
    if any(re.search(rf"\b{k}\b", low) for k in _SHORT):
        return "SELL"
    return None


def _management_action(text: str) -> tuple[str, dict] | None:
    low = text.lower()
    for pat, action in _MANAGEMENT:
        m = re.search(pat, low)
        if m:
            extra: dict = {}
            if action in ("tp_modify", "sl_modify") and m.groups():
                try:
                    extra["price"] = float(m.group(1))
                except (ValueError, TypeError):
                    pass
            return action, extra
    return None


def _extract_num_map(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for key, val in _NUM_KEY_RE.findall(text.lower()):
        k = key.replace(" ", "").replace("entryprice", "entry")
        k = {"ep": "entry", "stoploss": "sl", "takeprofit": "tp",
             "lev": "leverage", "conf": "confidence"}.get(k) or k
        try:
            out[k] = float(val)
        except ValueError:
            continue
    return out


def parse_message(text: str, message_id: int | None = None, ts_ms: int | None = None,
                  source: str = "fattyfatclub", channel: str = "",
                  symbol_map: SymbolMap | None = None) -> RawSignal:
    """Parse a channel message into a ``RawSignal``.

    A message carrying BOTH a side and an entry price is an ENTRY, even when it also
    lists sl/tp — otherwise every real signal would be misrouted to the management
    path. Management instructions are PURE: no side + no entry price.
    """
    smap = symbol_map or _DEFAULT_MAP
    text = (text or "").strip()
    raw: dict[str, object] = {"text": text}

    sym = _find_symbol(text)
    side = _side(text)
    chosen_side: Literal["BUY", "SELL"] = side or "BUY"  # type: ignore[assignment]
    nums = _extract_num_map(text)

    mgmt = _management_action(text)
    is_entry = side is not None and nums.get("entry") is not None
    if is_entry:
        mgmt = None

    inst = smap.resolve(sym) if sym else Instrument(symbol="", base="", quote="USDT")

    rs = RawSignal(
        symbol_usdt=inst.symbol,
        base=inst.base,
        side=chosen_side,
        entry=nums.get("entry"),
        sl=nums.get("sl"),
        tp=nums.get("tp"),
        notional_usd=nums.get("size", 0.0) or nums.get("risk", 0.0),
        leverage=int(nums.get("leverage", 1) or 1),
        confidence=min(1.0, max(0.0, nums.get("confidence", 0.0))),
        raw=raw,
        source=source,
        message_id=message_id,
        ts_ms=ts_ms,
        channel=channel,
        raw_text=text,
        is_management=mgmt is not None,
        management_action=mgmt[0] if mgmt else None,
    )
    if mgmt:
        raw["mgmt_price"] = mgmt[1].get("price")
        raw["mgmt_symbol"] = inst.symbol or None
    return rs


def parse_messages(items: Iterable[str], **kw) -> list[RawSignal]:
    return [parse_message(t, **kw) for t in items]
