"""Operator action: tighten risk params for a very small real-money account.

Sets max_concurrent=1 and product_type=USDT-FUTURES in fatty_config.params.
Prints BEFORE and AFTER. Does not touch the `paper` flag.
"""
import json
import os
import sys

sys.path.insert(0, "/opt/bots/bitget-listener")

from dotenv import dotenv_values  # noqa: E402

os.environ.update({k: v for k, v in dotenv_values(
    "/opt/bots/bitget-listener/.env").items() if v})

# PG creds live in the systemd unit's Environment= lines, not .env — pull them in.
import re  # noqa: E402
import subprocess  # noqa: E402

unit = subprocess.run(["systemctl", "cat", "bitget-listener.service"],
                      capture_output=True, text=True).stdout
for m in re.finditer(r"^Environment=(PG[A-Z]+)=(.*)$", unit, re.M):
    os.environ[m.group(1)] = m.group(2).strip()

from src.config.db import PostgresConfigStore  # noqa: E402

store = PostgresConfigStore()
cfg = store.load()
print("BEFORE paper=", cfg.paper, json.dumps(cfg.params, indent=1))

cfg.params["max_concurrent"] = 1
cfg.params["product_type"] = "USDT-FUTURES"
store.upsert(cfg)

after = PostgresConfigStore().load()
print("AFTER  paper=", after.paper, json.dumps(after.params, indent=1))
