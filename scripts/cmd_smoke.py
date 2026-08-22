"""Exercise every read-only command against the LIVE wired bot objects.

Read-only: builds a real BitgetAdapter from .env and calls the same
CommandRouter code path the Telegram poller uses. Places no orders.
"""
import asyncio
import os
import re
import subprocess
import sys

sys.path.insert(0, "/opt/bots/bitget-listener")

from dotenv import dotenv_values  # noqa: E402

os.environ.update({k: v for k, v in dotenv_values(
    "/opt/bots/bitget-listener/.env").items() if v})
unit = subprocess.run(["systemctl", "cat", "bitget-listener.service"],
                      capture_output=True, text=True).stdout
for m in re.finditer(r"^Environment=([A-Z_]+)=(.*)$", unit, re.M):
    os.environ.setdefault(m.group(1), m.group(2).strip())

from src.exchanges.bitget import BitgetAdapter        # noqa: E402
from src.notifier.telegram import CommandRouter       # noqa: E402
from src.store.ledger import TradeLedger             # noqa: E402
from types import SimpleNamespace                     # noqa: E402

OPERATOR = os.environ.get("TG_OPERATOR_ID", "")


async def main():
    adapter = BitgetAdapter(
        key=os.environ.get("BITGET_API_KEY", ""),
        secret=os.environ.get("BITGET_API_SECRET", ""),
        passphrase=os.environ.get("BITGET_PASSPHRASE", ""),
        product_type=os.environ.get("BITGET_PRODUCT_TYPE", "USDT-FUTURES"),
        paper=False,
    )
    ledger = TradeLedger(os.environ.get("LEDGER_PATH", ":memory:"))
    positions = SimpleNamespace(positions={}, adapter=adapter, stream=None)

    # Optional: seed a fake position so the populated layouts can be inspected
    # without waiting for a real fill. Never touches the exchange.
    if "--demo" in sys.argv:
        from src.domain.model import Instrument as I, Position as P, Side
        positions.positions = {
            "XRPUSDT": P(instrument=I.from_token("XRP"), side=Side.BUY,
                         size=17.0, entry_price=2.8412, sl_price=2.7100,
                         tp_price=3.0500, liq_price=2.4050,
                         unrealized_pnl=0.3417),
            "DOGEUSDT": P(instrument=I.from_token("DOGE"), side=Side.SELL,
                          size=240.0, entry_price=0.20841, sl_price=0.21900,
                          tp_price=0.19200, liq_price=None,
                          unrealized_pnl=-0.1128),
        }
    gate = SimpleNamespace(status=lambda: {
        "mode": "LIVE", "paper": False, "unmet": [], "conditions_met": False})
    orch = SimpleNamespace(daily_realized=0.0, deadman_halted=False,
                           params={"risk_per_trade_usd": 5.0,
                                   "max_daily_loss_pct": 30.0},
                           daily_loss_breached=lambda: False, orders=None)
    r = CommandRouter(gate=gate, position_manager=positions,
                      shadow_ledger=SimpleNamespace(weak_pairs=lambda: []),
                      ledger=ledger, operator_id=OPERATOR, orchestrator=orch)

    for cmd in ("/", "/health", "/balance", "/positions", "/pnl",
                "/activity", "/status", "/pairs"):
        out = await r.handle(cmd, OPERATOR)
        print(f"\n===== {cmd} =====")
        print(re.sub(r"</?(b|code|i)>", "", out))

    # authorization boundary, live
    print("\n===== /close all as a STRANGER =====")
    print(await r.handle("/close all", "111222333"))


asyncio.run(main())
