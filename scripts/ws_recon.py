"""Throwaway WS reconnaissance (not part of the build) — confirm Bitget V2 mix WS reality."""
import asyncio, json, websockets

URLS = [
    "wss://ws.bitget.com/v2/ws/public",
    "wss://ws.bitget.com/mix/v1/stream",
]

async def probe(url):
    try:
        async with websockets.connect(url, open_timeout=10) as ws:
            sub = {"op": "subscribe", "args": [{"instType": "USDT-FUTURES", "channel": "ticker", "instId": "BTCUSDT"}]}
            await ws.send(json.dumps(sub))
            out = []
            for _ in range(3):
                out.append(await asyncio.wait_for(ws.recv(), timeout=10))
            await ws.send("ping")
            out.append(await asyncio.wait_for(ws.recv(), timeout=10))
            print(f"OK {url}")
            for o in out:
                print("   ", str(o)[:300])
    except Exception as e:
        print(f"FAIL {url}: {type(e).__name__}: {e}")

async def main():
    for u in URLS:
        await probe(u)

asyncio.run(main())
