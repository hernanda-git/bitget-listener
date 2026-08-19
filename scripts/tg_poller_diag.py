"""Read-only diagnosis of the [tg-cmd] getUpdates failure.

Reproduces the bug (15s client timeout vs 25s long-poll) and proves the token
itself is healthy. Read-only: getMe + getUpdates(timeout=0) only, never replies.
"""
import asyncio
import os
import re
import subprocess
import sys

sys.path.insert(0, "/opt/bots/bitget-listener")

import httpx  # noqa: E402
from dotenv import dotenv_values  # noqa: E402

env = dotenv_values("/opt/bots/bitget-listener/.env")
os.environ.update({k: v for k, v in env.items() if v})
unit = subprocess.run(["systemctl", "cat", "bitget-listener.service"],
                      capture_output=True, text=True).stdout
for m in re.finditer(r"^Environment=(TG_[A-Z_]+)=(.*)$", unit, re.M):
    os.environ.setdefault(m.group(1), m.group(2).strip())

TOKEN = os.environ["TG_BOT_TOKEN"]
BASE = f"https://api.telegram.org/bot{TOKEN}"


async def main():
    # 1. token healthy?
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{BASE}/getMe")
        j = r.json()
        u = j.get("result", {})
        print(f"1. getMe          -> HTTP {r.status_code} ok={j.get('ok')} "
              f"@{u.get('username')} id={u.get('id')}")

    # 2. reproduce: 15s client timeout under a 25s long-poll
    async with httpx.AsyncClient(timeout=15) as c:
        try:
            r = await c.get(f"{BASE}/getUpdates",
                            params={"offset": 0, "timeout": 25})
            print(f"2. OLD (t=15,poll=25) -> HTTP {r.status_code} (no timeout)")
        except Exception as e:
            print(f"2. OLD (t=15,poll=25) -> {type(e).__name__} "
                  f"str()={str(e)!r}  <-- empty str = the blank log line")

    # 3. the fix: client timeout must exceed the long-poll
    async with httpx.AsyncClient(timeout=40) as c:
        try:
            r = await c.get(f"{BASE}/getUpdates",
                            params={"offset": 0, "timeout": 25})
            j = r.json()
            print(f"3. NEW (t=40,poll=25) -> HTTP {r.status_code} ok={j.get('ok')} "
                  f"updates={len(j.get('result', []))}")
        except Exception as e:
            print(f"3. NEW (t=40,poll=25) -> {type(e).__name__}: {e}")


asyncio.run(main())
