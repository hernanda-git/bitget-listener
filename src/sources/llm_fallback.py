"""LLM fallback for signal parsing — OPT-IN, zero-latency when unconfigured.

Why this exists
--------------
The deterministic regex parser in ``parser.py`` handles the structured signals the
trader posts most of the time (``#TOKEN $TOKEN LONG TRADE / ENTRY / TARGET / STOPLOSS``,
R-dialect management, etc.). But some real messages are free-text the regex can't
safely interpret (e.g. ``shorted $ONDO here at 0.345 / Stoploss above 0.3545`` with no
``entry`` label, or ``fartcoin first tp done`` with no ticker). Rather than bloat the
regex with ever-fragiler heuristics, an LLM is used as a FALLBACK for exactly those.

Hard constraints (operator requirement)
---------------------------------------
* **Never slows down valid scenarios.** The deterministic parser runs FIRST and its
  result is used as-is whenever it is confident. The LLM is only contacted when the
  deterministic parse yields nothing usable. So the 90% of signals that already work
  incur ZERO LLM latency and ZERO LLM cost.
* **Never breaks existing valid scenarios.** If the LLM is down, times out, returns
  garbage, or is unconfigured, we fall back to the deterministic result (or no signal).
  The LLM path can never raise into the trading pipeline.
* **Off by default.** With ``LLM_API_KEY`` unset, ``parse_with_fallback`` is a thin
  pass-through to ``parse_signals`` — no network, no import of httpx beyond what the
  adapter already uses.

Configuration (env)
-------------------
* ``LLM_BASE_URL``  -- OpenAI-compatible endpoint (default https://api.openai.com/v1).
* ``LLM_API_KEY``   -- set to enable the fallback; unset = disabled.
* ``LLM_MODEL``     -- chat model id (default gpt-4o-mini).
* ``LLM_TIMEOUT_S`` -- per-call timeout (default 3.0). On timeout, deterministic wins.
* ``LLM_ENABLED``   -- explicit "0"/"false" to force-disable even if a key is present.
"""

from __future__ import annotations

import json
import logging
import os
import re

import httpx

from src.domain.model import Instrument, RawSignal
from src.sources.parser import SymbolMap, parse_signals

log = logging.getLogger(__name__)

_PROMPT = (
    "You are a crypto futures signal extractor for a Bitget USDⓈ-M bot. "
    "Given a Telegram message from a trader, extract trading instructions as STRICT JSON.\n"
    "Rules:\n"
    "- One JSON object per message. If the message references multiple coins, pick the primary one.\n"
    "- symbol: the bare ticker uppercased (e.g. WLD, not WLDUSDT). Empty string if none.\n"
    "- side: 'BUY' (long) or 'SELL' (short). Empty if the message is not an entry.\n"
    "- entry, sl, tp: numeric prices, or null if absent.\n"
    "- action: one of 'entry', 'sl_to_entry', 'partial', 'full_close', 'cancel_pending', "
    "or 'none' (informational / unparsed). Use 'entry' only if a real entry price is present.\n"
    "- If the message is just chatter, meme, or a link with no trade intent, return "
    "{\"symbol\":\"\",\"side\":\"\",\"entry\":null,\"sl\":null,\"tp\":null,\"action\":\"none\"}.\n"
    "Respond with ONLY the JSON object, no markdown, no commentary.\n\n"
    "Message:\n"
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


class SignalLLM:
    """Thin OpenAI-compatible client. No-op when unconfigured."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None, timeout_s: float | None = None) -> None:
        self.base_url = (base_url or os.getenv("LLM_BASE_URL",
                                                "https://api.openai.com/v1")).rstrip("/")
        self.api_key = api_key if api_key is not None else os.getenv("LLM_API_KEY", "")
        enabled = os.getenv("LLM_ENABLED", "1") not in ("0", "false", "False")
        self.model = model or os.getenv("LLM_MODEL", "gpt-4o-mini")
        self.timeout_s = float(timeout_s or os.getenv("LLM_TIMEOUT_S", "3.0"))
        self.enabled = bool(self.api_key) and enabled

    def _parse_json(self, text: str) -> dict | None:
        m = _JSON_RE.search(text or "")
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except (ValueError, TypeError):
            return None
        if not isinstance(obj, dict):
            return None
        return obj

    def _to_rawsignal(self, obj: dict, message_id, ts_ms, source, channel,
                      symbol_map: SymbolMap | None,
                      raw_direction: str | None = None,
                      raw_text: str = "") -> RawSignal | None:
        sym_raw = str(obj.get("symbol") or "").strip().lstrip("$#").upper()
        if not sym_raw:
            return None
        inst = (symbol_map.resolve(sym_raw) if symbol_map is not None
                else Instrument.from_token(sym_raw))
        side = str(obj.get("side") or "").upper()
        model_direction = side if side in ("BUY", "SELL") else ""
        action = str(obj.get("action") or "none").lower()
        mgmt = action if action in ("sl_to_entry", "partial", "full_close",
                                     "cancel_pending") else None
        # 'entry' from the LLM is only honoured when a real numeric entry price exists.
        try:
            entry = float(obj["entry"]) if obj.get("entry") not in (None, "") else None
        except (ValueError, TypeError):
            entry = None
        if action == "entry" and entry is None:
            mgmt = None  # LLM said entry but gave no price -> not trustworthy
        if action == "entry" and not model_direction:
            return None
        if not model_direction:
            model_direction = "BUY"  # management actions do not execute direction
        try:
            sl = float(obj["sl"]) if obj.get("sl") not in (None, "") else None
        except (ValueError, TypeError):
            sl = None
        try:
            tp = float(obj["tp"]) if obj.get("tp") not in (None, "") else None
        except (ValueError, TypeError):
            tp = None
        return RawSignal(
            symbol_usdt=inst.symbol, base=inst.base,
            side=model_direction,
            entry=entry, sl=sl, tp=tp, notional_usd=0.0, leverage=1,
            confidence=0.0, raw={
                "text": raw_text, "llm": True,
                "model_direction": model_direction if action == "entry" else "",
                "raw_direction": raw_direction or "",
                "direction_conflict": bool(
                    raw_direction in ("BUY", "SELL")
                    and raw_direction != model_direction
                ),
            },
            source=source, message_id=message_id, ts_ms=ts_ms,
            channel=channel, raw_text=raw_text, is_management=mgmt is not None,
            management_action=mgmt,
        )

    async def parse(self, text: str, message_id=None, ts_ms=None, source="fattyfatclub",
                            channel="", symbol_map: SymbolMap | None = None,
                    raw_direction: str | None = None,
                    raw_text: str = "") -> RawSignal | None:
        if not self.enabled:
            return None
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}",
                              "Content-Type": "application/json"},
                    json={"model": self.model,
                          "messages": [{"role": "user", "content": _PROMPT + text}],
                          "temperature": 0.0, "max_tokens": 300},
                )
            if resp.status_code != 200:
                log.warning("[llm-fallback] provider returned HTTP %s — using deterministic",
                            resp.status_code)
                return None
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except (httpx.TimeoutException, Exception) as exc:  # noqa: BLE001 - fallback must never throw
            log.warning("[llm-fallback] call failed (%s) — using deterministic parse",
                        type(exc).__name__)
            return None
        obj = self._parse_json(content)
        if obj is None:
            log.warning("[llm-fallback] no JSON in response — using deterministic")
            return None
        rs = self._to_rawsignal(obj, message_id, ts_ms, source, channel, symbol_map,
                                raw_direction=raw_direction, raw_text=raw_text)
        if rs is None:
            return None
        log.info("[llm-fallback] recovered signal for %s action=%s", rs.symbol_usdt,
                 rs.management_action or "entry")
        return rs


async def parse_with_fallback(text: str, message_id=None, ts_ms=None,
                               source: str = "fattyfatclub", channel: str = "",
                               symbol_map: SymbolMap | None = None,
                               llm: SignalLLM | None = None) -> list[RawSignal]:
    """Deterministic-first parse with optional LLM fallback.

    The deterministic parser ALWAYS runs first. The LLM is consulted only when the
    deterministic result is empty (regex found no signal at all). This keeps valid
    signals fast and untouched; the LLM only helps with free-text the regex misses.
    """
    deterministic = parse_signals(text, message_id=message_id, ts_ms=ts_ms,
                                  source=source, channel=channel, symbol_map=symbol_map)
    # Confident if at least one parsed signal has a symbol (and for entries, an entry).
    confident = any(
        s.symbol_usdt and (s.is_management or s.entry is not None) for s in deterministic
    )
    if confident or llm is None or not llm.enabled:
        return deterministic
    # Deterministic found nothing usable -> ask the LLM once.
    raw_directions = set()
    for signal in deterministic:
        raw_directions.update((signal.raw or {}).get("direction_evidence", []))
    if len(raw_directions) > 1:
        return deterministic
    raw_direction = next(iter(raw_directions)) if len(raw_directions) == 1 else None
    rs = await llm.parse(text, message_id=message_id, ts_ms=ts_ms, source=source,
                         channel=channel, symbol_map=symbol_map,
                         raw_direction=raw_direction, raw_text=text)
    if rs is None:
        return deterministic
    return [rs]
