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

from src.domain.model import Instrument, Position, Side, Signal
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

    # Single source of truth for the menu, /help and the BotFather registration.
    # (command, args, description, state_changing)
    COMMANDS: list[tuple[str, str, str, bool]] = [
        ("help", "", "show this command list", False),
        ("status", "", "mode, positions, PnL, gate blockers", False),
        ("health", "", "liveness: mode, deadman, breaker, command channel", False),
        ("balance", "", "futures wallet + available margin", False),
        ("positions", "", "open positions with unrealized PnL", False),
        ("pnl", "", "unrealized + daily realized + lifetime net", False),
        ("activity", "[n]", "recent closed trades (default 5)", False),
        ("pairs", "", "pairs flagged weak by the shadow ledger", False),
        ("open", "<SYM> <BUY|SELL> <sl> [tp] [usd]", "manually open a position", True),
        ("close", "<SYM|all>", "market-close a position", True),
        ("sl_to_entry", "<SYM>", "move stop-loss to break-even", True),
        ("toggle", "", "flip PAPER/LIVE (gate-guarded)", True),
        ("paper", "", "force PAPER mode immediately", True),
        ("live", "<token>", "redeem a go-live token", True),
    ]

    def __init__(self, gate, position_manager, shadow_ledger, ledger=None,
                 operator_id: str | None = None, orchestrator=None) -> None:
        self.gate = gate
        self.positions = position_manager
        self.shadow = shadow_ledger
        self.ledger = ledger
        self.orch = orchestrator
        self.operator_id = str(operator_id or os.getenv("TG_OPERATOR_ID", ""))

    def authorized(self, sender_id) -> bool:
        if not self.operator_id:
            return False              # fail closed when unconfigured
        return str(sender_id) == self.operator_id

    # ------------------------------------------------------------------- helpers
    @property
    def _adapter(self):
        return getattr(self.positions, "adapter", None)

    def help_text(self, authorized: bool = True) -> str:
        """The menu shown for /help and for a bare '/'."""
        read = [c for c in self.COMMANDS if not c[3]]
        write = [c for c in self.COMMANDS if c[3]]

        def fmt(rows):
            out = []
            for name, args, desc, _ in rows:
                usage = f"/{name} {args}".strip()
                out.append(f"<code>{usage}</code> — {desc}")
            return out

        lines = ["<b>bitget-listener commands</b>", "", "<b>Read-only</b>"]
        lines += fmt(read)
        lines += ["", "<b>Operator only</b> (state-changing)"]
        lines += fmt(write)
        if not authorized:
            lines += ["", "⚠️ you are not the configured operator — "
                          "state-changing commands will be refused"]
        return "\n".join(lines)

    async def handle(self, text: str, sender_id: str | int = "") -> str:
        parts = text.strip().split()
        if not parts:
            return ""
        cmd, args = parts[0].lower(), parts[1:]
        # Telegram sends "/cmd@botname" in groups — strip the mention.
        if "@" in cmd:
            cmd = cmd.split("@", 1)[0]

        # A bare "/" (or /help, /start, /commands) lists everything.
        if cmd in ("/", "/help", "/start", "/commands", "/menu"):
            return self.help_text(self.authorized(sender_id))

        if cmd in ("/status", "/positions", "/pairs", "/health",
                   "/balance", "/pnl", "/activity"):     # read-only, open
            return await self._read_only(cmd, args)

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
        if cmd == "/open":
            return await self._open(args)
        if cmd == "/sl_to_entry":
            if not args:
                return "usage: /sl_to_entry <SYMBOL>"
            return str(await self.positions.sl_to_entry(args[0].upper()))
        return f"unknown command: {cmd}\nsend /help for the list"

    async def _close(self, args: list[str]) -> str:
        if not args:
            return "usage: /close <SYMBOL|all>"
        target = args[0].upper()
        if target == "ALL":
            syms = list(self.positions.positions.keys())
            if not syms:
                return "no open positions"
            out = []
            for sym in syms:
                try:
                    res = await self.positions.full_close(sym, reason="telegram")
                    out.append(f"{sym}: {res.get('status')}")
                except Exception as exc:            # report, never abort the rest
                    out.append(f"{sym}: ERROR {type(exc).__name__}: {exc}")
            return "\n".join(out)
        res = await self.positions.full_close(target, reason="telegram")
        return f"{target}: {res.get('status')}"

    async def _open(self, args: list[str]) -> str:
        """Manual entry. SL is MANDATORY — the sizer refuses an entry without one."""
        if len(args) < 3:
            return ("usage: /open <SYMBOL> <BUY|SELL> <sl> [tp] [usd]\n"
                    "example: <code>/open XRPUSDT BUY 2.10</code>\n"
                    "SL is mandatory (the bot refuses any entry without one).")
        if self.orch is None:
            return "manual open unavailable: orchestrator not wired into the router"

        sym = args[0].upper()
        if not sym.endswith("USDT"):
            sym += "USDT"
        side_txt = args[1].upper()
        if side_txt not in ("BUY", "SELL", "LONG", "SHORT"):
            return "side must be BUY or SELL"
        side = Side.BUY if side_txt in ("BUY", "LONG") else Side.SELL

        try:
            sl = float(args[2])
            tp = float(args[3]) if len(args) > 3 and args[3] != "-" else None
            notional = float(args[4]) if len(args) > 4 else 0.0
        except ValueError:
            return "sl / tp / usd must be numbers"
        if sl <= 0:
            return "sl must be > 0"

        inst = Instrument.from_token(sym.replace("USDT", ""))
        if self._adapter is None:
            return "no exchange adapter wired"
        try:
            mark = await self._adapter.get_mark_price(inst)
        except Exception as exc:
            return f"could not fetch mark price: {type(exc).__name__}: {exc}"

        # Refuse a stop on the wrong side of the market — this would be an
        # instant-liquidating entry, not a trade.
        if side is Side.BUY and sl >= mark:
            return f"refused: BUY stop {sl} is at/above mark {mark}"
        if side is Side.SELL and sl <= mark:
            return f"refused: SELL stop {sl} is at/below mark {mark}"

        if notional <= 0:
            params = getattr(self.orch, "params", {}) or {}
            notional = float(params.get("risk_per_trade_usd") or 5.0)

        sig = Signal(instrument=inst, side=side, entry=mark, sl=sl, tp=tp,
                     notional_usd=notional, leverage=1,
                     source="telegram-manual", raw={"manual": True})
        try:
            res = await self.orch.orders.open_from_signal(sig, mark_price=mark)
        except Exception as exc:
            return f"open FAILED: {type(exc).__name__}: {exc}"

        status = res.get("status")
        lines = [f"<b>{sym} {side.value} — {status}</b>",
                 f"mark: {mark}  sl: {sl}" + (f"  tp: {tp}" if tp else ""),
                 f"notional: ${notional:.2f}"]
        for key in ("size", "reason", "order_id", "avg_price"):
            if res.get(key) is not None:
                lines.append(f"{key}: {res[key]}")
        if str(status).upper() in ("FILLED", "OK", "PLACED", "OPEN"):
            await self.orch._after_open(sig, res) if hasattr(
                self.orch, "_after_open") else None
        return "\n".join(lines)

    # --------------------------------------------------------------- read-only
    async def _mark(self, symbol: str) -> float | None:
        """Mark price, preferring the WS cache and falling back to REST."""
        stream = getattr(self.positions, "stream", None)
        if stream is not None:
            try:
                px = stream.mark_price(symbol)
                if px:
                    return float(px)
            except Exception:
                pass
        try:
            inst = Instrument.from_token(symbol.replace("USDT", ""))
            if self._adapter is None:
                return None
            return float(await self._adapter.get_mark_price(inst))
        except Exception:
            return None

    async def _upnl_rows(self) -> tuple[list[str], float]:
        """Per-position lines plus the summed unrealized PnL."""
        rows, total = [], 0.0
        for p in self.positions.positions.values():
            sym = p.instrument.symbol
            # Trust the venue's own unrealizedPL when present (BUG B discipline:
            # never invent a number); otherwise derive it from the mark.
            upnl = p.unrealized_pnl
            mark = await self._mark(sym)
            if not upnl and mark and p.entry_price:
                sign = 1 if p.side is Side.BUY else -1
                upnl = (mark - p.entry_price) * p.size * sign
            total += upnl or 0.0
            liq = p.liq_price if p.liq_price is not None else "n/a"
            rows.append(
                f"<b>{sym}</b> {p.side.value} size={p.size}\n"
                f"  entry={p.entry_price}  mark={mark if mark else 'n/a'}\n"
                f"  sl={p.sl_price}  tp={p.tp_price}  liq={liq}\n"
                f"  uPnL: {upnl:+.4f} USDT")
        return rows, total

    async def _read_only(self, cmd: str, args: list[str] | None = None) -> str:
        args = args or []

        if cmd == "/status":
            st = self.gate.status()
            lines = [f"mode: {st['mode']}",
                     f"open positions: {len(self.positions.positions)}"]
            if self.ledger is not None:
                nets = self.ledger.net_pnls()
                lines.append(f"closed trades: {len(nets)}  net: {sum(nets):+.4f} USDT")
                lines.append(f"fees paid: {self.ledger.total_fees():.4f} USDT")
            if self.orch is not None:
                lines.append(f"daily realized: {self.orch.daily_realized:+.4f} USDT")
            if st["unmet"]:
                lines.append("go-live blocked by:")
                lines += [f"  - {u}" for u in st["unmet"]]
            return "\n".join(lines)

        if cmd == "/health":
            from src.notifier import bot_poller
            st = self.gate.status()
            deadman = getattr(self.orch, "deadman_halted", None)
            if callable(deadman):
                deadman = deadman()
            breaker = None
            if self._adapter is not None and hasattr(self._adapter, "rate_limit"):
                try:
                    breaker = self._adapter.rate_limit.is_open()
                except Exception:
                    breaker = None
            ok = not any([deadman is True, breaker is True,
                          bot_poller.tg_cmd_degraded()])
            lines = [f"{'✅' if ok else '⚠️'} <b>{'healthy' if ok else 'DEGRADED'}</b>",
                     f"mode: {st['mode']}",
                     f"deadman halted: {deadman}",
                     f"rate-limit breaker open: {breaker}",
                     f"command channel degraded: {bot_poller.tg_cmd_degraded()}"]
            if bot_poller.tg_cmd_consecutive_errors:
                lines.append(f"  consecutive poll errors: "
                             f"{bot_poller.tg_cmd_consecutive_errors}")
            lines.append(f"open positions: {len(self.positions.positions)}")
            return "\n".join(lines)

        if cmd == "/balance":
            if self._adapter is None:
                return "no exchange adapter wired"
            try:
                avail = await self._adapter.get_balance_usdt()
            except Exception as exc:
                return f"balance unavailable: {type(exc).__name__}: {exc}"
            coin = getattr(self._adapter, "margin_coin", "USDT")
            ptype = getattr(self._adapter, "product_type", "?")
            _, upnl = await self._upnl_rows()
            lines = [f"<b>{coin} futures wallet</b>  ({ptype})",
                     f"available: {avail:.6f} {coin}"]
            if self.positions.positions:
                lines.append(f"unrealized PnL: {upnl:+.4f} {coin}")
                lines.append(f"equity (approx): {avail + upnl:.6f} {coin}")
            return "\n".join(lines)

        if cmd == "/positions":
            if not self.positions.positions:
                return "no open positions"
            rows, total = await self._upnl_rows()
            return "\n".join(rows + [f"\n<b>total uPnL: {total:+.4f} USDT</b>"])

        if cmd == "/pnl":
            rows, upnl = await self._upnl_rows()
            lines = ["<b>PnL</b>"]
            lines.append(f"unrealized (open): {upnl:+.4f} USDT"
                         if self.positions.positions else
                         "unrealized (open): 0.0000 USDT (no positions)")
            if self.orch is not None:
                cap = None
                params = getattr(self.orch, "params", {}) or {}
                if params.get("max_daily_loss_pct") is not None:
                    cap = params.get("max_daily_loss_pct")
                lines.append(f"daily realized: {self.orch.daily_realized:+.4f} USDT")
                if cap is not None:
                    lines.append(f"  daily-loss cap: {cap}%")
                breached = getattr(self.orch, "daily_loss_breached", None)
                if callable(breached):
                    try:
                        if breached():
                            lines.append("  ⚠️ DAILY LOSS CAP BREACHED — entries halted")
                    except Exception:
                        pass
            if self.ledger is not None:
                nets = self.ledger.net_pnls()
                lines.append(f"lifetime net: {sum(nets):+.4f} USDT "
                             f"over {len(nets)} closed trades")
                lines.append(f"fees paid: {self.ledger.total_fees():.4f} USDT")
            return "\n".join(lines)

        if cmd == "/activity":
            if self.ledger is None:
                return "no ledger wired"
            try:
                limit = max(1, min(20, int(args[0]))) if args else 5
            except ValueError:
                limit = 5
            try:
                trades = self.ledger.closed_trades(limit=200)
            except Exception as exc:
                return f"ledger unavailable: {type(exc).__name__}: {exc}"
            if not trades:
                return "no closed trades yet"
            recent = trades[-limit:][::-1]
            lines = [f"<b>last {len(recent)} closed trades</b>"]
            for t in recent:
                sym = t.get("symbol") or t.get("instrument") or "?"
                net = t.get("net_usd")
                fee = t.get("fees_usd") or t.get("fee_usd")
                when = t.get("closed_at") or t.get("ts") or ""
                mark = "🟢" if (net or 0) >= 0 else "🔴"
                seg = [f"{mark} <b>{sym}</b>"]
                if net is not None:
                    seg.append(f"net {float(net):+.4f}")
                if fee is not None:
                    seg.append(f"fee {float(fee):.4f}")
                if t.get("reason"):
                    seg.append(str(t["reason"]))
                lines.append("  ".join(seg) + (f"\n  <i>{when}</i>" if when else ""))
            return "\n".join(lines)

        if cmd == "/pairs":
            weak = self.shadow.weak_pairs()
            return ("flagged pairs: " + ", ".join(weak)) if weak else "no flagged pairs"
        return ""
