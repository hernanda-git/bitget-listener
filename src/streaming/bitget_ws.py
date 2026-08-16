"""BitgetWS — Bitget V2 mix WebSocket client (public marks + private user events).

Shape adapted from mexc-listener's ``MexcWS`` (login → subscribe → heartbeat →
jittered reconnect → callbacks), retargeted to the transport **VERIFIED** by
``scripts/bitget_api_probe.py``:

- host ``wss://ws.bitget.com/v2/ws/public`` / ``/v2/ws/private``
  (the analysis doc's ``/mix/v1/stream`` returns HTTP 404 — it is dead);
- ``args`` are OBJECTS ``{"instType","channel","instId"}``, not ``"tickers.SYM"`` strings;
- channel name is ``ticker`` (singular);
- heartbeat is the LITERAL TEXT ``ping`` → server replies literal ``pong`` (not JSON).

Callbacks (all optional) let the orchestrator stay transport-agnostic:
``on_fill`` carries the REAL fee (BUG A), ``on_position`` the REAL liquidation
price (BUG B).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from typing import Awaitable, Callable

import websockets

from src.domain.model import Fill, Instrument, Position, Side
from src.interfaces.ports import MarketStream

log = logging.getLogger(__name__)

Callback = Callable[..., Awaitable[None]] | Callable[..., None]


class BitgetWS(MarketStream):
    """Single-purpose WS loop. One instance per stream kind (public or private)."""

    PUBLIC_URL = "wss://ws.bitget.com/v2/ws/public"
    PRIVATE_URL = "wss://ws.bitget.com/v2/ws/private"

    def __init__(self, symbols: list[str] | None = None, auth=None,
                 product_type: str = "SUSDT-FUTURES", private: bool = False,
                 url: str | None = None, ping_interval: float = 25.0,
                 max_backoff: float = 30.0, connector=None) -> None:
        self.symbols = [s.upper() for s in (symbols or [])]
        self.auth = auth
        self.product_type = product_type
        self.private = private
        self.url = url or (self.PRIVATE_URL if private else self.PUBLIC_URL)
        self.ping_interval = ping_interval
        self.max_backoff = max_backoff
        self._connector = connector or websockets.connect

        self._marks: dict[str, float] = {}
        self._task: asyncio.Task | None = None
        self._ws = None
        self._running = False
        self.last_msg_ts: float = 0.0
        self.connected = False
        self.reconnects = 0

        self.on_fill: Callback | None = None
        self.on_position: Callback | None = None
        self.on_wallet: Callback | None = None
        self.on_tick: Callback | None = None
        self.on_ready: Callback | None = None
        self.on_lost: Callback | None = None

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        self.connected = False

    def mark_price(self, symbol: str) -> float | None:
        return self._marks.get(symbol.upper())

    def is_stale(self, ttl: float = 60.0) -> bool:
        """Dead-man's-switch input: True when no frame arrived within ``ttl`` seconds."""
        if not self.last_msg_ts:
            return True
        return (time.time() - self.last_msg_ts) > ttl

    # ------------------------------------------------------------------- frames
    def subscribe_frame(self) -> dict:
        """The VERIFIED subscribe frame (object args, channel 'ticker')."""
        if self.private:
            args = [{"instType": self.product_type, "channel": ch, "coin": "default"}
                    for ch in ("orders", "positions", "account")]
        else:
            args = [{"instType": self.product_type, "channel": "ticker", "instId": s}
                    for s in self.symbols]
        return {"op": "subscribe", "args": args}

    def login_frame(self) -> dict:
        if self.auth is None:
            raise ValueError("private stream requires auth")
        return {"op": "login", "args": [self.auth.ws_login_arg()]}

    # --------------------------------------------------------------------- loop
    async def _run_forever(self) -> None:
        attempt = 0
        while self._running:
            try:
                await self._session()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                attempt += 1
                self.reconnects += 1
                delay = min(self.max_backoff, (2 ** min(attempt, 5))) * (0.5 + random.random())
                log.warning("WS %s disconnected (%s: %s) — reconnect in %.1fs",
                            self.url, type(exc).__name__, exc, delay)
                await self._emit(self.on_lost, str(exc))
                if not self._running:
                    return
                await asyncio.sleep(delay)

    async def _session(self) -> None:
        async with self._connector(self.url, open_timeout=15) as ws:
            self._ws = ws
            if self.private:
                await ws.send(json.dumps(self.login_frame()))
                raw = await asyncio.wait_for(ws.recv(), timeout=15)
                resp = self._loads(raw)
                if isinstance(resp, dict) and resp.get("event") == "error":
                    raise RuntimeError(f"WS login rejected: {resp}")
            await ws.send(json.dumps(self.subscribe_frame()))
            self.connected = True
            self.last_msg_ts = time.time()
            await self._emit(self.on_ready)
            hb = asyncio.create_task(self._heartbeat(ws))
            try:
                async for raw in ws:
                    self.last_msg_ts = time.time()
                    await self._dispatch(raw)
            finally:
                hb.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await hb
                self.connected = False
                self._ws = None

    async def _heartbeat(self, ws) -> None:
        """Bitget wants the LITERAL string 'ping' (verified) — not a JSON frame."""
        while True:
            await asyncio.sleep(self.ping_interval)
            await ws.send("ping")

    @staticmethod
    def _loads(raw) -> object:
        if isinstance(raw, bytes):
            raw = raw.decode()
        if str(raw).strip() in ("pong", "ping"):
            return {"event": "pong"}
        try:
            return json.loads(raw)
        except Exception:
            return {"event": "unparsed", "raw": str(raw)[:200]}

    async def _dispatch(self, raw) -> None:
        msg = self._loads(raw)
        if not isinstance(msg, dict):
            return
        if msg.get("event") in ("pong", "subscribe", "login"):
            return
        if msg.get("event") == "error":
            log.error("WS error frame: %s", msg)
            return
        arg = msg.get("arg") or {}
        channel = arg.get("channel")
        data = msg.get("data") or []
        if channel == "ticker":
            for row in data:
                sym = str(row.get("instId") or "").upper()
                px = row.get("markPrice") or row.get("lastPr")
                if sym and px:
                    self._marks[sym] = float(px)
                    await self._emit(self.on_tick, sym, float(px))
        elif channel == "orders":
            for row in data:
                fill = self._parse_order_event(row)
                if fill is not None:
                    await self._emit(self.on_fill, fill)
        elif channel == "positions":
            for row in data:
                await self._emit(self.on_position, self._parse_position_event(row))
        elif channel == "account":
            for row in data:
                await self._emit(self.on_wallet, row.get("marginCoin"),
                                 float(row.get("available") or 0))

    # -------------------------------------------------------------- WS parsing
    @staticmethod
    def _parse_order_event(row: dict) -> Fill | None:
        """Only emit a Fill for real executions; carry the REAL fee (BUG A)."""
        status = str(row.get("status", "")).lower()
        if status not in ("partially_filled", "filled", "full_fill", "partial_fill"):
            return None
        filled = float(row.get("baseVolume") or row.get("accBaseVolume") or 0)
        if filled <= 0:
            return None
        fee = 0.0
        for d in row.get("feeDetail") or []:
            fee += abs(float(d.get("totalFee") or d.get("fee") or 0))
        if not row.get("feeDetail") and row.get("fee") is not None:
            fee = abs(float(row["fee"]))
        return Fill(
            client_order_id=str(row.get("clientOid") or ""),
            price=float(row.get("priceAvg") or row.get("fillPrice") or row.get("price") or 0),
            qty=filled,
            side=str(row.get("side", "")).upper(),
            fee=fee,
            fee_asset=str(row.get("feeCoin") or "USDT"),
            ts_ms=int(row.get("uTime") or row.get("cTime") or 0),
            is_reduce=str(row.get("tradeSide", "")).lower() == "close"
            or str(row.get("reduceOnly", "")).upper() in ("YES", "TRUE"),
            exec_type=str(row.get("tradeScope") or "normal"),
            order_id=str(row.get("orderId") or ""),
            symbol=str(row.get("instId") or row.get("symbol") or "").upper(),
        )

    @staticmethod
    def _parse_position_event(row: dict) -> Position:
        """BUG B: keep the REAL liquidation price; absent -> None, never 0.00."""
        liq_raw = row.get("liquidationPrice")
        liq = float(liq_raw) if liq_raw not in (None, "", "0", "0.0") else None
        hold = str(row.get("holdSide", "long")).lower()
        return Position(
            instrument=Instrument.from_token(str(row.get("instId") or row.get("symbol") or "")),
            side=Side.BUY if hold == "long" else Side.SELL,
            size=float(row.get("total") or 0),
            entry_price=float(row.get("openPriceAvg") or 0),
            leverage=int(float(row.get("leverage") or 1)),
            liq_price=liq,
            unrealized_pnl=float(row.get("unrealizedPL") or 0),
            margin_mode=str(row.get("marginMode") or "isolated"),
            updated_at_ms=int(row.get("uTime") or 0) or None,
        )

    @staticmethod
    async def _emit(cb: Callback | None, *args) -> None:
        if cb is None:
            return
        res = cb(*args)
        if asyncio.iscoroutine(res):
            await res
