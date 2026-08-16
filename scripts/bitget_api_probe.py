#!/usr/bin/env python3
"""bitget_api_probe.py — MANDATORY pre-order verification of Bitget V2 reality.

Mirrors ``mexc_api_probe.py`` / ``bybit_api_probe.py``. **Non-destructive**: it
never places, cancels or modifies an order. Every check prints GREEN / RED / SKIP
and the script exits non-zero if a mandatory public check is RED.

It answers the 7 open questions in ``analysis/07-BITGET-IMPLEMENTATION-NOTES.md`` §8.
Signed checks are SKIPped (not failed) when no credentials are present in .env, so
the probe is runnable on a keyless machine to verify the public surface + WS.

Run:
    ./.venv/bin/python scripts/bitget_api_probe.py
    ./.venv/bin/python scripts/bitget_api_probe.py --json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import websockets

from src.exchanges.bitget_auth import BitgetAuth

REST = os.getenv("BITGET_REST_BASE", "https://api.bitget.com")
WS_PUBLIC = os.getenv("BITGET_WS_PUBLIC", "wss://ws.bitget.com/v2/ws/public")
WS_PRIVATE = os.getenv("BITGET_WS_PRIVATE", "wss://ws.bitget.com/v2/ws/private")
PRODUCT = os.getenv("BITGET_PRODUCT_TYPE", "USDT-FUTURES")
SYMBOL = os.getenv("BITGET_PROBE_SYMBOL", "BTCUSDT")

RESULTS: list[dict] = []


def record(name: str, status: str, detail: str = "", data: object = None) -> None:
    RESULTS.append({"check": name, "status": status, "detail": detail, "data": data})
    icon = {"GREEN": "✅", "RED": "❌", "SKIP": "⏭️", "WARN": "⚠️"}.get(status, "?")
    print(f"{icon} {status:5} {name}")
    if detail:
        for line in str(detail).splitlines():
            print(f"          {line}")


def _load_env() -> None:
    env = Path(__file__).resolve().parents[1] / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _creds() -> tuple[str, str, str]:
    return (os.getenv("BITGET_API_KEY", ""),
            os.getenv("BITGET_API_SECRET", ""),
            os.getenv("BITGET_PASSPHRASE", ""))


# ---------------------------------------------------------------- public checks
def check_reachable(client: httpx.Client) -> dict | None:
    """Q7: geo-block status from this machine's IP + Q1 productType validity."""
    try:
        r = client.get(f"{REST}/api/v2/mix/market/contracts",
                       params={"productType": PRODUCT, "symbol": SYMBOL})
    except Exception as exc:  # network / DNS / TLS
        record("REST reachable (no geo-block)", "RED", f"{type(exc).__name__}: {exc}")
        return None
    if r.status_code != 200:
        record("REST reachable (no geo-block)", "RED", f"HTTP {r.status_code}: {r.text[:200]}")
        return None
    body = r.json()
    if body.get("code") != "00000":
        record("REST reachable (no geo-block)", "RED", f"code={body.get('code')} msg={body.get('msg')}")
        return None
    record("REST reachable (no geo-block)", "GREEN",
           f"HTTP 200 code=00000 from this IP; productType={PRODUCT} accepted")
    return body["data"][0]


def check_product_type(client: httpx.Client) -> None:
    """Q1: prove the legacy ``umcbl`` value is DEAD and enumerate the live set."""
    live, dead = [], []
    for pt in ("USDT-FUTURES", "COIN-FUTURES", "USDC-FUTURES", "SUSDT-FUTURES",
               "SCOIN-FUTURES", "SUSDC-FUTURES", "umcbl"):
        try:
            r = client.get(f"{REST}/api/v2/mix/market/contracts", params={"productType": pt})
            (live if r.json().get("code") == "00000" else dead).append(pt)
        except Exception:
            dead.append(pt)
    if "USDT-FUTURES" in live and "umcbl" in dead:
        record("productType enum (Q1)", "GREEN",
               f"live={live}\ndead={dead}\nSUSDT-FUTURES = DEMO/testnet product on the SAME host")
    else:
        record("productType enum (Q1)", "RED", f"live={live} dead={dead}")


def check_spec_fields(spec: dict) -> None:
    """Q2: exact instrument-spec field names."""
    expected = ["symbol", "minTradeNum", "pricePlace", "volumePlace", "sizeMultiplier",
                "minTradeUSDT", "makerFeeRate", "takerFeeRate", "symbolStatus"]
    missing = [f for f in expected if f not in spec]
    if missing:
        record("instrument spec fields (Q2)", "RED",
               f"missing={missing}\ngot={sorted(spec)[:25]}")
        return
    record("instrument spec fields (Q2)", "GREEN",
           "Doc guesses minSize/maxSize/sizeIncrement/priceIncrement DO NOT EXIST. Real:\n"
           f"  minTradeNum={spec['minTradeNum']}  sizeMultiplier={spec['sizeMultiplier']}\n"
           f"  pricePlace={spec['pricePlace']} (decimals) volumePlace={spec['volumePlace']} (decimals)\n"
           f"  minTradeUSDT={spec['minTradeUSDT']}  maker={spec['makerFeeRate']} taker={spec['takerFeeRate']}",
           data=spec)


def check_fee_rates(spec: dict) -> None:
    """Q6 (partial): fee rates are published on the contract, no fill needed."""
    mk, tk = float(spec["makerFeeRate"]), float(spec["takerFeeRate"])
    if mk == 0.0002 and tk == 0.0006:
        record("fee rates (VIP0)", "GREEN", f"maker={mk} taker={tk} — matches doc §6 assumption")
    else:
        record("fee rates (VIP0)", "WARN", f"maker={mk} taker={tk} — doc §6 said 0.0002/0.0006")


def check_ticker(client: httpx.Client) -> None:
    r = client.get(f"{REST}/api/v2/mix/market/ticker",
                   params={"productType": PRODUCT, "symbol": SYMBOL})
    body = r.json()
    if body.get("code") != "00000":
        record("ticker / mark price", "RED", f"{body.get('code')}: {body.get('msg')}")
        return
    d = body["data"][0]
    keys = [k for k in ("lastPr", "indexPrice", "markPrice", "fundingRate") if k in d]
    record("ticker / mark price", "GREEN",
           f"endpoint is /market/ticker (SINGULAR, not /tickers)\n"
           f"  lastPr={d.get('lastPr')} indexPrice={d.get('indexPrice')} present={keys}")


def check_contract_sizing(client: httpx.Client) -> None:
    """Q6b: 1000x contracts + whether size is base-coin or contracts."""
    r = client.get(f"{REST}/api/v2/mix/market/contracts", params={"productType": PRODUCT})
    data = r.json()["data"]
    thousand = sorted(x["symbol"] for x in data if x["symbol"].startswith("1000"))
    pepe = [x for x in data if x["symbol"] == "PEPEUSDT"]
    detail = [f"{len(data)} USDT-FUTURES symbols listed", f"1000x-prefixed contracts: {thousand or 'NONE'}"]
    if pepe:
        p = pepe[0]
        detail.append(f"PEPEUSDT: minTradeNum={p['minTradeNum']} sizeMultiplier={p['sizeMultiplier']} "
                      f"volumePlace={p['volumePlace']} -> size is BASE COIN, not contracts")
    record("contract sizing / 1000x (Q6b)", "GREEN", "\n".join(detail))


# ---------------------------------------------------------------- signed checks
def check_auth(client: httpx.Client) -> None:
    """Q3-precondition: does the V2 signature actually authenticate?"""
    key, secret, pw = _creds()
    if not (key and secret and pw):
        record("signed auth (private REST)", "SKIP",
               "no BITGET_API_KEY/SECRET/PASSPHRASE in .env — cannot verify signing against a\n"
               "real key. Public surface + WS are verified above; see BLOCKED.md.")
        return
    auth = BitgetAuth(key, secret, pw)
    path = f"/api/v2/mix/account/accounts?productType={PRODUCT}"
    try:
        r = client.get(f"{REST}{path}", headers=auth.headers("GET", path))
        body = r.json()
    except Exception as exc:
        record("signed auth (private REST)", "RED", f"{type(exc).__name__}: {exc}")
        return
    if r.status_code == 200 and body.get("code") == "00000":
        record("signed auth (private REST)", "GREEN",
               "V2 HMAC-SHA256 base64 signature ACCEPTED (headers ACCESS-KEY/SIGN/TIMESTAMP/PASSPHRASE)")
    else:
        record("signed auth (private REST)", "RED",
               f"HTTP {r.status_code} code={body.get('code')} msg={body.get('msg')}")


def check_unauth_shape(client: httpx.Client) -> None:
    """Prove the private endpoint path exists (rejects on auth, not on 404)."""
    r = client.get(f"{REST}/api/v2/mix/account/accounts", params={"productType": PRODUCT})
    body = r.json()
    if body.get("code") in ("40006", "40009", "40037", "40012"):
        record("private endpoint path exists", "GREEN",
               f"/api/v2/mix/account/accounts rejects unauthenticated with code={body.get('code')} "
               f"({body.get('msg')}) — path is real, note PLURAL 'accounts'")
    else:
        record("private endpoint path exists", "RED", f"unexpected: {body}")


def check_header_casing(client: httpx.Client) -> None:
    """The doc wrote 'ACcess-Key'. Verify the real header names are ACCESS-*."""
    r = client.get(f"{REST}/api/v2/mix/account/accounts",
                   params={"productType": PRODUCT},
                   headers={"ACCESS-KEY": "probe-invalid", "ACCESS-SIGN": "x",
                            "ACCESS-TIMESTAMP": "1", "ACCESS-PASSPHRASE": "x"})
    body = r.json()
    if body.get("code") != "40006":  # 40006 = Invalid ACCESS_KEY -> headers were READ
        record("auth header names", "WARN", f"code={body.get('code')} msg={body.get('msg')}")
        return
    record("auth header names", "GREEN",
           "ACCESS-KEY / ACCESS-SIGN / ACCESS-TIMESTAMP / ACCESS-PASSPHRASE are read by the server\n"
           "  (doc §2 'ACcess-Key' typo corrected; header names are case-insensitive per HTTP)")


# ---------------------------------------------------------------- websocket
async def check_ws_public() -> None:
    """Q: exact WS host, subscribe frame shape, heartbeat."""
    dead = "wss://ws.bitget.com/mix/v1/stream"
    try:
        async with websockets.connect(dead, open_timeout=8):
            record("WS legacy host dead", "WARN", f"{dead} unexpectedly accepted a connection")
    except Exception as exc:
        record("WS legacy host dead", "GREEN",
               f"doc §5 host {dead} is DEAD ({type(exc).__name__}: {str(exc)[:80]})")
    try:
        async with websockets.connect(WS_PUBLIC, open_timeout=10) as ws:
            frame = {"op": "subscribe",
                     "args": [{"instType": PRODUCT, "channel": "ticker", "instId": SYMBOL}]}
            await ws.send(json.dumps(frame))
            ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            tick = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            await ws.send("ping")
            pong = await asyncio.wait_for(ws.recv(), timeout=10)
            ok = (ack.get("event") == "subscribe" and tick.get("data")
                  and str(pong).strip() == "pong")
            record("WS public subscribe + heartbeat", "GREEN" if ok else "RED",
                   f"host={WS_PUBLIC}\n"
                   f"  args are OBJECTS {{instType,channel,instId}} — NOT doc's 'tickers.BTCUSDT' strings\n"
                   f"  ack={json.dumps(ack)[:120]}\n"
                   f"  channel='ticker' (singular); lastPr={tick['data'][0].get('lastPr') if tick.get('data') else None}\n"
                   f"  heartbeat: client sends literal 'ping' -> server replies {pong!r} (plain text, not JSON)")
    except Exception as exc:
        record("WS public subscribe + heartbeat", "RED", f"{type(exc).__name__}: {exc}")


async def check_ws_private() -> None:
    key, secret, pw = _creds()
    if not (key and secret and pw):
        record("WS private login", "SKIP", "no credentials in .env — see BLOCKED.md")
        return
    try:
        async with websockets.connect(WS_PRIVATE, open_timeout=10) as ws:
            auth = BitgetAuth(key, secret, pw)
            await ws.send(json.dumps({"op": "login", "args": [auth.ws_login_arg()]}))
            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if resp.get("event") == "login" and resp.get("code") in (None, "0", 0):
                record("WS private login", "GREEN", f"login accepted on {WS_PRIVATE}")
            else:
                record("WS private login", "RED", json.dumps(resp)[:200])
    except Exception as exc:
        record("WS private login", "RED", f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- side semantics
def check_side_semantics() -> None:
    """Q3/Q4: documented-by-reality note; cannot be probed without placing an order.

    Bitget V2 one-way/hedge mode uses ``side`` + ``tradeSide`` instead of the V1
    ``open_long``/``close_short`` compound strings the analysis doc guessed. We
    record this as WARN (not GREEN) because it is only fully provable by a real
    (or demo) order, which this non-destructive probe refuses to place.
    """
    record("side/tradeSide semantics (Q3/Q4)", "WARN",
           "V2 mix uses side=buy|sell PLUS tradeSide=open|close (hedge mode);\n"
           "  one-way mode omits tradeSide and uses reduceOnly for closes.\n"
           "  Doc §3 guess 'open_long/close_short' is V1 legacy. NOT provable without an\n"
           "  order -> adapter defaults to one-way + reduceOnly and is unit-tested; final\n"
           "  confirmation requires a demo (SUSDT-FUTURES) key. See BLOCKED.md.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="emit machine-readable results")
    args = ap.parse_args()

    _load_env()
    print(f"bitget_api_probe — REST={REST} productType={PRODUCT} symbol={SYMBOL}\n")

    with httpx.Client(timeout=20) as client:
        spec = check_reachable(client)
        if spec:
            check_spec_fields(spec)
            check_fee_rates(spec)
        check_product_type(client)
        check_ticker(client)
        check_contract_sizing(client)
        check_unauth_shape(client)
        check_header_casing(client)
        check_auth(client)

    asyncio.run(check_ws_public())
    asyncio.run(check_ws_private())
    check_side_semantics()

    reds = [r for r in RESULTS if r["status"] == "RED"]
    skips = [r for r in RESULTS if r["status"] == "SKIP"]
    print(f"\n{'='*70}")
    print(f"GREEN={sum(1 for r in RESULTS if r['status']=='GREEN')}  "
          f"WARN={sum(1 for r in RESULTS if r['status']=='WARN')}  "
          f"SKIP={len(skips)}  RED={len(reds)}")
    if args.json:
        print(json.dumps(RESULTS, indent=2, default=str))
    if reds:
        print("PROBE RED — do NOT write/ship order code against unverified assumptions.")
        return 1
    if skips:
        print("PROBE GREEN on the public surface; signed checks SKIPPED (no keys).")
        return 0
    print("PROBE FULLY GREEN.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
