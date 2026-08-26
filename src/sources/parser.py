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

_LONG = {"long", "buy", "l", "b", "longed", "longs"}
_SHORT = {"short", "sell", "s", "sh", "shorted", "shorting", "shorts"}

# Management action patterns (lowercased). First match wins. Ordered most-specific first.
# R-dialect: "2R up" / "6R full tp" are trader shorthand — a number followed by "R"
# means the trade reached that risk-multiple. "up" / "be" => move stop to break-even;
# "full tp" / "booking tp1" / "tp1 booked" => book partial (tp1).
_MANAGEMENT = [
    (r"\b(sl to entry|sl be|move sl to be|breakeven|break even)\b", "sl_to_entry"),
    (r"\b(tp1 booked|tp booked|book partial|partial|partials|booking tp1|tp1|"
     r"tp done|tp hit|first tp|booked tp)\b", "partial"),
    # Do not treat standalone "full" as a close command: normal prose such as
    # "full breakdown" must never close the only open position. Keep explicit
    # close verbs and the established trader shorthand only.
    (r"\b(close full|exit full|full close|closed at entry|cutting|closed)\b", "full_close"),
    (r"\b(cancel|void|invalidated|no longer valid)\b", "cancel_pending"),
    (r"\btp[123]?\s+[\$]?(\d+(?:\.\d+)?)\b", "tp_modify"),
    (r"\bsl\s+[\$]?(\d+(?:\.\d+)?)\b", "sl_modify"),
]

# R-multiple shorthand: "<n>R" near a ticker. "up"/"be" => break-even; "full tp"/
# "booking tp1" present => partial. Standalone "nR" with no other clue => sl_to_entry
# (lock profit at entry and let it run), matching the trader's "2R up" idiom.
_R_MULTIPLE = re.compile(r"(\d+(?:\.\d+)?)\s*R\b", re.IGNORECASE)


def _r_dialect_action(clause: str) -> str | None:
    """Map an R-based management clause to a concrete action.

    Only fires when the clause actually carries an R-multiple ("2R", "6R"), so an
    explicit numeric modify like "$BTC tp 0.5" is NOT mistaken for a partial.

    ``$INJ 2R up``      -> sl_to_entry (lock BE, let it run)
    ``$PUMP 6R ... tp1`` -> partial      (book tp1)
    ``$X 3R full``      -> full_close
    """
    if not _R_MULTIPLE.search(clause):
        return None
    low = clause.lower()
    # A numeric price right after tpN/sl ("tp1 3350", "sl 61250") is an explicit
    # modify, NOT a partial/close — bail so the numeric handler picks it up.
    if re.search(r"(?:tp[123]?|sl)\s*[:<=>]?\s*[\$]?\s*\d", low):
        return None
    has_partial = bool(re.search(r"tp1|booking tp|full tp|partial|book|tp done|tp hit|first tp", low))
    has_full = bool(re.search(r"\bfull close\b|\bexit full\b|\bfull\b.*\bclose\b|closed|close here|cut", low))
    if has_full:
        return "full_close"
    if has_partial:
        return "partial"
    return "sl_to_entry"

_NUM_KEY_RE = re.compile(
    r"(entry|entry price|ep|sl|stoploss|stop loss|tp|takeprofit|take profit|"
    r"target|targets|lev|leverage|conf|confidence|size|risk)\s*[:=]?\s*"
    r"(?:around|near|above|below|at|~)?\s*"
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


def _direction_evidence(text: str) -> set[str]:
    low = text.lower()
    out: set[str] = set()
    if any(re.search(rf"\b{k}\b", low) for k in _LONG):
        out.add("BUY")
    if any(re.search(rf"\b{k}\b", low) for k in _SHORT):
        out.add("SELL")
    return out


def _conditional(text: str, symbol: str | None, nums: dict[str, float]) -> tuple[str, float] | None:
    """Classify conditional prose before any model or order path can run."""
    if not symbol:
        return None
    low = text.lower()
    trigger = nums.get("entry")
    if trigger is None:
        m = re.search(r"(?:hold\s+(?:above|below)|(?:pullback|retrace)|buy\s+the\s+dip|"
                      r"breakout\s+(?:above|below))\D{0,20}(\d+(?:\.\d+)?)", low)
        trigger = float(m.group(1)) if m else None
    if trigger is None:
        return None
    if re.search(r"\bhold\s+above\b|\bbreakout\s+above\b", low):
        return "hold_above", trigger
    if re.search(r"\bhold\s+below\b|\bbreakout\s+below\b", low):
        return "hold_below", trigger
    if re.search(r"\bpullback\b|\bretrace\b|\bbuy\s+the\s+dip\b", low):
        return "limit_entry", trigger
    return None


def _management_action(text: str) -> tuple[str, dict] | None:
    low = text.lower()
    # 1) Numeric modifies — only when an actual price follows (tp 0.5 / sl 61250 /
    # tp1 3350). Requiring the digit+separator keeps "tp1 booked" out of here.
    for pat, action in _MANAGEMENT:
        if action not in ("tp_modify", "sl_modify"):
            continue
        m = re.search(pat, low)
        if m:
            extra: dict = {}
            if m.groups():
                try:
                    extra["price"] = float(m.group(1))
                except (ValueError, TypeError):
                    pass
            return action, extra
    # 2) R-dialect: "$INJ 2R up" / "$PUMP 6R full tp booking tp1" carry no
    # literal "sl to entry" but mean exactly that (or partial/full).
    r_action = _r_dialect_action(text)
    if r_action is not None:
        return r_action, {}
    # 3) The remaining literal management verbs (sl_to_entry / partial / full_close
    # / cancel_pending).
    for pat, action in _MANAGEMENT:
        if action in ("tp_modify", "sl_modify"):
            continue
        m = re.search(pat, low)
        if m:
            return action, {}
    return None


def _extract_num_map(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for key, val in _NUM_KEY_RE.findall(text.lower()):
        k = key.replace(" ", "").replace("entryprice", "entry")
        k = {"ep": "entry", "stoploss": "sl", "takeprofit": "tp",
             "target": "tp", "targets": "tp",
             "lev": "leverage", "conf": "confidence"}.get(k) or k
        try:
            out[k] = float(val)
        except ValueError:
            continue
    return out


def _split_multi_symbol(text: str) -> list[str]:
    """Split a multi-symbol line into per-symbol clauses.

    e.g. "$INJ 2R up and $PUMP 6R full tp up booking tp1 on both"
      -> ["$INJ 2R up", "$PUMP 6R full tp up booking tp1 on both"]
    Each clause keeps the trailing context ("on both") which is harmless and
    lets the R-dialect matcher see "tp1"/"full" for the last symbol.
    """
    ticks = list(re.finditer(r"\$[A-Za-z0-9]{2,14}|[A-Za-z0-9]{2,14}USDT", text, re.IGNORECASE))
    if len(ticks) <= 1:
        return [text]
    clauses = []
    prev = 0
    for i, t in enumerate(ticks):
        end = ticks[i + 1].start() if i + 1 < len(ticks) else len(text)
        clauses.append(text[prev:end].strip())
        prev = end
    return [c for c in clauses if c]


def parse_signals(text: str, message_id: int | None = None, ts_ms: int | None = None,
                  source: str = "fattyfatclub", channel: str = "",
                  symbol_map: SymbolMap | None = None) -> list[RawSignal]:
    """Parse a channel message into one or more ``RawSignal`` (one per symbol).

    A line can reference several coins with per-symbol management (e.g.
    "$INJ 2R up and $PUMP 6R full tp up booking tp1 on both"). Each symbol becomes
    its own RawSignal so the pipeline acts on every one of them.

    A message carrying BOTH a side and an entry price is an ENTRY, even when it also
    lists sl/tp — otherwise every real signal would be misrouted to the management
    path. Management instructions are PURE: no side + no entry price.
    """
    smap = symbol_map or _DEFAULT_MAP
    text = (text or "").strip()
    clauses = _split_multi_symbol(text)
    results: list[RawSignal] = []
    for clause in clauses:
        raw: dict[str, object] = {"text": clause}
        sym = _find_symbol(clause)
        side = _side(clause)
        chosen_side: Literal["BUY", "SELL"] = side or "BUY"  # type: ignore[assignment]
        nums = _extract_num_map(clause)
        evidence = _direction_evidence(clause)

        conditional_language = bool(re.search(
            r"\bhold\s+(?:above|below)\b|\bpullback\b|\bretrace\b|"
            r"\bbuy\s+the\s+dip\b|\bbreakout\s+(?:above|below)\b", clause,
            re.IGNORECASE))
        mgmt = None if conditional_language else _management_action(clause)
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
            raw={**raw, "direction_evidence": sorted(evidence),
                 "direction_conflict": len(evidence) > 1},
            source=source,
            message_id=message_id,
            ts_ms=ts_ms,
            channel=channel,
            raw_text=clause,
            is_management=mgmt is not None,
            management_action=mgmt[0] if mgmt else None,
        )
        conditional = None if is_entry else _conditional(clause, sym, nums)
        if conditional:
            kind, trigger = conditional
            if kind == "limit_entry":
                rs.entry = trigger
                rs.raw["conditional_entry"] = True
                rs.raw["entry_order_type"] = "LIMIT"
            else:
                inferred = side or ("BUY" if kind == "hold_above" else "SELL")
                rs.side = inferred  # type: ignore[assignment]
                rs.raw["pending_setup"] = {
                    "setup_id": f"{channel or source}:{message_id or 0}",
                    "symbol": inst.symbol,
                    "direction": "LONG" if inferred == "BUY" else "SHORT",
                    "trigger_price": trigger,
                    "condition": kind,
                    "raw_text": clause,
                }
        if mgmt:
            rs.raw["mgmt_price"] = mgmt[1].get("price")
            rs.raw["mgmt_symbol"] = inst.symbol or None
        results.append(rs)
    return results


def parse_message(text: str, message_id: int | None = None, ts_ms: int | None = None,
                  source: str = "fattyfatclub", channel: str = "",
                  symbol_map: SymbolMap | None = None) -> RawSignal:
    """Backward-compatible single-symbol parse. Returns the first RawSignal.

    For multi-symbol lines use ``parse_signals`` (returns one per symbol).
    """
    return parse_signals(text, message_id=message_id, ts_ms=ts_ms, source=source,
                         channel=channel, symbol_map=symbol_map)[0]


def parse_messages(items: Iterable[str], **kw) -> list[RawSignal]:
    out: list[RawSignal] = []
    for t in items:
        out.extend(parse_signals(t, **kw))
    return out
