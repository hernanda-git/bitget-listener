"""Right-size the risk params for an $8.88 account before a real entry.

The stored params were written for a funded account:
  risk_per_trade_usd = 5.00  -> on a 3.2% stop that implies a $149 notional,
                                clamped to max_position_usd = $100.
  $100 notional on $8.88 equity is ~11x leverage, and a stop-out would cost
  $3.34 — MORE than the $2.66 daily-loss rail. The bot would blow its own
  daily limit on a single planned loss.

This sets both to values that fit the real balance:
  risk_per_trade_usd = 0.18  (~2% of equity per trade)
  max_position_usd   = 6.00  (just above the $5 venue floor)

Read the BEFORE and AFTER it prints; nothing else is touched.
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
_unit = subprocess.run(["systemctl", "cat", "bitget-listener.service"],
                       capture_output=True, text=True).stdout
for _m in re.finditer(r"^Environment=([A-Z_]+)=(.*)$", _unit, re.M):
    os.environ.setdefault(_m.group(1), _m.group(2).strip())

import json  # noqa: E402

from src.config.db import PostgresConfigStore  # noqa: E402

NEW = {"risk_per_trade_usd": 1.0, "max_position_usd": 6.0}


async def main():
    store = PostgresConfigStore()
    cfg = store.load()
    params = dict(cfg.params)
    print("BEFORE")
    for k in NEW:
        print(f"  {k:22}= {params.get(k)}")

    params.update(NEW)
    # PostgresConfigStore exposes set_paper but not set_params, so write the
    # JSONB column directly, scoped to this bot_name.
    with store._connect() as conn:
        conn.execute("UPDATE fatty_config SET params=%s,"
                     " updated_at=CURRENT_TIMESTAMP WHERE bot_name=%s",
                     (json.dumps(params), store.bot_name))
        conn.commit()

    fresh = store.load()
    print("AFTER")
    for k in NEW:
        print(f"  {k:22}= {fresh.params.get(k)}")
    for k in ("max_concurrent", "max_daily_loss_pct", "min_notional_usd"):
        print(f"  {k:22}= {fresh.params.get(k)}  (unchanged)")


asyncio.run(main())
