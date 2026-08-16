"""Signal sources: FakeSignalSource (tests) + TelethonSource (live @fattyfatclub).

``TelethonSource`` imports telethon lazily so the fakes-only test suite never needs
the dependency or a live session file.
"""

from __future__ import annotations

import asyncio
import logging
import os

from src.domain.model import RawSignal
from src.interfaces.ports import SignalSource
from src.sources.parser import SymbolMap, parse_message

log = logging.getLogger(__name__)


class FakeSignalSource(SignalSource):
    """Replays canned messages into the pipeline queue. Zero network."""

    def __init__(self, messages: list[str] | None = None,
                 symbol_map: SymbolMap | None = None) -> None:
        self.messages = list(messages or [])
        self.symbol_map = symbol_map
        self.queue: asyncio.Queue = asyncio.Queue()
        self.started = False

    async def start(self) -> None:
        self.started = True
        for i, text in enumerate(self.messages, start=1):
            await self.queue.put(parse_message(text, message_id=i,
                                               symbol_map=self.symbol_map))

    async def stop(self) -> None:
        self.started = False

    def subscribe(self) -> asyncio.Queue:
        return self.queue

    async def push(self, text: str, message_id: int | None = None) -> RawSignal:
        rs = parse_message(text, message_id=message_id, symbol_map=self.symbol_map)
        await self.queue.put(rs)
        return rs


class TelethonSource(SignalSource):
    """Live Telegram user-session listener for @fattyfatclub.

    Requires TG_API_ID / TG_API_HASH / TG_SESSION. Without a pre-authorized session
    this cannot run headlessly — that is a hard blocker, documented in BLOCKED.md
    rather than worked around.
    """

    def __init__(self, channel: str | None = None, session: str | None = None,
                 api_id: int | None = None, api_hash: str | None = None,
                 symbol_map: SymbolMap | None = None) -> None:
        self.channel = channel or os.getenv("TG_CHANNEL", "fattyfatclub")
        self.session = session or os.getenv("TG_SESSION", "")
        self.api_id = api_id or int(os.getenv("TG_API_ID") or 0)
        self.api_hash = api_hash or os.getenv("TG_API_HASH", "")
        self.symbol_map = symbol_map
        self.queue: asyncio.Queue = asyncio.Queue()
        self._client = None

    def _require_creds(self) -> None:
        missing = [n for n, v in (("TG_API_ID", self.api_id),
                                  ("TG_API_HASH", self.api_hash),
                                  ("TG_SESSION", self.session)) if not v]
        if missing:
            raise RuntimeError(
                "TelethonSource cannot start — missing " + ", ".join(missing) +
                ". A live Telethon session must be created interactively; see BLOCKED.md.")

    async def start(self) -> None:
        self._require_creds()
        from telethon import TelegramClient, events   # lazy: tests never import telethon

        self._client = TelegramClient(self.session, self.api_id, self.api_hash)

        @self._client.on(events.NewMessage(chats=self.channel))
        async def _handler(event):    # pragma: no cover - requires a live session
            rs = parse_message(event.raw_text, message_id=event.id,
                               ts_ms=int(event.date.timestamp() * 1000),
                               channel=self.channel, symbol_map=self.symbol_map)
            rs.has_media = bool(event.media)
            await self.queue.put(rs)

        await self._client.start()
        log.info("TelethonSource listening on @%s", self.channel)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
            self._client = None

    def subscribe(self) -> asyncio.Queue:
        return self.queue
