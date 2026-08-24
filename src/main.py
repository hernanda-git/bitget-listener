"""bitget-listener entrypoint.

    python -m src.main                # paper run (default, demo product type)
    python -m src.main --probe        # run the non-destructive API probe
    python -m src.main --go-live      # mint a one-time go-live token (gated)
    python -m src.main --web          # dashboard only

Defaults are deliberately timid: paper=True, SUSDT-FUTURES (demo), whitelist-only.
The paper flag is loaded from the DB when one is configured, so a restart cannot
silently resurrect live trading.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys

from src.config.db import BotConfig, InMemoryConfigStore, PostgresConfigStore
from src.eval.shadow_ledger import LedgerEvalStore, ShadowLedger
from src.exchanges.bitget import BitgetAdapter
from src.notifier.golive import GateConditions, GoLiveGate
from src.notifier.telegram import TelegramNotifier
from src.services.audit_lock import AuditLock, AuditLockError
from src.services.deadman import DeadManSwitch
from src.services.orchestrator import Orchestrator
from src.services.risk import RiskLimits
from src.sources.parser import SymbolMap
from src.sources.telegram import FakeSignalSource, TelethonSource
from src.store.ledger import TradeLedger
from src.store.pg_ledger import PostgresLedger
from src.streaming.bitget_ws import BitgetWS

log = logging.getLogger("bitget-listener")


def load_env(path: str = ".env") -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(path)
    except Exception:
        pass


def build_config() -> tuple[BotConfig, object]:
    if os.getenv("PGDATABASE") or os.getenv("PGHOST"):
        store = PostgresConfigStore(os.getenv("BOT_NAME", "bitget-listener"))
        return store.load(), store
    store = InMemoryConfigStore()
    cfg = store.load()
    cfg.paper = os.getenv("PAPER", "1") not in ("0", "false", "False")
    cfg.params["product_type"] = os.getenv("BITGET_PRODUCT_TYPE", "SUSDT-FUTURES")
    return cfg, store


async def build_runtime(cfg: BotConfig, store, use_fake_source: bool = False):
    product_type = cfg.params.get("product_type", "SUSDT-FUTURES")
    # One shared rate-limit gate for REST + WS so a 429 on either surface trips
    # the same ban-prevention breaker and the dashboard shows one source of truth.
    from src.exchanges.rate_limit import RateLimitGate
    rate_limit = RateLimitGate()
    adapter = BitgetAdapter(
        key=os.getenv("BITGET_API_KEY", ""),
        secret=os.getenv("BITGET_API_SECRET", ""),
        passphrase=os.getenv("BITGET_PASSPHRASE", ""),
        product_type=product_type,
        paper=cfg.paper,
        rate_limit=rate_limit,
    )
    # Ledger selection must track whether the shared Postgres is actually in use.
    # The live systemd unit sets PGDATABASE/PGHOST, so we use the shared
    # PostgresLedger there; otherwise (no-creds paper smoke, CI without the
    # shared DB) we fall back to a file-backed TradeLedger so the bot still runs
    # and the "originally-broke-the-build" smoke test stays green without a DB.
    _pg_configured = bool(os.getenv("PGDATABASE") or os.getenv("PGHOST"))
    if _pg_configured:
        ledger = PostgresLedger()
        # Self-heal the shared schema on every start (idempotent; cheap).
        from bot_common import ensure_schema
        with contextlib.suppress(Exception):
            ensure_schema()
    else:
        from src.store.ledger import TradeLedger
        ledger = TradeLedger(os.getenv("LEDGER_PATH", ":memory:"))
    shadow = ShadowLedger(LedgerEvalStore(ledger),
                          min_trades=int(os.getenv("SHADOW_MIN_TRADES", "30")),
                          auto_downsize=os.getenv("AUTO_DOWNSIZE", "0") == "1")
    gate = GoLiveGate(conditions=GateConditions(), config_store=store)
    # The authoritative LIVE flag is the persisted PAPER value (env PAPER=0 +
    # Postgres fatty_config.paper). This is INDEPENDENT of the gate's
    # conditions_met interlock (see golive.py): a direct PAPER=0 flip runs LIVE
    # even if conditions_met is False. conditions_met is an audit signal only.
    gate.paper = cfg.paper

    # The static SYMBOL_WHITELIST is no longer the gate: a hand-picked list is
    # exactly what silently dropped the PUMP signal (tradable, just not listed).
    # Allow-list is now OPT-IN — if SYMBOL_WHITELIST is empty, every symbol that
    # passes the live tradability check is eligible. An explicit list still works.
    raw_wl = os.getenv("SYMBOL_WHITELIST", "")
    whitelist = {s.strip().upper() for s in raw_wl.split(",") if s.strip()}
    symbol_map = SymbolMap(whitelist)

    # Private WS (fills + live positions/liq-price) is the contract-correct path
    # for those events — Bitget pushes them over the authenticated channel rather
    # than making us poll REST get_fills/get_positions on a loop. Built only when
    # real credentials exist; in paper/demo there is no private stream.
    _bk, _bs, _bp = (os.getenv("BITGET_API_KEY", ""), os.getenv("BITGET_API_SECRET", ""),
                     os.getenv("BITGET_PASSPHRASE", ""))
    private_stream = None
    if _bk and _bs and _bp:
        from src.exchanges.bitget_auth import BitgetAuth
        private_stream = BitgetWS(
            symbols=sorted(whitelist), product_type=product_type,
            auth=BitgetAuth(_bk, _bs, _bp), private=True, rate_limit=rate_limit)

    if use_fake_source:
        source = FakeSignalSource([], symbol_map=symbol_map, trace_store=ledger)
    else:
        source = TelethonSource(symbol_map=symbol_map, trace_store=ledger)

    stream = BitgetWS(symbols=sorted(whitelist), product_type=product_type,
                     rate_limit=rate_limit)
    notifier = TelegramNotifier()
    limits = RiskLimits(
        risk_per_trade_usd=float(cfg.params.get("risk_per_trade_usd", 5.0)),
        max_position_usd=float(cfg.params.get("max_position_usd", 100.0)),
        max_concurrent=int(cfg.params.get("max_concurrent", 2)),
        max_daily_loss_pct=float(cfg.params.get("max_daily_loss_pct", 30.0)),
        max_leverage=int(cfg.params.get("max_leverage", 20)),
        min_notional_usd=float(cfg.params.get("min_notional_usd", 5.0)),
        equity_usd=float(os.getenv("EQUITY_USD", "100")),
    )
    # Per-signal affordability: a symbol is eligible when its effective min
    # notional is actually affordable against live available margin — not because
    # it happens to be on a hand-picked list. ETH (0.01 * $3100 = $31) fails on a
    # small account; PUMP ($5 floor) passes. Measured, never assumed.
    from src.services.tradability import TradabilityChecker
    tradability = TradabilityChecker(
        adapter, margin_headroom=0.95, max_leverage=limits.max_leverage,
        blocklist=set())
    orch = Orchestrator(source, adapter, gate, ledger, notifier=notifier, stream=stream,
                        shadow=shadow, limits=limits, symbol_map=symbol_map,
                        whitelist=whitelist, rate_limit=rate_limit,
                        private_stream=private_stream, tradability=tradability)
    orch.deadman = DeadManSwitch(stream, adapter, orch.positions, notifier,
                                 ttl_s=float(os.getenv("DEADMAN_TTL_S", "60")))
    return orch


async def cmd_run(args) -> int:
    cfg, store = build_config()
    orch = await build_runtime(cfg, store, use_fake_source=args.fake_source)

    # Operator Telegram command poller (Bot API). Independent of the Telethon
    # @fattyfatclub listener and of the notifier sender. Commands are gated by
    # CommandRouter + GoLiveGate, so this can never flip to live on its own.
    from src.notifier.telegram import CommandRouter
    from src.notifier.bot_poller import poll_commands, register_bot_commands
    from src.services.preflight import preflight_venue

    # Venue-state preflight: a misconfigured account (hedge mode -> 40774; or
    # isolated margin -> liquidation inside the stop) silently breaks entries.
    # Repair what we can before the first signal, and refuse loudly if we can't.
    probe = (sorted(orch.whitelist) or ["PUMPUSDT"])[0]
    preflight = await preflight_venue(orch.adapter, probe, repair=True,
                                      want_margin_mode="crossed")
    if not preflight.ok:
        log.error("VENUE PREFLIGHT FAILED: %s", preflight.summary)
        if orch.notifier is not None:
            with contextlib.suppress(Exception):
                await orch.notifier.send(
                    f"⚠️ Venue preflight failed — trades may be rejected:\n"
                    f"{preflight.summary}")

    router = CommandRouter(gate=orch.gate, position_manager=orch.positions,
                          shadow_ledger=orch.shadow, ledger=orch.ledger,
                          orchestrator=orch)
    # Publish the command menu so Telegram's "/" popup lists them natively.
    await register_bot_commands(router)
    poller_task = asyncio.create_task(poll_commands(router))

    log.warning("starting in %s mode (product_type=%s)",
                "PAPER" if orch.gate.paper else "LIVE",
                cfg.params.get("product_type"))
    # Self-healing lock-file discipline (MASTER-PROMPT): only one live run/--web
    # tick may hold the process. A stale (>=30m) or dead-owned lock self-heals.
    lock = AuditLock(phase="run" + ("/web" if args.web else ""))
    try:
        lock.acquire()
    except AuditLockError as exc:
        log.error("refusing to start: another live tick holds .audit.lock (%s)", exc)
        return 1
    try:
        if args.web:
            import uvicorn

            from src.web.dashboard import create_app
            app = create_app(orch)
            await orch.start()
            server = uvicorn.Server(uvicorn.Config(
                app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")), log_level="info"))
            try:
                await server.serve()
            finally:
                await orch.stop()
            return 0
        try:
            await orch.start()
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await orch.stop()
    finally:
        if 'poller_task' in locals() and poller_task is not None:
            poller_task.cancel()
        # Release the shared Telegram httpx client so the process exits clean.
        from src.notifier.telegram import close_client
        await close_client()
        lock.release()
    return 0


async def cmd_go_live(args) -> int:
    """Mint a token only if every condition holds. Never flips the flag itself."""
    cfg, store = build_config()
    orch = await build_runtime(cfg, store, use_fake_source=True)
    gate = orch.gate
    gate.conditions.tests_green = os.getenv("TESTS_GREEN") == "1"
    gate.conditions.probe_green = os.getenv("PROBE_GREEN") == "1"
    gate.observe_expectancy(orch.shadow, min_samples=10)
    for fill in await orch.adapter.get_fills():
        gate.observe_fill(fill)
    for pos in await orch.adapter.get_positions():
        gate.observe_position(pos)
    ok, msg = gate.mint_token()
    print(msg if not ok else f"go-live token (valid 5 min, single use): {msg}\n"
                             f"redeem with:  /live {msg}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    load_env()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser(prog="bitget-listener")
    p.add_argument("--probe", action="store_true", help="run the non-destructive API probe")
    p.add_argument("--go-live", action="store_true", help="mint a one-time go-live token")
    p.add_argument("--web", action="store_true", help="serve the dashboard alongside the bot")
    p.add_argument("--fake-source", action="store_true",
                   help="use the fake signal source (no Telethon session needed)")
    args = p.parse_args(argv)

    if args.probe:
        from scripts.bitget_api_probe import main as probe_main
        return probe_main()
    if args.go_live:
        return asyncio.run(cmd_go_live(args))
    return asyncio.run(cmd_run(args))


if __name__ == "__main__":
    sys.exit(main())
