"""TelegramNotifier + command router — the operator surface.

Trade alerts and the ``/status``, ``/toggle``, ``/live <token>``, ``/positions``,
``/close``, ``/pairs`` commands. The notifier is a thin adapter: every command
delegates to the services, so the router itself holds no trading logic.

Authorization: only ``TG_OPERATOR_ID`` may run state-changing commands, and
``/live`` additionally requires a gate token.

A single module-level httpx client is shared by the notifier and the command
poller, so we never open a new TLS connection per outgoing message. The notifier
also supports an async context manager (``async with TelegramNotifier() as n``)
for callers that want explicit lifecycle control.
"""

from __future__ import annotations

import logging
import os

import httpx

from src.domain.model import Instrument, Position
from src.interfaces.ports import Notifier

log = logging.getLogger(__name__)

# Shared, lazily-created client. Reused across alerts and the command poller.
_CLIENT: httpx.AsyncClient | None = None


async def get_client() -> httpx.AsyncClient:
    global _CLIENT
    if _CLIENT is None or _CLIENT.is_closed:
        _CLIENT = httpx.AsyncClient(timeout=15)
    return _CLIENT


async def close_client() -> None:
    global _CLIENT
    if _CLIENT is not None and not _CLIENT.is_closed:
        await _CLIENT.aclose()
    _CLIENT = None


class TelegramNotifier(Notifier):
    def __init__(self, token: str | None = None, chat_id: str | None = None,
                 client: httpx.AsyncClient | None = None) -> None:
        self.token = token or os.getenv("TG_BOT_TOKEN", "")
        self.chat_id = chat_id or os.getenv("TG_CHAT_ID", "")
        # Owned client (when no client is injected) is closed on aclose()/exit.
        self._client = client
        self._owns_client = client is None
        self.sent: list[str] = []

    async def __aenter__(self) -> "TelegramNotifier":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def send(self, text: str) -> bool:
        self.sent.append(text)
        if not self.token or not self.chat_id:
            log.info("[notifier:no-credentials] %s", text)
            return False
        client = self._client or await get_client()
        try:
            r = await client.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text,
                      "parse_mode": "HTML", "disable_web_page_preview": True})
            return r.status_code == 200
        except Exception as exc:
            log.error("notifier send failed: %s", exc)
            return False
        finally:
            if self._owns_client:
                await self.aclose()

    # ------------------------------------------------------------------ messages
    @staticmethod
    def format_entry(result: dict) -> str:
        order = result.get("order")
        mode = "PAPER" if result.get("status") == "PAPER" else "LIVE"
        if order is None:
            return f"[{mode}] {result.get('status')}: {result.get('reason','')}"
        return (f"[{mode}] {order.side.value} {order.instrument.symbol}\n"
                f"qty={order.qty} entry={order.price} lev={order.leverage}x\n"
                f"SL={order.sl} TP={order.tp}")

    @staticmethod
    def format_exit(row) -> str:
        net = getattr(row, "net_usd", None)
        fees = getattr(row, "open_fee", 0.0) + getattr(row, "close_fee", 0.0)
        return (f"CLOSED {getattr(row, 'symbol', '?')} @ {getattr(row, 'exit_price', '?')}\n"
                f"net={net:+.4f} USDT (fees {fees:.4f} included)"
                if net is not None else f"CLOSED {getattr(row, 'symbol', '?')}")


class CommandRouter:
    """Routes operator commands. State-changing commands require the operator id."""

    def __init__(self, gate, position_manager, shadow_ledger, ledger=None,
                 operator_id: str | None = None) -> None:
        self.gate = gate
        self.positions = position_manager
        self.shadow = shadow_ledger
        self.ledger = ledger
        self.operator_id = str(operator_id or os.getenv("TG_OPERATOR_ID", ""))

    def authorized(self, sender_id) -> bool:
        if not self.operator_id:
            return False              # fail closed when unconfigured
        return str(sender_id) == self.operator_id

    async def handle(self, text: str, sender_id: str | int = "") -> str:
        parts = text.strip().split()
        if not parts:
            return ""
        cmd, args = parts[0].lower(), parts[1:]

        if cmd in ("/status", "/positions", "/pairs"):     # read-only, open
            return await self._read_only(cmd)

        if not self.authorized(sender_id):
            return "unauthorized — state-changing commands are operator-only"

        if cmd == "/toggle":
            return self.gate.toggle("telegram")
        if cmd == "/paper":
            return self.gate.to_paper("telegram")
        if cmd == "/live":
            if not args:
                return "usage: /live <token>  (run --go-live first)"
            ok, msg = self.gate.redeem(args[0])
            return msg
        if cmd == "/close":
            return await self._close(args)
        if cmd == "/sl_to_entry":
            if not args:
                return "usage: /sl_to_entry <SYMBOL>"
            return str(await self.positions.sl_to_entry(args[0].upper()))
        return f"unknown command: {cmd}"

    async def _close(self, args: list[str]) -> str:
        if not args:
            return "usage: /close <SYMBOL>"
        sym = args[0].upper()
        res = await self.positions.full_close(sym, reason="telegram")
        return f"{sym}: {res.get('status')}"

    async def _read_only(self, cmd: str) -> str:
        if cmd == "/status":
            st = self.gate.status()
            lines = [f"mode: {st['mode']}",
                     f"open positions: {len(self.positions.positions)}"]
            if self.ledger is not None:
                nets = self.ledger.net_pnls()
                lines.append(f"closed trades: {len(nets)}  net: {sum(nets):+.4f} USDT")
                lines.append(f"fees paid: {self.ledger.total_fees():.4f} USDT")
            if st["unmet"]:
                lines.append("go-live blocked by:")
                lines += [f"  - {u}" for u in st["unmet"]]
            return "\n".join(lines)
        if cmd == "/positions":
            if not self.positions.positions:
                return "no open positions"
            return "\n".join(
                f"{p.instrument.symbol} {p.side.value} size={p.size} entry={p.entry_price} "
                f"sl={p.sl_price} liq={p.liq_price}"
                for p in self.positions.positions.values())
        if cmd == "/pairs":
            weak = self.shadow.weak_pairs()
            return ("flagged pairs: " + ", ".join(weak)) if weak else "no flagged pairs"
        return ""
