"""Orchestrator — wires sources, streams, services and the gate into one runtime.

Flow for every inbound message:

    RawSignal -> (management? -> PositionManager fast path, no LLM)
              -> whitelist + deadman + concurrency + daily-loss guards
              -> FixedRiskSizer (fee-aware) x ShadowLedger multiplier
              -> OrderService (paper gate -> validate -> repair -> place)
              -> TradeLedger + notifier

The paper flag is read from the GATE on every single order, never cached, so a
``/paper`` mid-session takes effect immediately.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from src.domain.model import Instrument, Position, RawSignal, Side, Signal, TradeDecision
from src.eval.shadow_ledger import ShadowLedger
from src.exchanges.rate_limit import RateLimitGate
from src.notifier.telegram import TelegramNotifier
from src.services.order import OrderService
from src.services.position import PositionManager
from src.services.risk import FixedRiskSizer, RiskLimits

log = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, source, adapter, gate, ledger, notifier=None, stream=None,
                 deadman=None, shadow: ShadowLedger | None = None,
                 limits: RiskLimits | None = None, symbol_map=None,
                 whitelist: set[str] | None = None,
                 rate_limit: RateLimitGate | None = None,
                 private_stream=None,
                 tradability=None) -> None:
        self.source = source
        self.adapter = adapter
        self.gate = gate
        self.ledger = ledger
        self.notifier = notifier
        self.stream = stream
        self.deadman = deadman
        self.shadow = shadow or ShadowLedger()
        self.limits = limits or RiskLimits()
        self.symbol_map = symbol_map
        self.whitelist = whitelist or set()
        self.sizer = FixedRiskSizer(self.limits)
        self.orders = OrderService(adapter, paper=gate.paper, sizer=self.sizer)
        self.positions = PositionManager(adapter, self.orders, ledger, stream)
        self.rate_limit = rate_limit or RateLimitGate()
        self.private_stream = private_stream  # BitgetWS(private=True) when creds exist
        # Per-signal affordability (replaces the brittle static whitelist).
        self.tradability = tradability
        self.open_trade_ids: dict[str, int] = {}
        self.daily_realized = 0.0
        self.skipped: list[dict] = []
        self._task: asyncio.Task | None = None
        self._running = False

    # ------------------------------------------------------------------- guards
    def _sync_paper_flag(self) -> None:
        """The gate is the single source of truth for paper/live, read per order."""
        self.orders.paper = self.gate.paper

    async def guard(self, rs: RawSignal) -> str | None:
        """Returns a rejection reason, or None when the signal may proceed."""
        if self.deadman is not None and not self.deadman.trading_allowed:
            return "dead-man's switch active (feed stale)"
        if not rs.symbol_usdt:
            return "no symbol parsed"
        # Static denylist still short-circuits instantly, but we no longer DROP
        # a tradable symbol just because it wasn't on a hand-picked list.
        if self.whitelist and rs.symbol_usdt not in self.whitelist:
            # Whitelist is now opt-IN only: if configured, it still filters.
            return f"{rs.symbol_usdt} not on allow-list"
        if len(self.positions.positions) >= self.limits.max_concurrent:
            return f"max concurrent positions ({self.limits.max_concurrent}) reached"
        if rs.symbol_usdt in self.positions.positions:
            return f"already in a {rs.symbol_usdt} position"
        if self.daily_loss_breached():
            return "daily loss limit breached"
        if self.tradability is not None:
            t = await self.tradability.check(rs.symbol_usdt)
            if not t.ok:
                return f"{rs.symbol_usdt} not tradable: {t.detail}"
        return None

    def daily_loss_breached(self) -> bool:
        cap = self.limits.max_daily_loss_pct / 100.0 * self.limits.equity_usd
        return self.daily_realized <= -abs(cap)

    # ------------------------------------------------------------------ pipeline
    def to_signal(self, rs: RawSignal) -> Signal | None:
        if not rs.symbol_usdt or rs.side not in ("BUY", "SELL"):
            return None
        inst = (self.symbol_map.resolve(rs.base or rs.symbol_usdt)
                if self.symbol_map is not None else Instrument.from_token(rs.symbol_usdt))
        return Signal(instrument=inst, side=Side(rs.side), entry=rs.entry, sl=rs.sl,
                      tp=rs.tp, notional_usd=rs.notional_usd, leverage=rs.leverage,
                      confidence=rs.confidence, message_id=rs.message_id,
                      ts_ms=rs.ts_ms, raw={"raw_text": rs.raw_text, **(rs.raw or {})})

    async def handle(self, rs: RawSignal) -> dict:
        """Single entry point for one inbound message."""
        if rs.is_management:
            return await self.handle_management(rs)

        # Ban-prevention: if the rate-limit breaker is open (repeated 429s),
        # refuse to place NEW orders and park silently rather than risk an IP ban.
        if self.rate_limit.is_open():
            reason = (f"rate-limit breaker open "
                      f"({self.rate_limit.cooldown_remaining_s:.0f}s remaining)")
            self.skipped.append({"symbol": rs.symbol_usdt, "reason": reason})
            log.warning("skipping signal: %s", reason)
            return {"status": "SKIPPED", "reason": reason}

        reason = await self.guard(rs)
        if reason is not None:
            self.skipped.append({"symbol": rs.symbol_usdt, "reason": reason})
            log.info("skipping signal: %s", reason)
            return {"status": "SKIPPED", "reason": reason}

        signal = self.to_signal(rs)
        if signal is None:
            return {"status": "SKIPPED", "reason": "unparseable signal"}

        self._sync_paper_flag()
        multiplier = self.shadow.size_multiplier(signal.instrument.symbol)
        result = await self.orders.open_from_signal(signal, size_multiplier=multiplier)
        await self._after_open(signal, result)
        return result

    async def _after_open(self, signal: Signal, result: dict) -> None:
        status = result.get("status")
        if status not in ("PLACED", "PAPER"):
            if self.notifier is not None:
                with contextlib.suppress(Exception):
                    await self.notifier.send(
                        f"REJECTED {signal.instrument.symbol}: {result.get('reason','')}")
            return
        order = result.get("order")
        if order is None:
            return
        fee = self.sizer.estimate_open_fee(order.qty, order.price or signal.entry,
                                          result.get("spec"))
        tid = self.ledger.record_open(
            symbol=order.instrument.symbol, side=order.side.value, qty=order.qty,
            entry_price=order.price or signal.entry, open_fee=fee, leverage=order.leverage,
            sl_price=order.sl, tp_price=order.tp, paper=(status == "PAPER"),
            client_order_id=order.client_order_id, decision_id=str(signal.message_id or ""))
        self.open_trade_ids[order.instrument.symbol] = tid
        # Paper mode runs with NO credentials, so it must never call a signed
        # endpoint. Track the position locally instead of syncing from the venue.
        # On a sync failure we also keep the local view so the position is never lost.
        if status == "PAPER":
            self._local_position(order)
        else:
            try:
                await self.positions.sync()
            except Exception as exc:
                log.error("position sync failed after open (%s) — keeping local view", exc)
                self._local_position(order)
            # Place the trader's EXACT SL/TP as resting plan orders (BUG C:
            # reduce-only, never auto-cancelled). A fill without protection is a
            # naked position; the signal's stop/target is authoritative.
            if (order.sl is not None or order.tp is not None):
                try:
                    existing = self.positions.get(order.instrument.symbol)
                    pos = Position(
                        instrument=order.instrument, side=order.side, size=order.qty,
                        entry_price=order.price or 0.0, leverage=order.leverage,
                        margin_mode=order.margin_mode,
                        sl_price=existing.sl_price if existing else None,
                        tp_price=existing.tp_price if existing else None,
                    )
                    plan = await self.orders.attach_protective(pos, order.sl, order.tp)
                    if self.notifier is not None:
                        with contextlib.suppress(Exception):
                            await self.notifier.send(
                                f"PROTECTION {order.instrument.symbol}: "
                                f"SL={order.sl} TP={order.tp} ({plan.get('status')})")
                except Exception as exc:
                    log.error("attach_protective failed after open (%s) — position is "
                              "NAKED, operator must set stops manually", exc)
                    if self.notifier is not None:
                        with contextlib.suppress(Exception):
                            await self.notifier.send(
                                f"WARNING {order.instrument.symbol} opened WITHOUT stops "
                                f"(attach failed: {exc}). Set SL/TP manually.")
        if self.notifier is not None:
            with contextlib.suppress(Exception):
                await self.notifier.send(TelegramNotifier.format_entry(result))

    def _local_position(self, order) -> None:
        self.positions.upsert(Position(
            instrument=order.instrument, side=order.side, size=order.qty,
            entry_price=order.price or 0.0,
            leverage=order.leverage, sl_price=order.sl, tp_price=order.tp,
            margin_mode=order.margin_mode))

    async def handle_management(self, rs: RawSignal) -> dict:
        """Deterministic management fast path — no LLM, milliseconds."""
        action = rs.management_action
        symbol = rs.symbol_usdt or self._only_open_symbol()
        if symbol is None:
            return {"status": "NO_POSITION", "action": action}
        self._sync_paper_flag()
        price = (rs.raw or {}).get("mgmt_price")
        if action == "sl_to_entry":
            res = await self.positions.sl_to_entry(symbol)
        elif action == "partial":
            res = await self.positions.book_partial(symbol, 0.5)
        elif action == "full_close":
            res = await self.positions.full_close(symbol)
        elif action == "cancel_pending":
            res = await self.positions.cancel_pending(symbol)
        elif action == "sl_modify":
            res = await self.positions.modify_stop(symbol, sl=price)
        elif action == "tp_modify":
            res = await self.positions.modify_stop(symbol, tp=price)
        else:
            res = {"status": "UNKNOWN_ACTION", "action": action}
        if action in ("full_close", "partial") and res.get("status") not in (
                "NO_POSITION", "REJECTED"):
            await self._book_close(symbol, res)
        return res

    def _only_open_symbol(self) -> str | None:
        """Bare management text applies only when exactly one position is open."""
        if len(self.positions.positions) == 1:
            return next(iter(self.positions.positions))
        return None

    async def _book_close(self, symbol: str, res: dict) -> None:
        tid = self.open_trade_ids.get(symbol)
        if tid is None:
            return
        mark = None
        if self.stream is not None:
            mark = self.stream.mark_price(symbol)
        if mark is None:
            with contextlib.suppress(Exception):
                mark = await self.adapter.get_mark_price(Instrument.from_token(symbol))
        if mark is None:
            log.error("cannot book close for %s without a price — leaving trade open", symbol)
            return
        row = self.ledger.record_close(
            tid, exit_price=mark, close_fee=self.sizer.estimate_close_fee(
                self.ledger.get(tid)["qty"], mark, None),
            exit_reason=res.get("action", "close"))
        self.daily_realized += (row.net_usd or 0.0)
        self.shadow.record(symbol, (row.net_usd or 0.0) > 0, row.net_usd or 0.0)
        self.open_trade_ids.pop(symbol, None)
        self.gate.observe_expectancy(self.shadow)
        if self.notifier is not None:
            from src.notifier.telegram import TelegramNotifier
            with contextlib.suppress(Exception):
                await self.notifier.send(TelegramNotifier.format_exit(row))

    # --------------------------------------------------------------- stream hooks
    async def on_fill(self, fill) -> None:
        self.gate.observe_fill(fill)
        self.positions.assert_fee_recorded(fill)

    def on_position(self, position) -> None:
        self.gate.observe_position(position)
        self.positions.upsert(position)

    async def on_tick(self, symbol: str, mark: float) -> None:
        await self.positions.on_mark(symbol, mark)

    # ------------------------------------------------------------------ lifecycle
    async def run_once(self) -> int:
        """Drain the source queue once. Returns the number handled."""
        queue = self.source.subscribe()
        handled = 0
        while not queue.empty():
            rs = queue.get_nowait()
            with contextlib.suppress(Exception):
                await self.handle(rs)
            handled += 1
        return handled

    async def start(self) -> None:
        await self.source.start()
        if self.stream is not None:
            self.stream.on_fill = self.on_fill
            self.stream.on_position = self.on_position
            self.stream.on_tick = lambda s, p: asyncio.create_task(self.on_tick(s, p))
            await self.stream.start()
        # Private WS carries fills (go-live observe_fill + BUG A fee tripwire) and
        # live position/liq-price updates — the contract-correct, low-rate path
        # vs polling REST get_fills/get_positions on a loop.
        if self.private_stream is not None:
            self.private_stream.on_fill = self.on_fill
            self.private_stream.on_position = self.on_position
            await self.private_stream.start()
        if self.deadman is not None:
            await self.deadman.start()
        self._running = True
        self._task = asyncio.create_task(self._loop())
        self._equity_task = asyncio.create_task(self._equity_sampler())
        self._protection_task = asyncio.create_task(self._protection_sampler())

    async def _equity_sampler(self) -> None:
        """Periodically append a wallet-equity sample so the dashboard can chart
        historical balance even when no trades are happening."""
        while self._running:
            try:
                await asyncio.sleep(getattr(self, "equity_sample_sec", 60))
                self._record_equity_sample()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - sampler must never kill the bot
                log.exception("equity sampler error: %s", exc)

    async def _protection_sampler(self) -> None:
        """BUG #3: periodically refresh the REAL resting SL/TP for open positions
        so /api/state (and the dashboard) surface live plan-order protection even
        though the private WS positions channel does not carry it."""
        while self._running:
            try:
                await asyncio.sleep(getattr(self, "protection_sample_sec", 30))
                for sym, pos in list(self.positions.positions.items()):
                    sl, tp = await self.positions._protection(pos.instrument)
                    if sl is None and tp is None:
                        continue
                    self.positions.upsert(Position(**{**pos.__dict__,
                        "sl_price": sl if sl is not None else pos.sl_price,
                        "tp_price": tp if tp is not None else pos.tp_price}))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.debug("protection sampler error: %s", exc)

    def _record_equity_sample(self) -> None:
        try:
            nets = self.ledger.net_pnls()
            equity = sum(nets)
            self.ledger.record_equity(
                ts_ms=int(time.time() * 1000),
                equity_usd=equity,
                fees_usd=self.ledger.total_fees(),
                open_pos=len(self.positions.positions),
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("equity sample skipped: %s", exc)

    async def _loop(self) -> None:
        queue = self.source.subscribe()
        while self._running:
            try:
                rs = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            try:
                await self.handle(rs)
            except Exception as exc:
                log.exception("handler error: %s", exc)

    async def stop(self) -> None:
        self._running = False
        for tk in (self._task, getattr(self, "_equity_task", None),
                   getattr(self, "_protection_task", None)):
            if tk:
                tk.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await tk
        for comp in (self.deadman, self.stream, self.private_stream, self.source):
            if comp is not None:
                with contextlib.suppress(Exception):
                    await comp.stop()

    def snapshot(self) -> dict:
        """State for the web dashboard / /status."""
        from src.notifier import bot_poller
        nets = self.ledger.net_pnls()
        return {
            "mode": "PAPER" if self.gate.paper else "LIVE",
            "gate": self.gate.status(),
            "tg_cmd_conflict": bool(bot_poller.tg_cmd_conflict),
            "tg_cmd_degraded": bool(bot_poller.tg_cmd_degraded()),
            "tg_cmd_consecutive_errors": int(bot_poller.tg_cmd_consecutive_errors),
            "positions": [
                {"symbol": p.instrument.symbol, "side": p.side.value, "size": p.size,
                 "entry": p.entry_price, "sl": p.sl_price, "tp": p.tp_price,
                 "liq": p.liq_price, "upnl": p.unrealized_pnl}
                for p in self.positions.positions.values()],
            "closed_trades": len(nets),
            "net_usd": sum(nets),
            "fees_usd": self.ledger.total_fees(),
            "daily_realized": self.daily_realized,
            "deadman_halted": bool(self.deadman.halted) if self.deadman else False,
            "flagged_pairs": self.shadow.weak_pairs(),
            "rate_limit_breaker_open": self.rate_limit.is_open(),
            "rate_limit_cooldown_s": round(self.rate_limit.cooldown_remaining_s, 1),
            "rate_limit_alerts": self.rate_limit.alerts[-5:],
            "skipped": self.skipped[-20:],
            "equity_history": self.ledger.equity_history(500),
        }
