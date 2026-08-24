"""Signal sources: FakeSignalSource (tests) + TelethonSource (live @fattyfatclub).

``TelethonSource`` imports telethon lazily so the fakes-only test suite never needs
the dependency or a live session file.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid
from dataclasses import asdict
from pathlib import Path

from chart_signal_vision import (
    ChartVisionExtractor,
    chart_trade_fields,
    derive_protection_suggestion,
    parse_pending_setup_text,
)

from src.domain.model import RawSignal
from src.interfaces.ports import SignalSource
from src.sources.parser import SymbolMap, parse_signals
from src.sources.llm_fallback import SignalLLM, parse_with_fallback

log = logging.getLogger(__name__)


class FakeSignalSource(SignalSource):
    """Replays canned messages into the pipeline queue. Zero network."""

    def __init__(self, messages: list[str] | None = None,
                 symbol_map: SymbolMap | None = None, trace_store=None) -> None:
        self.messages = list(messages or [])
        self.symbol_map = symbol_map
        self.trace_store = trace_store
        self.queue: asyncio.Queue = asyncio.Queue()
        self.started = False

    async def start(self) -> None:
        self.started = True
        for i, text in enumerate(self.messages, start=1):
            if self.trace_store is not None:
                await asyncio.to_thread(
                    self.trace_store.record_received, f"fake:{i}", i, "fake", text)
            for rs in parse_signals(text, message_id=i, channel="fake", symbol_map=self.symbol_map):
                await self.queue.put(rs)
            if self.trace_store is not None:
                await asyncio.to_thread(
                    self.trace_store.record_stage, f"fake:{i}", i, "PARSER", "OK", "",
                    {"signals": [asdict(rs) for rs in parse_signals(
                        text, message_id=i, channel="fake", symbol_map=self.symbol_map)]})

    async def stop(self) -> None:
        self.started = False

    def subscribe(self) -> asyncio.Queue:
        return self.queue

    async def push(self, text: str, message_id: int | None = None) -> list[RawSignal]:
        mid = message_id if message_id is not None else int(uuid.uuid4().int % 2_000_000_000)
        if self.trace_store is not None:
            await asyncio.to_thread(
                self.trace_store.record_received, f"fake:{mid}", mid, "fake", text)
        sigs = parse_signals(text, message_id=mid, channel="fake", symbol_map=self.symbol_map)
        if self.trace_store is not None:
            await asyncio.to_thread(
                self.trace_store.record_stage, f"fake:{mid}", mid, "PARSER", "OK", "",
                {"signals": [asdict(rs) for rs in sigs]})
        for rs in sigs:
            await self.queue.put(rs)
        return sigs


class TelethonSource(SignalSource):
    """Live Telegram user-session listener for @fattyfatclub.

    Requires TG_API_ID / TG_API_HASH / TG_SESSION. Without a pre-authorized session
    this cannot run headlessly — that is a hard blocker, documented in BLOCKED.md
    rather than worked around.
    """

    def __init__(self, channel: str | None = None, session: str | None = None,
                 api_id: int | None = None, api_hash: str | None = None,
                 symbol_map: SymbolMap | None = None, trace_store=None) -> None:
        self.channel = channel or os.getenv("TG_CHANNEL", "fattyfatclub")
        self.session = session or os.getenv("TG_SESSION", "")
        self.api_id = api_id or int(os.getenv("TG_API_ID") or 0)
        self.api_hash = api_hash or os.getenv("TG_API_HASH", "")
        self.symbol_map = symbol_map
        self.trace_store = trace_store
        self.queue: asyncio.Queue = asyncio.Queue()
        self._client = None
        # Optional chart plugin. It is disabled unless a compatible vision key
        # exists, keeping text signals deterministic and zero-cost.
        self.chart_vision = ChartVisionExtractor() if (
            os.getenv("CHART_VISION_ENABLED", "0").lower() in ("1", "true", "yes")
            and (os.getenv("CHART_VISION_API_KEY") or os.getenv("GITLAWB_API_KEY"))
        ) else None
        # LLM fallback is a no-op unless LLM_API_KEY is configured; when enabled it
        # only ever runs for messages the deterministic parser cannot classify.
        self.llm = SignalLLM()

    async def _trace_stage(self, message_id: int, stage: str, status: str = "OK",
                           reason: str = "", payload: dict | None = None) -> None:
        if self.trace_store is not None:
            await asyncio.to_thread(
                self.trace_store.record_stage,
                f"{self.channel}:{int(message_id)}", int(message_id), stage,
                status, reason, payload or {})

    def _require_creds(self) -> None:
        missing = [n for n, v in (("TG_API_ID", self.api_id),
                                  ("TG_API_HASH", self.api_hash),
                                  ("TG_SESSION", self.session)) if not v]
        if missing:
            raise RuntimeError(
                "TelethonSource cannot start — missing " + ", ".join(missing) +
                ". A live Telethon session must be created interactively; see BLOCKED.md.")

    async def _enrich_with_chart(self, message, signals: list[RawSignal], caption: str) -> list[RawSignal]:
        """Parse an attached chart and enrich, never invent, a RawSignal."""
        if self.chart_vision is None or not getattr(message, "media", None):
            return signals
        path = Path("/tmp") / f"bitget_chart_{int(message.id)}.jpg"
        try:
            downloaded = await self._client.download_media(message, file=str(path))
            if not downloaded:
                return signals
            media_path = Path(str(downloaded))
            media_bytes = media_path.read_bytes()
            media_sha256 = hashlib.sha256(media_bytes).hexdigest()
            chart = await self.chart_vision.extract(downloaded, caption)
            fields = chart_trade_fields(chart, caption)
            suggestion = derive_protection_suggestion(chart, caption)
            pending = parse_pending_setup_text(
                caption, chart, caption, setup_id=f"{self.channel}:{int(message.id)}")
            if fields["timeframe"] and not chart.get("timeframe"):
                chart["timeframe"] = fields["timeframe"]
            for rs in signals:
                if not rs.symbol_usdt and fields["symbol_usdt"]:
                    inst = self.symbol_map.resolve(fields["symbol_usdt"]) if self.symbol_map else None
                    if inst is not None:
                        rs.symbol_usdt, rs.base = inst.symbol, inst.base
                if not rs.entry:
                    rs.entry = fields["entry"]
                if not rs.sl:
                    rs.sl = fields["sl"]
                if not rs.tp:
                    rs.tp = fields["tp"]
                if rs.side == "BUY" and fields["side"]:
                    rs.side = fields["side"]
                rs.chart = chart
                rs.movement_direction = fields["movement_direction"]
                rs.movement_waypoints = fields["movement_waypoints"]
                rs.image_path = str(downloaded)
                rs.raw["media_path"] = str(downloaded)
                rs.raw["media_bytes"] = len(media_bytes)
                rs.raw["media_sha256"] = media_sha256
                rs.raw["vision_provider"] = type(self.chart_vision).__name__
                rs.raw["chart_executable"] = fields["executable"]
                rs.raw["protection_suggestion"] = suggestion
                rs.raw["pending_setup"] = asdict(pending) if pending is not None else None
                rs.raw["chart_current_price"] = fields["current_price"]
            return signals
        except Exception as exc:  # chart enrichment must never kill Telegram intake
            log.warning("chart enrichment failed for message %s: %s", getattr(message, "id", "?"), exc)
            return signals

    async def start(self) -> None:
        self._require_creds()
        from telethon import TelegramClient, events   # lazy: tests never import telethon

        self._client = TelegramClient(self.session, self.api_id, self.api_hash)

        @self._client.on(events.NewMessage(chats=self.channel))
        async def _handler(event):    # pragma: no cover - requires a live session
            if self.trace_store is not None:
                await asyncio.to_thread(self.trace_store.record_received,
                    f"{self.channel}:{int(event.id)}", int(event.id), self.channel,
                    event.raw_text or "", bool(event.media),
                    source_timestamp=event.date.isoformat() if event.date else None,
                    reply_to_message_id=getattr(event.message, "reply_to_msg_id", None),
                )
            signals = await parse_with_fallback(event.raw_text, message_id=event.id,
                                    ts_ms=int(event.date.timestamp() * 1000),
                                    channel=self.channel, symbol_map=self.symbol_map,
                                    llm=self.llm)
            await self._trace_stage(event.id, "PARSER", "OK", "", {
                "signals": [asdict(rs) for rs in signals],
                "llm_used": any((rs.raw or {}).get("llm") for rs in signals),
            })
            signals = await self._enrich_with_chart(event.message, signals, event.raw_text)
            await self._trace_stage(event.id, "VISION",
                                    "OK" if self.chart_vision is not None else "DISABLED", "",
                                    {"enabled": self.chart_vision is not None,
                                     "has_media": bool(event.media),
                                     "signals": [asdict(rs) for rs in signals]})
            for rs in signals:
                rs.has_media = bool(event.media)
                await self.queue.put(rs)

        await self._client.start()
        log.info("TelethonSource listening on @%s", self.channel)
        # History catch-up: process signals posted before we connected (or read
        # from history) so a management call while the bot was down is not missed.
        # Only messages OLDER than connect time are enqueued; live NewMessage
        # events handle everything newer, avoiding duplicate processing.
        await self._catch_up()

    async def _catch_up(self, limit: int | None = None) -> None:
        limit = limit or int(os.getenv("TG_CATCHUP", "50"))
        if limit <= 0 or self._client is None:
            return
        try:
            entity = await self._client.get_entity(self.channel)
        except Exception as exc:  # noqa: BLE001 - channel resolution must not kill startup
            log.warning("catch-up: could not resolve channel %s (%s) — skipping",
                        self.channel, exc)
            return
        started_at = __import__("time").time()
        seen = 0
        try:
            async for msg in self._client.iter_messages(entity, limit=limit):
                # Only process messages that arrived before this session started.
                if msg.date is not None and msg.date.timestamp() >= started_at:
                    continue
                text = msg.message or (msg.text or "")
                if not text.strip():
                    continue
                if self.trace_store is not None:
                    await asyncio.to_thread(
                        self.trace_store.record_received,
                        f"{self.channel}:{int(msg.id)}", int(msg.id), self.channel,
                        text, bool(msg.media),
                        source_timestamp=msg.date.isoformat() if msg.date else None,
                        reply_to_message_id=getattr(msg, "reply_to_msg_id", None),
                    )
                for rs in await parse_with_fallback(text, message_id=msg.id,
                                        ts_ms=int(msg.date.timestamp() * 1000) if msg.date else None,
                                        channel=self.channel, symbol_map=self.symbol_map,
                                        llm=self.llm):
                    await self._trace_stage(msg.id, "PARSER", "OK", "", {
                        "signal": asdict(rs),
                        "llm_used": bool((rs.raw or {}).get("llm")),
                        "historical": True,
                    })
                    # Catch-up exists to recover MISSED MANAGEMENT calls (sl/close/tp)
                    # posted while the bot was down. Replaying old ENTRY signals would
                    # open stale positions, so only management is processed here.
                    if not rs.is_management:
                        if self.trace_store is not None:
                            await asyncio.to_thread(
                                self.trace_store.record_stage,
                                f"{self.channel}:{int(msg.id)}", int(msg.id),
                                "TERMINAL", "REJECTED", "CATCHUP_ENTRY_NOT_REPLAYED",
                                {"historical": True},
                            )
                        continue
                    rs.has_media = bool(msg.media)
                    await self.queue.put(rs)
                    seen += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("catch-up scan failed (%s) — continuing with live only", exc)
            return
        if seen:
            log.info("catch-up enqueued %d historical signal(s)", seen)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
            self._client = None

    def subscribe(self) -> asyncio.Queue:
        return self.queue
