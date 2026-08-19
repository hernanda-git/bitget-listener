"""Probe: open a REAL public Bitget WS with an EMPTY whitelist (production config)
and capture (1) the exact subscribe frame sent, (2) the first live ticker frame.

Run: python scripts/probe_ws_sentinel.py
No API keys, no trades — pure public market-data liveness check.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("probe_ws_sentinel")

from src.streaming.bitget_ws import BitgetWS


async def main() -> None:
    # Mirror production: empty whitelist, real USDT-FUTURES product type.
    ws = BitgetWS(symbols=[], product_type="USDT-FUTURES")
    got: list[dict] = []
    ready = []

    def on_tick(sym, px):
        got.append({"sym": sym, "px": px, "ts": time.time()})

    ws.on_tick = on_tick
    ws.on_ready = lambda: ready.append(True)

    # Capture the exact frame by wrapping the connector's send.
    import websockets

    real_connect = websockets.connect

    def intercept(url, **kw):
        class _Ctx:
            async def __aenter__(self_inner):
                conn = await real_connect(url, **kw)
                real_send = conn.send

                async def send(payload, *a, **k):
                    log.info("SENT FRAME: %s", payload)
                    return await real_send(payload, *a, **k)

                conn.send = send
                return conn

            async def __aexit__(self_inner, *a):
                return False

        return _Ctx()

    ws._connector = intercept

    await ws.start()
    # wait up to 12s for a live BTCUSDT ticker to prove the feed is alive
    for _ in range(120):
        if got:
            break
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.3)
    await ws.stop()

    print("\n===== RAW RESULT =====")
    print("ready_emitted:", bool(ready))
    print("ticker_frames_received:", len(got))
    if got:
        g = got[0]
        print(f"first_ticker: {g['sym']} @ {g['px']} (epoch {g['ts']:.0f})")
        print("feed_alive:", g["sym"] == "BTCUSDT")


if __name__ == "__main__":
    asyncio.run(main())
