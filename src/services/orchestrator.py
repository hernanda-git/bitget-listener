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
import os
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
        if await self.daily_loss_breached():
            return "daily loss limit breached"
        if self.tradability is not None:
            t = await self.tradability.check(rs.symbol_usdt)
            if not t.ok:
                return f"{rs.symbol_usdt} not tradable: {t.detail}"
        return None

    async def daily_loss_breached(self) -> bool:
        """Kill-switch: refuse new entries once realized loss exceeds the cap.

        The cap denominator is the LIVE wallet equity, not the static
        ``EQUITY_USD`` env var. Sizing never used EQUITY_USD, so the env was only
        feeding this denominator — and with the real wallet at ~0.076 USDT versus
        the configured 8.88, the cap was ~117x too loose (a $2.66 loss budget on
        a $0.076 account, effectively disabling the breaker).

        Safety: falls back to the configured equity_usd only when the live read
        fails, and floors the denominator at a small non-zero value so a zero/
        unreadable balance can NEVER make the cap vanish (which would let trading
        continue with no loss guard at all).
        """
        live_equity = await self._live_equity_usd()
        denom = live_equity if live_equity and live_equity > 0 else self.limits.equity_usd
        denom = max(denom, 1.0)  # never let the cap collapse to ~0
        cap = self.limits.max_daily_loss_pct / 100.0 * denom
        return self.daily_realized <= -abs(cap)

    async def _live_equity_usd(self) -> float:
        """Best-effort live wallet equity for the daily-loss denominator.

        In PAPER mode we must NEVER call a signed endpoint (the account endpoint
        is authenticated). Paper trading has no real equity to read, so we return
        0.0 and let ``daily_loss_breached`` fall back to the configured
        ``equity_usd`` — which is exactly the denominator paper mode wants.
        """
        if getattr(self, "gate", None) is not None and getattr(self.gate, "paper", False):
            return 0.0
        try:
            if self.adapter is not None:
                return float(await self.adapter.get_balance_usdt())
        except Exception as exc:  # noqa: BLE001 - never block the guard on a read failure
            log.warning("live equity read failed (%s) — using configured equity_usd", exc)
        return 0.0

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

        # Realtime push: a valid signal was accepted and is about to be traded.
        # (Rejections already push "REJECTED <SYM>: <reason>" in _after_open.)
        if self.notifier is not None:
            with contextlib.suppress(Exception):
                await self.notifier.send(
                    TelegramNotifier.format_signal(signal))

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
            # The WS monitor is the primary SL/TP enforcement (the exchange tpsl
            # endpoint is unreliable). Seed the position's bot-side SL/TP from the
            # signal NOW so the monitor is armed even before/without venue attach.
            pos = self.positions.get(order.instrument.symbol)
            if pos is not None and (order.sl is not None or order.tp is not None):
                self.positions.upsert(Position(**{**pos.__dict__,
                    "sl_price": order.sl if order.sl is not None else pos.sl_price,
                    "tp_price": order.tp if order.tp is not None else pos.tp_price}))
                pos = self.positions.get(order.instrument.symbol)
            # Place the trader's EXACT SL/TP as resting plan orders on the venue
            # (BUG C: reduce-only, never auto-cancelled) as a best-effort belt-
            # AND-suspenders; monitor-side closure is the real safety net.
            if pos is not None and (order.sl is not None or order.tp is not None):
                try:
                    plan = await self.orders.attach_protective(pos, order.sl, order.tp)
                    if self.notifier is not None:
                        with contextlib.suppress(Exception):
                            await self.notifier.send(
                                f"PROTECTION {order.instrument.symbol}: "
                                f"SL={order.sl} TP={order.tp} ({plan.get('status')})")
                except Exception as exc:
                    log.error("attach_protective failed after open (%s) — position is "
                              "NAKED on the venue but MONITOR-ARMED via bot-side SL/TP", exc)
                    if self.notifier is not None:
                        with contextlib.suppress(Exception):
                            await self.notifier.send(
                                f"WARNING {order.instrument.symbol} venue stops FAILED "
                                f"(monitor-side SL/TP armed: SL={order.sl} TP={order.tp}). "
                                f"Attach error: {exc}")
        # Ensure the WS price feed tracks this position's coin so the SL/TP
        # monitor receives ticks for it (not just the dead-man sentinel).
        self._track_position_symbols()
        if self.notifier is not None:
            with contextlib.suppress(Exception):
                await self.notifier.send(TelegramNotifier.format_open(result))

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
        log.info("management signal: %s action=%s", symbol or "(none)", action)
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
        # Re-sync the WS price feed: a close frees a symbol, a partial keeps it.
        self._track_position_symbols()
        return res

    def _only_open_symbol(self) -> str | None:
        """Bare management text applies only when exactly one position is open."""
        if len(self.positions.positions) == 1:
            return next(iter(self.positions.positions))
        return None

    async def _book_close(self, symbol: str, res: dict) -> None:
        tid = self.open_trade_ids.get(symbol)
        if tid is None:
            # DEFENSE-IN-DEPTH: the symbol may not be in the in-memory map (e.g. a
            # restart cleared it before the ledger rehydrate, or the open predates
            # the mapping). Fall back to the ledger's open trade for this symbol so
            # the close is NEVER silently dropped (that was a real defect: a
            # tp1_booked close executed on the exchange but wrote no ledger row).
            try:
                open_rows = [r for r in self.ledger.open_trades()
                             if r["symbol"] == symbol]
                if open_rows:
                    tid = open_rows[0]["id"]
                    self.open_trade_ids[symbol] = tid
            except Exception as exc:  # noqa: BLE001
                log.warning("open-trade lookup failed for %s: %s", symbol, exc)
        if tid is None:
            log.error("cannot book close for %s: no open trade in ledger — "
                      "leaving position as-is (exchange already closed it)", symbol)
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
            exit_reason=res.get("reason") or res.get("action", "close"))
        self.daily_realized += (row.net_usd or 0.0)
        self.shadow.record(symbol, (row.net_usd or 0.0) > 0, row.net_usd or 0.0)
        self.open_trade_ids.pop(symbol, None)
        self.gate.observe_expectancy(self.shadow)
        if self.notifier is not None:
            from src.notifier.telegram import TelegramNotifier
            with contextlib.suppress(Exception):
                reason = row.exit_reason or res.get("reason") or ""
                # Distinct realtime push per exit type so the operator sees
                # TP hit / SL hit / manual close / liquidation as separate events.
                if reason == "target_hit":
                    await self.notifier.send(TelegramNotifier.format_tp_hit(row))
                elif reason == "stop_breached":
                    await self.notifier.send(TelegramNotifier.format_sl_hit(row))
                elif reason == "liquidation":
                    await self.notifier.send(TelegramNotifier.format_liquidation(row))
                elif reason in ("telegram", "manual"):
                    await self.notifier.send(TelegramNotifier.format_manual_tp(row))
                else:
                    await self.notifier.send(TelegramNotifier.format_exit(row))

    # --------------------------------------------------------------- stream hooks
    async def on_fill(self, fill) -> None:
        self.gate.observe_fill(fill)
        self.positions.assert_fee_recorded(fill)

    def on_position(self, position) -> None:
        self.gate.observe_position(position)
        # Preserve bot-side SL/TP: the private WS positions channel does NOT carry
        # our monitor-side levels, so a raw upsert would wipe them on every push.
        # Only adopt the incoming level when it is actually present.
        existing = self.positions.get(position.instrument.symbol)
        if existing is not None:
            if position.sl_price is None and existing.sl_price is not None:
                position = Position(**{**position.__dict__, "sl_price": existing.sl_price})
            if position.tp_price is None and existing.tp_price is not None:
                position = Position(**{**position.__dict__, "tp_price": existing.tp_price})
        # After a restart the in-memory level is gone but the persisted file has it.
        # Re-arm the monitor so an open position is never naked (no venue stop exists).
        if position.sl_price is None and position.tp_price is None:
            armed = self.positions._armed.get(position.instrument.symbol.upper())
            if armed:
                if armed.get("sl") is not None:
                    position = Position(**{**position.__dict__, "sl_price": armed["sl"]})
                if armed.get("tp") is not None:
                    position = Position(**{**position.__dict__, "tp_price": armed["tp"]})
                log.info("re-armed bot-side SL/TP for %s from persisted state: SL=%s TP=%s",
                         position.instrument.symbol, position.sl_price, position.tp_price)
        self.positions.upsert(position)
        self._track_position_symbols()

    async def on_tick(self, symbol: str, mark: float) -> None:
        res = await self.positions.on_mark(symbol, mark)
        if res is not None and res.get("status") not in ("NO_POSITION", "REJECTED"):
            await self._book_close(symbol, res)

    def _track_position_symbols(self) -> None:
        """Keep the public WS subscribed to every open position's coin so the
        SL/TP monitor actually receives ticks for it (and the dead-man sentinel
        stays as a fallback when nothing is open)."""
        if self.stream is None or getattr(self.stream, "private", True):
            return
        syms = list(self.positions.positions.keys())
        if syms:
            self.stream.add_symbols(syms)
        else:
            # Nothing open: ensure at least the liveness sentinel is tracked.
            self.stream.add_symbols([getattr(self.stream, "LIVENESS_SENTINEL", "BTCUSDT")])

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

    async def _arm_open_protection(self) -> None:
        """Startup warmup: arm bot-side SL/TP for open positions that have none,
        using the trader's intended levels from the ARM_SL_TP env override.

        Format: comma-separated SYMBOL:SL:TP (e.g. "PUMPUSDT:0.002882:0.00337").
        This is the monitor's PRIMARY enforcement level and works even when the
        exchange's place-tpsl-order endpoint is unavailable. Open positions that
        already carry a bot-side level are left untouched.
        """
        raw = os.getenv("ARM_SL_TP", "").strip()
        if not raw:
            return
        overrides: dict[str, tuple[float, float]] = {}
        for chunk in raw.split(","):
            parts = chunk.strip().split(":")
            if len(parts) == 3:
                try:
                    overrides[parts[0].strip().upper()] = (float(parts[1]), float(parts[2]))
                except ValueError:
                    log.warning("ARM_SL_TP bad entry ignored: %s", chunk)
        if not overrides:
            return
        # Make sure we have the current open positions before arming.
        with contextlib.suppress(Exception):
            await self.positions.sync()
        self._track_position_symbols()
        for sym, (sl, tp) in overrides.items():
            pos = self.positions.get(sym)
            if pos is None:
                continue
            if pos.sl_price is not None or pos.tp_price is not None:
                continue
            log.info("arming bot-side SL/TP for %s from ARM_SL_TP: SL=%s TP=%s", sym, sl, tp)
            with contextlib.suppress(Exception):
                await self.positions.modify_stop(sym, sl=sl, tp=tp)

    async def start(self) -> None:
        # REHYDRATE open_trade_ids from the ledger so a management close/partial
        # that arrives after a restart (or when the open happened before the PG
        # refactor) still books correctly. Without this, _book_close early-returns
        # on a missing id and the ledger silently skips the close entirely.
        try:
            for row in self.ledger.open_trades():
                self.open_trade_ids[row["symbol"]] = row["id"]
            if self.open_trade_ids:
                log.info("rehydrated open_trade_ids from ledger: %s", self.open_trade_ids)
        except Exception as exc:  # noqa: BLE001 - never block startup on a read failure
            log.warning("open_trade_ids rehydrate failed (%s) — closes will re-book on open", exc)
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
        self._reconcile_task = asyncio.create_task(self._position_reconciler())
        # Arm bot-side SL/TP for any open position lacking it (trader's levels
        # from ARM_SL_TP). The WS monitor then closes on breach/target.
        await self._arm_open_protection()

    async def _position_reconciler(self) -> None:
        """Periodically reconcile the in-memory position cache against the
        EXCHANGE so /api/state, /bitget_status and /bitget_positions report the
        real size/PnL instead of a stale cache that only updates on a WS push or
        an open (BUG: a partial/full close that the WS misses leaves the cache
        wrong, so the operator sees old PnL). Cheap REST pull; never blocks
        trading. A failure keeps the last good view rather than nuking state.
        """
        while self._running:
            try:
                await asyncio.sleep(getattr(self, "reconcile_sec", 30))
                if self.adapter is None:
                    continue
                live = await self.adapter.get_positions()
                # Drop cached positions the exchange no longer has; refresh sizes.
                live_syms = {p.instrument.symbol for p in (live or []) if p.size > 0}
                for sym in list(self.positions.positions.keys()):
                    if sym not in live_syms:
                        self.positions.positions.pop(sym, None)
                for p in (live or []):
                    if p.size > 0:
                        # Preserve bot-side SL/TP: the exchange has no resting
                        # stop (this account's tpsl endpoint is unreliable), so
                        # the monitor-side levels MUST survive the reconcile —
                        # otherwise a raw upsert wipes SL/TP to None every cycle
                        # and the position goes naked. Adopt venue levels only
                        # when actually present, exactly like on_position().
                        existing = self.positions.get(p.instrument.symbol)
                        if existing is not None:
                            if p.sl_price is None and existing.sl_price is not None:
                                p = Position(**{**p.__dict__,
                                                "sl_price": existing.sl_price})
                            if p.tp_price is None and existing.tp_price is not None:
                                p = Position(**{**p.__dict__,
                                                "tp_price": existing.tp_price})
                        self.positions.upsert(p)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconciler must never kill the bot
                log.debug("position reconciler error: %s", exc)

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
        so /api/state surfaces live plan-order protection.

        Merge rule: exchange values win ONLY when present; a bot-side level that
        the monitor depends on (set from the signal or via /sl_modify) is NEVER
        clobbered to None just because the venue has no plan order.
        """
        while self._running:
            try:
                await asyncio.sleep(getattr(self, "protection_sample_sec", 30))
                for sym, pos in list(self.positions.positions.items()):
                    sl, tp = await self.positions._protection(pos.instrument)
                    new_sl = sl if sl is not None else pos.sl_price
                    new_tp = tp if tp is not None else pos.tp_price
                    if new_sl != pos.sl_price or new_tp != pos.tp_price:
                        self.positions.upsert(Position(**{**pos.__dict__,
                            "sl_price": new_sl, "tp_price": new_tp}))
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
                   getattr(self, "_protection_task", None),
                   getattr(self, "_reconcile_task", None)):
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
