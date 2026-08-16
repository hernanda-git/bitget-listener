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
import logging
import os
import sys

from src.config.db import BotConfig, InMemoryConfigStore, PostgresConfigStore
from src.eval.shadow_ledger import LedgerEvalStore, ShadowLedger
from src.exchanges.bitget import BitgetAdapter
from src.notifier.golive import GateConditions, GoLiveGate
from src.notifier.telegram import TelegramNotifier
from src.services.deadman import DeadManSwitch
from src.services.orchestrator import Orchestrator
from src.services.risk import RiskLimits
from src.sources.parser import SymbolMap
from src.sources.telegram import FakeSignalSource, TelethonSource
from src.store.ledger import TradeLedger
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
    adapter = BitgetAdapter(
        key=os.getenv("BITGET_API_KEY", ""),
        secret=os.getenv("BITGET_API_SECRET", ""),
        passphrase=os.getenv("BITGET_PASSPHRASE", ""),
        product_type=product_type,
        paper=cfg.paper,
    )
    ledger = TradeLedger(os.getenv("LEDGER_PATH", "bitget_listener.db"))
    shadow = ShadowLedger(LedgerEvalStore(ledger),
                          min_trades=int(os.getenv("SHADOW_MIN_TRADES", "30")),
                          auto_downsize=os.getenv("AUTO_DOWNSIZE", "0") == "1")
    gate = GoLiveGate(conditions=GateConditions(), config_store=store)
    gate.paper = cfg.paper           # persisted flag wins over anything in code

    whitelist = {s.strip().upper() for s in os.getenv(
        "SYMBOL_WHITELIST", "BTCUSDT,ETHUSDT,SOLUSDT").split(",") if s.strip()}
    symbol_map = SymbolMap(whitelist)

    if use_fake_source:
        source = FakeSignalSource([], symbol_map=symbol_map)
    else:
        source = TelethonSource(symbol_map=symbol_map)

    stream = BitgetWS(symbols=sorted(whitelist), product_type=product_type)
    notifier = TelegramNotifier()
    limits = RiskLimits(
        risk_per_trade_usd=float(cfg.params.get("risk_per_trade_usd", 5.0)),
        max_position_usd=float(cfg.params.get("max_position_usd", 100.0)),
        max_concurrent=int(cfg.params.get("max_concurrent", 2)),
        max_daily_loss_pct=float(cfg.params.get("max_daily_loss_pct", 30.0)),
        max_leverage=int(cfg.params.get("max_leverage", 20)),
        equity_usd=float(os.getenv("EQUITY_USD", "100")),
    )
    orch = Orchestrator(source, adapter, gate, ledger, notifier=notifier, stream=stream,
                        shadow=shadow, limits=limits, symbol_map=symbol_map,
                        whitelist=whitelist)
    orch.deadman = DeadManSwitch(stream, adapter, orch.positions, notifier,
                                 ttl_s=float(os.getenv("DEADMAN_TTL_S", "60")))
    return orch


async def cmd_run(args) -> int:
    cfg, store = build_config()
    orch = await build_runtime(cfg, store, use_fake_source=args.fake_source)
    log.warning("starting in %s mode (product_type=%s)",
                "PAPER" if orch.gate.paper else "LIVE",
                cfg.params.get("product_type"))
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
