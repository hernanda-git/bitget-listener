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
import asyncio
import contextlib
from datetime import datetime

import httpx

from src.domain.model import Instrument, Position, Side, Signal
from src.interfaces.ports import Notifier
from src.notifier import fmt

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

    # ----------------------------------------------------------------- event push
    # Realtime alerts for the five lifecycle moments the operator asked for:
    # signal triggered, position opened, TP hit, manual TP triggered, SL hit
    # (plus liquidation as a safety-case of SL). Each is a distinct, scannable card.
    @staticmethod
    def format_signal(signal) -> str:
        side = getattr(signal, "side", None)
        side_v = side.value if side is not None else "?"
        sym = getattr(getattr(signal, "instrument", None), "symbol", "?")
        glyph = "▲" if str(side_v).upper() in ("BUY", "LONG") else "▼"
        rows = [
            f"{glyph} <b>{sym}</b>  {side_v}  · signal received",
            fmt.plain_row("Entry", fmt.price(getattr(signal, "entry", None))),
            fmt.plain_row("Stop", fmt.price(getattr(signal, "sl", None))),
        ]
        tp = getattr(signal, "tp", None)
        if tp is not None:
            rows.append(fmt.plain_row("Target", fmt.price(tp)))
        notional = getattr(signal, "notional_usd", None)
        if notional:
            rows.append(fmt.plain_row("Plan size", f"${notional:.2f}"))
        return fmt.join(fmt.title("Signal triggered", "📡"), fmt.lines(*rows))

    @staticmethod
    def format_open(result: dict) -> str:
        order = result.get("order")
        if order is None:
            return ""
        side_v = getattr(order.side, "value", "?")
        glyph = "▲" if str(side_v).upper() in ("BUY", "LONG") else "▼"
        mode = "PAPER" if result.get("status") == "PAPER" else "LIVE"
        rows = [
            f"{glyph} <b>{order.instrument.symbol}</b>  {side_v}  · position opened ({mode})",
            fmt.plain_row("Entry", fmt.price(order.price)),
            fmt.plain_row("Size", fmt.qty(order.qty)),
            fmt.plain_row("Stop", fmt.price(order.sl)),
        ]
        if order.tp is not None:
            rows.append(fmt.plain_row("Target", fmt.price(order.tp)))
        if order.leverage:
            rows.append(fmt.plain_row("Leverage", f"{order.leverage}x"))
        return fmt.join(fmt.title("Position opened", "🟢"), fmt.lines(*rows))

    @staticmethod
    def format_report(data: dict) -> str:
        """Operator-friendly periodic health/state report."""
        mode = data.get("mode", "?")
        healthy = data.get("healthy", False)
        title = fmt.title("Bitget periodic report", "✅" if healthy else "⚠️")
        rails = fmt.lines(
            fmt.plain_row("Mode", f"<b>{mode}</b>"),
            fmt.plain_row("Exchange positions", str(data.get("exchange_positions", 0))),
            fmt.plain_row("Ledger open rows", str(data.get("ledger_open_rows", 0))),
            fmt.plain_row("Available", f"<b>{fmt.money(data.get('available'), 4, signed=False)} USDT</b>"),
            fmt.plain_row("Lifetime net", f"{fmt.money(data.get('net'), 4)} USDT"),
            fmt.plain_row("Fees", f"{fmt.money(data.get('fees'), 4, signed=False)} USDT"),
            fmt.plain_row("Deadman", "halted" if data.get("deadman") else "normal"),
            fmt.plain_row("Rate limit", "tripped" if data.get("breaker") else "normal"),
            fmt.plain_row("Telegram commands", "degraded" if data.get("tg_degraded") else "normal"),
        )
        blocks = [title, rails]
        positions = data.get("positions") or []
        if positions:
            blocks.append(fmt.lines("📈 <b>Positions</b>",
                                    fmt.plain_row("Open", ", ".join(positions))))
        if data.get("unmet"):
            blocks.append(fmt.lines("🔒 <b>Gate evidence still missing</b>",
                                    *[f"{fmt.BULLET} {x}" for x in data["unmet"]]))
        return fmt.join(*blocks)

    @classmethod
    def _exit_card(cls, reason: str, row, glyph: str, head: str) -> str:
        sym = getattr(row, "symbol", "?")
        net = getattr(row, "net_usd", None)
        ex = getattr(row, "exit_price", None)
        er = getattr(row, "exit_reason", "") or reason
        rows = [f"{glyph} <b>{sym}</b>  · {head}"]
        rows.append(fmt.plain_row("Exit", fmt.price(ex)))
        rows.append(fmt.plain_row("Net", fmt.money(net, 4) + " USDT"))
        if er and er != "full_close":
            rows.append(fmt.plain_row("Reason", er))
        fees = getattr(row, "open_fee", 0.0) + getattr(row, "close_fee", 0.0)
        rows.append(fmt.plain_row("Fees", fmt.money(fees, 4, signed=False) + " USDT"))
        return fmt.join(fmt.title(head, glyph), fmt.lines(*rows))

    @staticmethod
    def format_tp_hit(row) -> str:
        return TelegramNotifier._exit_card("target_hit", row, "🎯", "TP hit")

    @staticmethod
    def format_sl_hit(row) -> str:
        return TelegramNotifier._exit_card("stop_breached", row, "🛑", "SL hit")

    @staticmethod
    def format_manual_tp(row) -> str:
        return TelegramNotifier._exit_card("manual", row, "✋", "Manual close")

    @staticmethod
    def format_liquidation(row) -> str:
        return TelegramNotifier._exit_card("liquidation", row, "💥", "Liquidation")

    # ----------------------------------------------------- management / system events
    @staticmethod
    def format_partial(row, fraction: float = 0.5) -> str:
        sym = getattr(row, "symbol", "?")
        ex = getattr(row, "exit_price", None)
        net = getattr(row, "net_usd", None)
        pct = int(round(fraction * 100))
        rows = [
            fmt.row("Symbol", sym),
            fmt.row("Booked at", fmt.price(ex)),
        ]
        if net is not None:
            rows.append(fmt.row("Booked PnL", fmt.money(net, 4) + " USDT"))
        rows.append(fmt.row("Remaining", f"{100 - pct}% still running"))
        rows.append(fmt.hint("⏱ " + fmt.when(datetime.now())))
        return fmt.join(
            fmt.title("TP1 booked", "🔰"),
            fmt.rule(),
            fmt.lines(*rows))

    @staticmethod
    def format_breakeven(symbol: str, sl: float | None) -> str:
        rows = [
            fmt.row("Symbol", symbol),
            fmt.row("New SL", fmt.price(sl)),
            fmt.hint("⏱ " + fmt.when(datetime.now())),
        ]
        return fmt.join(
            fmt.title("Break-even", "🔒"),
            fmt.rule(),
            fmt.lines(*rows))

    @staticmethod
    def format_stop_modify(symbol: str, sl: float | None) -> str:
        rows = [
            fmt.row("Symbol", symbol),
            fmt.row("New SL", fmt.price(sl)),
            fmt.hint("⏱ " + fmt.when(datetime.now())),
        ]
        return fmt.join(
            fmt.title("Stop updated", "🛡"),
            fmt.rule(),
            fmt.lines(*rows))

    @staticmethod
    def format_tp_modify(symbol: str, tp: float | None) -> str:
        rows = [
            fmt.row("Symbol", symbol),
            fmt.row("New TP", fmt.price(tp)),
            fmt.hint("⏱ " + fmt.when(datetime.now())),
        ]
        return fmt.join(
            fmt.title("Target updated", "🎯"),
            fmt.rule(),
            fmt.lines(*rows))

    @staticmethod
    def format_skipped(reason: str, rs=None, symbol: str | None = None) -> str:
        sym = symbol or (getattr(rs, "symbol_usdt", "") or "")
        raw = (getattr(rs, "raw_text", "") or "").strip()
        side = getattr(rs, "side", "") or ""
        action = getattr(rs, "management_action", None) or ""
        mid = getattr(rs, "message_id", None)
        if action:
            kind = f"management · {action}"
        elif side:
            kind = f"entry · {side}"
        else:
            kind = "entry · —"
        rows = [
            fmt.row("Symbol", sym or "—"),
            fmt.row("Type", kind),
            fmt.row("Reason", reason),
        ]
        if mid is not None:
            rows.append(fmt.row("Message", str(mid)))
        rows.append(fmt.row("Raw", raw or "—"))
        rows.append(fmt.hint("⏱ " + fmt.when(datetime.now())))
        return fmt.join(
            fmt.title("Signal skipped", "⏭"),
            fmt.rule(),
            fmt.lines(*rows))

    @staticmethod
    def format_mode_change(mode: str, detail: str = "") -> str:
        glyph = "🧪" if str(mode).upper() == "PAPER" else "🟢"
        rows = [fmt.row("Mode", str(mode).upper())]
        if detail:
            rows.append(fmt.row("Note", detail))
        rows.append(fmt.hint("⏱ " + fmt.when(datetime.now())))
        return fmt.join(
            fmt.title("Mode change", glyph),
            fmt.rule(),
            fmt.lines(*rows))

    @staticmethod
    def format_rate_limit_parked(remaining_s: float) -> str:
        rows = [
            fmt.row("State", "new entries parked to avoid IP ban"),
            fmt.row("Recheck", f"{remaining_s:.0f}s"),
            fmt.hint("⏱ " + fmt.when(datetime.now())),
        ]
        return fmt.join(
            fmt.title("Rate-limit park", "⏸"),
            fmt.rule(),
            fmt.lines(*rows))

    @staticmethod
    def format_catchup(symbol: str, action: str) -> str:
        rows = [
            fmt.row("Symbol", symbol),
            fmt.row("Action", action),
            fmt.hint("⏱ " + fmt.when(datetime.now())),
        ]
        return fmt.join(
            fmt.title("Catch-up", "🔄"),
            fmt.rule(),
            fmt.lines(*rows))


class CommandRouter:
    """Routes operator commands. State-changing commands require the operator id."""

    # Single source of truth for the menu, /help and the BotFather registration.
    # (command, args, description, state_changing, group)
    COMMANDS: list[tuple[str, str, str, bool, str]] = [
        ("bitget_status", "", "position, PnL and mode overview", False, "Monitor"),
        ("bitget_positions", "", "open positions with live PnL", False, "Monitor"),
        ("bitget_pnl", "", "unrealized, daily and lifetime PnL", False, "Monitor"),
        ("bitget_balance", "", "wallet, margin and equity", False, "Monitor"),
        ("bitget_activity", "[n]", "recent closed trades", False, "Monitor"),
        ("bitget_health", "", "system liveness and safety rails", False, "Monitor"),
        ("bitget_price", "<SYMBOL>", "live mark price", False, "Monitor"),
        ("bitget_limit_orders", "", "pending limit orders", False, "Monitor"),
        ("bitget_pairs", "", "pairs flagged weak by the shadow ledger", False, "Monitor"),
        ("bitget_open", "<SYM> <BUY|SELL> <sl> [tp] [usd]",
         "open a position manually", True, "Trade"),
        ("bitget_close", "<SYM|all>", "market-close a position", True, "Trade"),
        ("bitget_sl_to_entry", "<SYM>", "move stop-loss to break-even", True, "Trade"),
        ("bitget_toggle", "", "switch between paper and live", True, "Control"),
        ("bitget_highrisk", "[on|off]", "toggle high-risk sizing profile", True, "Control"),
        ("bitget_paper", "", "return to paper mode immediately", True, "Control"),
        ("bitget_live", "<token>", "redeem a go-live token", True, "Control"),
        ("bitget_help", "", "show this menu", False, "Monitor"),
    ]

    # Order the groups appear in the menu.
    GROUP_ORDER = ("Monitor", "Trade", "Control")
    GROUP_GLYPH = {"Monitor": "📊", "Trade": "⚡", "Control": "🔐"}

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

    def _push_mode(self, detail: str) -> None:
        """Realtime push when the trading mode changes (toggle/paper/live)."""
        if self.orch is not None and getattr(self.orch, "notifier", None) is not None:
            mode = "LIVE" if "LIVE" in detail.upper() else (
                "PAPER" if "PAPER" in detail.upper() else "")
            with contextlib.suppress(Exception):
                asyncio.create_task(
                    self.orch.notifier.send(
                        TelegramNotifier.format_mode_change(mode or "?", detail)))

    # ------------------------------------------------------------------- helpers
    @property
    def _adapter(self):
        return getattr(self.positions, "adapter", None)

    def help_text(self, authorized: bool = True) -> str:
        """The menu shown for /help and for a bare '/'."""
        blocks = [fmt.title("Bitget Listener", "🤖")]
        for group in self.GROUP_ORDER:
            rows = [c for c in self.COMMANDS if c[4] == group]
            if not rows:
                continue
            glyph = self.GROUP_GLYPH.get(group, "")
            locked = " · operator only" if rows[0][3] else ""
            section = [f"{glyph} <b>{group}</b>{locked}"]
            for name, args, desc, _sc, _g in rows:
                usage = f"/{name} {args}".strip()
                section.append(f"<code>{usage}</code>\n{fmt.BULLET} {desc}")
            blocks.append("\n".join(section))
        if not authorized:
            blocks.append(fmt.hint("You are not the configured operator. "
                                   "Monitoring works; trading commands are refused."))
        return fmt.join(*blocks)

    async def handle(self, text: str, sender_id: str | int = "") -> str:
        parts = text.strip().split()
        if not parts:
            return ""
        cmd, args = parts[0].lower(), parts[1:]
        # Telegram sends "/cmd@botname" in groups — strip the mention.
        if "@" in cmd:
            cmd = cmd.split("@", 1)[0]
        # A bare "/" lists the menu. Then strip the bitget_ namespace.
        if cmd == "/":
            return self.help_text(self.authorized(sender_id))
        if cmd == "/bitget":
            return self.help_text(self.authorized(sender_id))
        if cmd.startswith("/bitget_"):
            action = cmd[len("/bitget_"):]
        else:
            action = cmd.lstrip("/")

        # A bare "/" (or /help, /start, /commands, /menu, /bitget_help) lists everything.
        if action in ("/", "help", "start", "commands", "menu"):
            return self.help_text(self.authorized(sender_id))

        if action in ("status", "positions", "pairs", "health", "price",
                      "limit_orders", "balance", "pnl", "activity"):     # read-only, open
            return await self._read_only("/" + action, args)

        if not self.authorized(sender_id):
            return fmt.join(
                fmt.title("Not authorized", "🔒"),
                fmt.hint("Trading commands are limited to the configured operator. "
                         "Monitoring commands are open — try /bitget_status."))

        if action == "toggle":
            out = str(self.gate.toggle("telegram"))
            self._push_mode(out)
            return fmt.join(fmt.title("Mode change", "🔐"), out)
        if action == "highrisk":
            if self.orch is None or not hasattr(self.orch, "set_high_risk"):
                return fmt.join(fmt.title("Risk profile unavailable", "⚠️"))
            if args and args[0].lower() not in ("on", "off"):
                return fmt.join(fmt.title("Invalid risk profile", "⚠️"),
                                fmt.row("Usage", "/bitget_highrisk [on|off]"))
            current = bool(self.orch.high_risk_enabled())
            enabled = (not current) if not args else args[0].lower() == "on"
            out = self.orch.set_high_risk(enabled)
            return fmt.join(fmt.title("Risk profile changed", "⚠️" if enabled else "✅"),
                            fmt.plain_row("Profile", f"<b>{'HIGH' if enabled else 'NORMAL'}</b>"),
                            fmt.plain_row("Result", out),
                            fmt.hint("Applies to new entries only. Existing positions are unchanged."))
        if action == "paper":
            out = str(self.gate.to_paper("telegram"))
            self._push_mode(out)
            return fmt.join(fmt.title("Paper mode", "🧪"), out)
        if action == "live":
            if not args:
                return fmt.join(fmt.title("Go live", "🔐"),
                                fmt.row("Usage", "/bitget_live <token>"),
                                fmt.hint("Mint a token first, then redeem it here."))
            ok, msg = self.gate.redeem(args[0])
            self._push_mode("LIVE" if ok else f"live rejected: {msg}")
            return fmt.join(fmt.title("Go live" if ok else "Rejected",
                                      "✅" if ok else "⚠️"), str(msg))
        if action == "close":
            return await self._close(args)
        if action == "open":
            return await self._open(args)
        if action == "sl_to_entry":
            if not args:
                return fmt.join(fmt.title("Move stop to entry", "⚡"),
                                fmt.row("Usage", "/bitget_sl_to_entry <SYMBOL>"))
            res = await self.positions.sl_to_entry(args[0].upper())
            return fmt.join(fmt.title("Stop moved to entry", "⚡"), str(res))
        return fmt.join(fmt.title("Unknown command", "❔"),
                        fmt.plain_row("Received", f"<code>{cmd}</code>"),
                        fmt.hint("Send /bitget_help to see everything available."))

    async def _close(self, args: list[str]) -> str:
        if not args:
            return fmt.join(fmt.title("Close a position", "⚡"),
                            fmt.row("Usage", "/bitget_close <SYMBOL>"),
                            fmt.row("Or", "/bitget_close all"))
        target = args[0].upper()
        if target == "ALL":
            syms = list(self.positions.positions.keys())
            if not syms:
                return fmt.join(fmt.title("Nothing to close", "📭"),
                                fmt.hint("The bot is already flat."))
            rows, failures = [], 0
            for sym in syms:
                try:
                    res = await self.positions.full_close(sym, reason="telegram")
                    rows.append(fmt.plain_row(f"✅ {sym}", str(res.get("status"))))
                except Exception as exc:            # report, never abort the rest
                    failures += 1
                    rows.append(fmt.plain_row(f"⚠️ {sym}",
                                              f"{type(exc).__name__}: {exc}"))
            head = fmt.title(
                f"Closed {len(syms) - failures}/{len(syms)}",
                "✅" if not failures else "⚠️")
            return fmt.join(head, fmt.lines(*rows))
        res = await self.positions.full_close(target, reason="telegram")
        return fmt.join(fmt.title("Position closed", "✅"),
                        fmt.plain_row("Symbol", f"<b>{target}</b>"),
                        fmt.plain_row("Result", str(res.get("status"))))

    async def _open(self, args: list[str]) -> str:
        """Manual entry. SL is MANDATORY — the sizer refuses an entry without one."""
        usage = fmt.join(
            fmt.title("Open a position", "⚡"),
            fmt.lines(fmt.row("Usage", "/bitget_open <SYMBOL> <BUY|SELL> <sl> [tp] [usd]"),
                      fmt.row("Example", "/bitget_open XRPUSDT BUY 2.10")),
            fmt.hint("A stop-loss is mandatory — the bot refuses any entry "
                     "without one."))
        if len(args) < 3:
            return usage
        if self.orch is None:
            return fmt.join(fmt.title("Manual open unavailable", "⚠️"),
                            fmt.hint("The orchestrator is not wired into the router."))

        sym = args[0].upper()
        if not sym.endswith("USDT"):
            sym += "USDT"
        side_txt = args[1].upper()
        if side_txt not in ("BUY", "SELL", "LONG", "SHORT"):
            return fmt.join(fmt.title("Invalid side", "⚠️"),
                            fmt.hint("Side must be BUY or SELL."))
        side = Side.BUY if side_txt in ("BUY", "LONG") else Side.SELL

        try:
            sl = float(args[2])
            tp = float(args[3]) if len(args) > 3 and args[3] != "-" else None
            notional = float(args[4]) if len(args) > 4 else 0.0
        except ValueError:
            return fmt.join(fmt.title("Invalid number", "⚠️"),
                            fmt.hint("Stop, target and size must be numbers."))
        if sl <= 0:
            return fmt.join(fmt.title("Invalid stop", "⚠️"),
                            fmt.hint("The stop-loss must be greater than zero."))

        inst = Instrument.from_token(sym.replace("USDT", ""))
        if self._adapter is None:
            return fmt.join(fmt.title("No exchange adapter", "⚠️"))
        try:
            mark = await self._adapter.get_mark_price(inst)
        except Exception as exc:
            return fmt.join(fmt.title("Price unavailable", "⚠️"),
                            fmt.hint(f"{type(exc).__name__}: {exc}"))

        # Refuse a stop on the wrong side of the market — this would be an
        # instant-liquidating entry, not a trade.
        wrong_side = ((side is Side.BUY and sl >= mark)
                      or (side is Side.SELL and sl <= mark))
        if wrong_side:
            where = "at or above" if side is Side.BUY else "at or below"
            return fmt.join(
                fmt.title("Entry refused", "🛑"),
                fmt.lines(fmt.plain_row("Reason",
                                        f"stop is {where} the current price"),
                          fmt.row("Stop", fmt.price(sl)),
                          fmt.row("Mark", fmt.price(mark))),
                fmt.hint("This would liquidate immediately."))

        if notional <= 0:
            params = getattr(self.orch, "params", {}) or {}
            notional = float(params.get("risk_per_trade_usd") or 5.0)

        sig = Signal(instrument=inst, side=side, entry=mark, sl=sl, tp=tp,
                     notional_usd=notional, leverage=1,
                     source="telegram-manual", raw={"manual": True})
        try:
            res = await self.orch.orders.open_from_signal(sig, mark_price=mark)
        except Exception as exc:
            return fmt.join(fmt.title("Order failed", "⚠️"),
                            fmt.plain_row("Symbol", f"<b>{sym}</b>"),
                            fmt.hint(f"{type(exc).__name__}: {exc}"))

        status = str(res.get("status") or "")
        filled = status.upper() in ("FILLED", "OK", "PLACED", "OPEN")
        head = fmt.title("Position opened" if filled else f"Order {status.lower()}",
                         "✅" if filled else "⚠️")
        body = [fmt.plain_row("Symbol",
                              f"<b>{sym}</b>  {fmt.side_glyph(side.value)} "
                              f"{side.value}"),
                fmt.row("Entry", fmt.price(mark)),
                fmt.row("Stop", fmt.price(sl))]
        if tp:
            body.append(fmt.row("Target", fmt.price(tp)))
        body.append(fmt.row("Size", f"${notional:.2f}"))
        if res.get("size") is not None:
            body.append(fmt.row("Contracts", fmt.qty(res["size"])))
        if res.get("avg_price") is not None:
            body.append(fmt.row("Avg fill", fmt.price(res["avg_price"])))
        blocks = [head, fmt.lines(*body)]
        if res.get("reason"):
            blocks.append(fmt.hint(str(res["reason"])))
        if filled and hasattr(self.orch, "_after_open"):
            await self.orch._after_open(sig, res)
        return fmt.join(*blocks)

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
        """Per-position cards plus the summed unrealized PnL."""
        cards, total = [], 0.0
        for p in self.positions.positions.values():
            sym = p.instrument.symbol
            # Trust the venue's own unrealizedPL when present (never invent one);
            # otherwise derive it from the live mark.
            upnl = p.unrealized_pnl
            mark = await self._mark(sym)
            if not upnl and mark and p.entry_price:
                sign = 1 if p.side is Side.BUY else -1
                upnl = (mark - p.entry_price) * p.size * sign
            total += upnl or 0.0

            # Percentage return on the position's own notional, derived from the
            # SAME figure as the PnL so the sign can never contradict it.
            move = None
            if upnl is not None and p.entry_price and p.size:
                notional = abs(float(p.entry_price) * float(p.size))
                if notional > 0:
                    move = upnl / notional * 100

            head = (f"{fmt.signed_glyph(upnl)} <b>{sym}</b>  "
                    f"{fmt.side_glyph(p.side.value)} {p.side.value}")
            pnl_txt = fmt.money(upnl, 4, suffix=" USDT")
            if move is not None:
                pnl_txt += f"  ({fmt.money(move, 2)}%)"
            cards.append(fmt.lines(
                head,
                fmt.plain_row("PnL", f"<b>{pnl_txt}</b>"),
                fmt.row("Size", fmt.qty(p.size)),
                fmt.row("Entry", fmt.price(p.entry_price)),
                fmt.row("Mark", fmt.price(mark)),
                fmt.row("Stop", fmt.price(p.sl_price)),
                fmt.row("Target", fmt.price(p.tp_price)),
                fmt.row("Liq.", fmt.price(p.liq_price)),
            ))
        return cards, total

    async def _read_only(self, cmd: str, args: list[str] | None = None) -> str:
        args = args or []
        # REAL-TIME FIX: the operator complained the bot reported stale PnL/size.
        # Every read-only command now reconciles the in-memory cache against the
        # EXCHANGE first, so /status /positions /pnl reflect the live venue
        # position (size, mark, PnL) instead of a cache that only updates on a WS
        # push. Best-effort: if the exchange call fails we still answer with the
        # last good cache rather than throwing.
        if self.positions is not None and getattr(self.positions, "adapter", None) is not None:
            with contextlib.suppress(Exception):
                await self.positions.sync()

        if cmd == "/price":
            if not args:
                return fmt.join(fmt.title("Price", "💹"),
                                fmt.row("Usage", "/bitget_price <SYMBOL>"))
            symbol = args[0].upper()
            inst = Instrument.from_token(symbol[:-4] if symbol.endswith("USDT") else symbol)
            if self._adapter is None:
                return fmt.join(fmt.title("Price unavailable", "⚠️"),
                                fmt.hint("No exchange adapter is wired."))
            try:
                mark = await self._adapter.get_mark_price(inst)
            except Exception as exc:
                return fmt.join(fmt.title("Price unavailable", "⚠️"),
                                fmt.row("Symbol", inst.symbol),
                                fmt.hint(f"{type(exc).__name__}: {exc}"))
            return fmt.join(fmt.title("Live price", "💹"), fmt.rule(),
                            fmt.lines(fmt.row("Symbol", inst.symbol),
                                      fmt.row("Mark", fmt.price(mark))))

        if cmd == "/limit_orders":
            if self._adapter is None:
                return fmt.join(fmt.title("Orders unavailable", "⚠️"),
                                fmt.hint("No exchange adapter is wired."))
            try:
                raw_orders = await self._adapter.get_open_orders()
            except Exception as exc:
                return fmt.join(fmt.title("Orders unavailable", "⚠️"),
                                fmt.hint(f"{type(exc).__name__}: {exc}"))
            orders = [o for o in raw_orders
                      if str(o.get("orderType", o.get("ordType", ""))).lower() == "limit"
                      and not getattr(self._adapter, "is_protective", lambda _: False)(o)]
            if not orders:
                return fmt.join(fmt.title("No pending limit orders", "📭"),
                                fmt.hint("The exchange has no working non-protective LIMIT orders."))
            cards = []
            for o in orders:
                cards.append(fmt.lines(
                    fmt.row("Symbol", o.get("symbol", "?")),
                    fmt.row("Side", o.get("side", "?")),
                    fmt.row("Price", fmt.price(o.get("price", o.get("px")))),
                    fmt.row("Size", fmt.qty(o.get("size", o.get("sz", o.get("quantity", 0))))),
                    fmt.row("Order ID", o.get("orderId", o.get("clientOid", "?")))))
            return fmt.join(fmt.title(f"Pending limit orders · {len(cards)}", "📋"),
                            *cards)

        if cmd == "/status":
            st = self.gate.status()
            live = st["mode"] == "LIVE"
            head = fmt.title("Status", "🟢" if live else "🧪")
            mode_row = fmt.plain_row("Mode", f"<b>{st['mode']}</b>")
            n_open = len(self.positions.positions)
            risk_enabled = bool(self.orch and getattr(self.orch, "high_risk_enabled", lambda: False)())
            body = [mode_row, fmt.plain_row(
                "Risk profile", f"<b>{'HIGH' if risk_enabled else 'NORMAL'}</b>"),
                    fmt.plain_row("Open positions", f"<b>{n_open}</b>")]
            if self.orch is not None:
                body.append(fmt.plain_row(
                    "Today", f"<b>{fmt.money(self.orch.daily_realized, 4)} USDT</b>"))
            if self.ledger is not None:
                nets = self.ledger.net_pnls()
                body.append(fmt.plain_row(
                    "Lifetime", f"<b>{fmt.money(sum(nets), 4)} USDT</b>"
                                f"  <i>{len(nets)} trades</i>"))
                body.append(fmt.plain_row(
                    "Fees", fmt.money(self.ledger.total_fees(), 4, signed=False)
                            + " USDT"))
            blocks = [head, fmt.lines(*body)]
            if st["unmet"]:
                blocks.append(fmt.lines(
                    "🔒 <b>Go-live checklist</b>",
                    *[f"{fmt.BULLET} {u}" for u in st["unmet"]]))
            return fmt.join(*blocks)

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
            degraded = bot_poller.tg_cmd_degraded()
            healthy = not any([deadman is True, breaker is True, degraded])

            head = fmt.title("Healthy" if healthy else "Degraded",
                             "✅" if healthy else "⚠️")
            rails = fmt.lines(
                fmt.plain_row("Mode", f"<b>{st['mode']}</b>"),
                fmt.plain_row("Trading", "halted" if deadman is True else "armed"),
                fmt.plain_row("Rate limit",
                              "tripped" if breaker is True
                              else ("unknown" if breaker is None else "normal")),
                fmt.plain_row("Commands", "degraded" if degraded else "responsive"),
                fmt.plain_row("Open positions", str(len(self.positions.positions))),
            )
            blocks = [head, rails]
            notes = []
            if bot_poller.tg_cmd_consecutive_errors:
                notes.append(f"{bot_poller.tg_cmd_consecutive_errors} consecutive "
                             "command-poll failures")
            if deadman is True:
                notes.append("Dead-man switch has halted new entries.")
            if breaker is True:
                notes.append("Rate-limit breaker is open; the bot is parked.")
            if notes:
                blocks.append(fmt.lines(*[f"{fmt.BULLET} {n}" for n in notes]))
            return fmt.join(*blocks)

        if cmd == "/balance":
            if self._adapter is None:
                return fmt.join(fmt.title("Balance unavailable", "⚠️"),
                                fmt.hint("No exchange adapter is wired."))
            try:
                avail = await self._adapter.get_balance_usdt()
            except Exception as exc:
                return fmt.join(
                    fmt.title("Balance unavailable", "⚠️"),
                    fmt.plain_row("Reason", f"<code>{type(exc).__name__}</code>"),
                    fmt.hint(str(exc)))
            coin = getattr(self._adapter, "margin_coin", "USDT")
            ptype = getattr(self._adapter, "product_type", "?")
            demo = str(ptype).startswith("S")
            _, upnl = await self._upnl_rows()

            body = [fmt.plain_row("Available", f"<b>{fmt.money(avail, 4, signed=False)}"
                                               f" {coin}</b>")]
            if self.positions.positions:
                body.append(fmt.plain_row("Unrealized",
                                          f"{fmt.money(upnl, 4)} {coin}"))
                body.append(fmt.plain_row(
                    "Equity", f"<b>{fmt.money(avail + upnl, 4, signed=False)}"
                              f" {coin}</b>"))
            blocks = [fmt.title("Wallet", "💰"), fmt.lines(*body),
                      fmt.hint(f"{ptype}" + (" · demo funds" if demo
                                             else " · real funds"))]
            return fmt.join(*blocks)

        if cmd == "/positions":
            if not self.positions.positions:
                return fmt.join(fmt.title("No open positions", "📭"),
                                fmt.hint("The bot is flat and waiting for a signal."))
            cards, total = await self._upnl_rows()
            head = fmt.title(f"Positions · {len(cards)}", "📈")
            footer = fmt.plain_row("Total PnL",
                                   f"<b>{fmt.money(total, 4)} USDT</b>")
            return fmt.join(head, *cards, fmt.lines(fmt.rule(), footer))

        if cmd == "/pnl":
            _cards, upnl = await self._upnl_rows()
            n_open = len(self.positions.positions)

            open_block = [f"{fmt.signed_glyph(upnl if n_open else None)} "
                          f"<b>Open</b>"]
            if n_open:
                open_block.append(fmt.plain_row(
                    "Unrealized", f"<b>{fmt.money(upnl, 4)} USDT</b>"
                                  f"  <i>{n_open} position"
                                  f"{'s' if n_open != 1 else ''}</i>"))
            else:
                open_block.append(fmt.plain_row("Unrealized", "flat"))

            today_block = None
            if self.orch is not None:
                daily = self.orch.daily_realized
                rows = [f"{fmt.signed_glyph(daily)} <b>Today</b>",
                        fmt.plain_row("Realized",
                                      f"<b>{fmt.money(daily, 4)} USDT</b>")]
                params = getattr(self.orch, "params", {}) or {}
                cap = params.get("max_daily_loss_pct")
                if cap is not None:
                    rows.append(fmt.plain_row("Loss cap", fmt.pct(cap)))
                breached = getattr(self.orch, "daily_loss_breached", None)
                if callable(breached):
                    try:
                        res = await breached() if asyncio.iscoroutinefunction(breached) else breached()
                        if res:
                            rows.append("🛑 <b>Daily loss cap reached</b> "
                                        "— new entries halted")
                    except Exception:
                        pass
                today_block = fmt.lines(*rows)

            life_block = None
            if self.ledger is not None:
                nets = self.ledger.net_pnls()
                net = sum(nets)
                wins = [n for n in nets if n > 0]
                rows = [f"{fmt.signed_glyph(net if nets else None)} <b>Lifetime</b>",
                        fmt.plain_row("Net", f"<b>{fmt.money(net, 4)} USDT</b>")]
                if nets:
                    rows.append(fmt.plain_row("Trades", str(len(nets))))
                    rows.append(fmt.plain_row(
                        "Win rate", fmt.pct(len(wins) / len(nets) * 100)))
                rows.append(fmt.plain_row(
                    "Fees", fmt.money(self.ledger.total_fees(), 4, signed=False)
                            + " USDT"))
                life_block = fmt.lines(*rows)

            return fmt.join(fmt.title("Profit & Loss", "📊"),
                            fmt.lines(*open_block), today_block, life_block)

        if cmd == "/activity":
            if self.ledger is None:
                return fmt.join(fmt.title("No ledger", "⚠️"))
            try:
                limit = max(1, min(20, int(args[0]))) if args else 5
            except ValueError:
                limit = 5
            try:
                trades = self.ledger.closed_trades(limit=200)
            except Exception as exc:
                return fmt.join(fmt.title("Activity unavailable", "⚠️"),
                                fmt.hint(f"{type(exc).__name__}: {exc}"))
            if not trades:
                return fmt.join(fmt.title("No closed trades yet", "📭"),
                                fmt.hint("Completed trades will be listed here."))
            recent = trades[-limit:][::-1]
            cards = []
            for t in recent:
                sym = t.get("symbol") or t.get("instrument") or "?"
                net = t.get("net_usd")
                fee = t.get("fees_usd") if t.get("fees_usd") is not None \
                    else t.get("fee_usd")
                stamp = fmt.when(t.get("closed_at") or t.get("ts") or "")
                rows = [f"{fmt.signed_glyph(net)} <b>{sym}</b>"
                        + (f"  <i>{stamp}</i>" if stamp else "")]
                if net is not None:
                    rows.append(fmt.plain_row("Net",
                                              f"<b>{fmt.money(net, 4)} USDT</b>"))
                if fee is not None:
                    rows.append(fmt.plain_row(
                        "Fee", fmt.money(fee, 4, signed=False) + " USDT"))
                if t.get("reason"):
                    rows.append(fmt.plain_row("Closed by", str(t["reason"])))
                cards.append(fmt.lines(*rows))
            nets = [t.get("net_usd") for t in recent if t.get("net_usd") is not None]
            footer = fmt.plain_row("Subtotal", f"<b>{fmt.money(sum(nets), 4)}"
                                               f" USDT</b>") if nets else None
            return fmt.join(fmt.title(f"Recent activity · {len(recent)}", "🧾"),
                            *cards,
                            fmt.lines(fmt.rule(), footer) if footer else None)

        if cmd == "/pairs":
            weak = self.shadow.weak_pairs()
            if not weak:
                return fmt.join(fmt.title("No flagged pairs", "✅"),
                                fmt.hint("Every tracked pair is performing "
                                         "within tolerance."))
            return fmt.join(
                fmt.title(f"Flagged pairs · {len(weak)}", "🚩"),
                fmt.lines(*[f"{fmt.BULLET} <code>{w}</code>" for w in weak]),
                fmt.hint("These pairs underperform in the shadow ledger."))
        return ""
