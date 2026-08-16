# 07 — Bitget V2 implementation notes (concrete payloads)

## 0. ✅ VERIFIED REALITY (probe run 2026-08-17, `scripts/bitget_api_probe.py`, GREEN=9 RED=0)

**This section SUPERSEDES every conflicting claim below it.** Everything here came from a live
response on this machine's IP. Sections 1–9 are kept for provenance; where they disagree with
this section, they are WRONG and are annotated `❌ SUPERSEDED`.

| # (§8 question) | Verdict | VERIFIED fact |
|---|---|---|
| 1. testnet / product | ✅ | `productType=umcbl` is **DEAD** (`code 40034 Parameter umcbl does not exist`). Live set: `USDT-FUTURES`, `COIN-FUTURES`, `USDC-FUTURES`, **`SUSDT-FUTURES`**, `SCOIN-FUTURES`, `SUSDC-FUTURES`. `SUSDT-FUTURES` is the **demo/testnet product on the same host** (`SBTCSUSDT` etc.) → that is our testnet-first path, no separate hostname. |
| 2. spec field names | ✅ | Doc's `minSize`/`maxSize`/`sizeIncrement`/`priceIncrement` **do not exist**. Real: `minTradeNum`, `sizeMultiplier`, `pricePlace` (decimals), `volumePlace` (decimals), `minTradeUSDT`, `makerFeeRate`, `takerFeeRate`, `symbolStatus`. |
| 3. side enum | ⚠ | V2 mix uses `side=buy\|sell` **plus** `tradeSide=open\|close` (hedge mode); one-way mode omits `tradeSide` and closes via `reduceOnly`. Doc's `open_long`/`close_short` is **V1 legacy**. Not provable without placing an order → adapter uses one-way + `reduceOnly`, unit-tested; needs a demo key to close out. |
| 4. preset TP/SL | ⚠ | `presetStopSurplusPrice` / `presetStopLossPrice` on `/order/place-order`; separate `/order/place-tpsl-order` for post-open protective orders. Requires a key to confirm end-to-end. |
| 5. dead-man switch | ⚠ | No documented `set-auto-cancel` for V2 mix → dead-man's switch is implemented **client-side** (watchdog → `cancel-all` of non-protective orders + flatten). |
| 6. fee field | ✅ | Fee **rates** are on the contract: `makerFeeRate=0.0002`, `takerFeeRate=0.0006` (VIP0), matching the assumption. Per-fill field is `fee` on `/order/fills` and is reported **NEGATIVE**; adapter normalizes to a positive cost. |
| 7. geo-block | ✅ | **No geo-block from this IP.** `HTTP 200 code=00000`. No Cloudflare Worker proxy needed (unlike MEXC). |

### Corrected endpoints / transport (all VERIFIED)
- REST base `https://api.bitget.com`, **`productType=USDT-FUTURES`**.
- Contracts: `GET /api/v2/mix/market/contracts?productType=USDT-FUTURES[&symbol=BTCUSDT]` (754 symbols).
- Ticker: `GET /api/v2/mix/market/ticker` — **SINGULAR**, not `/tickers`. Has `lastPr`, `indexPrice`, `markPrice`, `fundingRate`.
- Private account path: `GET /api/v2/mix/account/accounts` — **PLURAL** (`40006 Invalid ACCESS_KEY` when unsigned ⇒ path is real).
- Auth headers are **`ACCESS-KEY` / `ACCESS-SIGN` / `ACCESS-TIMESTAMP` / `ACCESS-PASSPHRASE`** (doc §2's `ACcess-Key` was a typo). Server confirmed it reads them (`40006`/`40037`, not `404`).
- **Passphrase is sent RAW**, *not* `base64(hmac(secret, passphrase))` — doc §2's pre-signing is the OKX variant and is WRONG for Bitget.
- **WS host `wss://ws.bitget.com/mix/v1/stream` is DEAD (HTTP 404).** Real: **`wss://ws.bitget.com/v2/ws/public`** and `/v2/ws/private`.
- WS `args` are **objects** `{"instType":"USDT-FUTURES","channel":"ticker","instId":"BTCUSDT"}` — NOT doc §5's `"tickers.BTCUSDT"` strings. Channel name is `ticker`, singular.
- WS heartbeat: **client sends the literal string `ping`, server replies literal `pong`** (plain text, not JSON).
- Sizing is in **BASE COIN**, not contracts (`PEPEUSDT: minTradeNum=1000, sizeMultiplier=1000, volumePlace=0`). 1000x-prefixed contracts DO exist: `1000BONKUSDT`, `1000SATSUSDT`, `1000000MOGUSDT`, `10000NEXUSDT`, etc. — so a plain `PEPEUSDT` *and* `1000BONKUSDT` style coexist and the SymbolMap must not assume a `1000` prefix.

---

> ⚠ **ORIGINAL UNVERIFIED ANALYSIS BELOW — superseded by §0 where they conflict.**
> Web docs were unreachable during this analysis. The V2 signing model and endpoint *shapes*
> below follow the documented Bitget V2 convention; exact query-string parameters, JSON field
> names, and WS channel strings must be validated against a live response and then pinned in this
> file (code > doc > chat, per the repos' own governance rule).

## 1. Base URLs (UNVERIFIED exact hostnames — verify)
| Purpose | URL (mainnet, assumed) | Testnet |
|---------|------------------------|---------|
| REST V2 | `https://api.bitget.com` | `https://api.bitget.com` (Bitget V2 has **no separate testnet host** for some products; test orders use a `test=true`/`businessType` flag or a dedicated test API key — **verify**) |
| WS (mix) | `wss://ws.bitget.com/mix/v1/stream` | same host (test flag in subscribe) |

> ACTION: confirm whether Bitget exposes a true futures testnet for `UMCBL`. If not, the
> safe gate becomes "test mode on mainnet with tiny notional + paper flag", per the bybit
> testnet-first philosophy adapted.

## 2. Auth — V2 HMAC-SHA256 (UNIFIED signature)
Bitget V2 unified header auth:
```
timestamp = str(int(time.time()*1000))          # milliseconds, string
prehash   = timestamp + method.toUpperCase() + requestPath + body   # body = "" for GET
sign      = base64( hmac_sha256(secret, prehash) )                  # BASE64, not hex
headers:
  ACcess-Key:       <api_key>
  ACcess-Sign:      <base64 sign>
  ACcess-Timestamp: <timestamp>
  ACcess-Passphrase:<base64( hmac_sha256(secret, passphrase) )>     # passphrase pre-signed ONCE
  Content-Type:     application/json
```
- `requestPath` = path + query string (e.g. `/api/v2/mix/order/place?symbol=BTCUSDT`).
- For GET: `body` is empty string `""` (NOT the query string — the query is in `requestPath`).
- For POST: `body` = the raw JSON request body string (compact).
- Passphrase is signed once at startup: `ACcess-Passphrase = base64(hmac_sha256(secret, passphrase))`.

Reference implementation shape (mirrors `bybit.py` `BybitAuth`):
```python
class BitgetAuth:
    def __init__(self, key, secret, passphrase):
        self.key = key
        self.secret = secret
        self.passphrase = base64.b64encode(
            hmac.new(secret.encode(), passphrase.encode(), hashlib.sha256).digest()
        ).decode()

    def headers(self, method, request_path, body=""):
        ts = str(int(time.time()*1000))
        prehash = ts + method.upper() + request_path + body
        sign = base64.b64encode(
            hmac.new(self.secret.encode(), prehash.encode(), hashlib.sha256).digest()
        ).decode()
        return {
            "ACcess-Key": self.key,
            "ACcess-Sign": sign,
            "ACcess-Timestamp": ts,
            "ACcess-Passphrase": self.passphrase,
            "Content-Type": "application/json",
        }
```

## 3. REST endpoints to implement (UNVERIFIED exact paths — verify in probe)
All under `https://api.bitget.com`. `productType=umcbl` identifies USDT-M futures.

### Public (no auth)
| Method & path | Purpose |
|---------------|---------|
| `GET /api/v2/mix/market/contracts?productType=umcbl&symbol=BTCUSDT` | Instrument spec: `minSize`, `maxSize`, `sizeIncrement`, `priceIncrement`, `maxLeverage`, `symbol`. **Confirm field names.** |
| `GET /api/v2/mix/market/tickers?productType=umcbl&symbol=BTCUSDT` | Last/mark price. |
| `GET /api/v2/mix/market/fills?productType=umcbl&symbol=BTCUSDT` | Recent public trades (optional). |

### Signed
| Method & path | Purpose | Key body fields (UNVERIFIED) |
|---------------|---------|------------------------------|
| `POST /api/v2/mix/order/place` | Place opening LIMIT/MARKET. | `symbol`, `productType=umcbl`, `marginMode=isolated`, `side=open_long/open_short`, `orderType=limit/market`, `price`, `size`, `leverage`, `clientOid`, `timeInForce`, `presetTakeProfitPrice`, `presetStopLossPrice` (or `autoTpSl`) |
| `POST /api/v2/mix/order/place-plan` | Plan/trigger (SL/TP) or TP/SL attached to position; reduce-only close. | `symbol`, `productType=umcbl`, `side=close_long/close_short`, `orderType=market/limit`, `triggerPrice`, `price`, `size`, `reduceOnly=true`, `presetTakeProfitPrice`/`presetStopLossPrice` |
| `POST /api/v2/mix/order/cancel` | Cancel by `orderId`/`clientOid`. | `symbol`, `productType=umcbl`, `orderId` |
| `POST /api/v2/mix/order/cancel-all` | Mass cancel. | `symbol`, `productType=umcbl`, `marginMode` |
| `GET /api/v2/mix/order/open-orders?...` | Open orders. | `symbol`, `productType=umcbl` |
| `POST /api/v2/mix/position/set-leverage` | Set leverage. | `symbol`, `productType=umcbl`, `leverage`, `marginMode`, `holdSide=long/short` |
| `GET /api/v2/mix/position/single-position?symbol=...&productType=umcbl` | Open position + real `liquidationPrice`. | — |
| `GET /api/v2/mix/account/account?productType=umcbl` | Balance (`available`). | — |
| `GET /api/v2/mix/account/account` | Assets/equity. | — |
| `POST /api/v2/mix/order/set-auto-cancel` | Dead-man's switch (Bitget-native `scheduleCancel` equiv). | `symbol`, `productType=umcbl`, `cancelType`, `delaySec` |

> **Side semantics note:** Bitget V2 uses **string sides** `open_long` / `open_short` /
> `close_long` / `close_short` (similar spirit to MEXC's integer sides, but strings). The
> adapter's `_order_body` must translate the internal `Side.BUY/SELL` + open/close intent into
> these. Confirm exact enum strings in probe.

## 4. Rounding / validation (inherit MEXC Edge C)
- `round_price(symbol, price)`: step DOWN to `priceIncrement`; clamp UP to `minPrice` only if
  `minPrice <= rounded*10`.
- `round_qty(symbol, qty, price_ref)`: step to `sizeIncrement`, then loop up until
  `qty*price >= minNotional`; clip `[minSize, maxSize]`.
- `validate_order`: sizeIncrement step, min/max size, `qty*price>=minNotional`, price bounds.
- Idempotency: `clientOid` UNIQUE in DB + `get_active_for_decision()` guard → `DUPLICATE_SKIPPED`.
- On recoverable reject → deterministic `RepairOrder` (reload spec → re-round → re-validate →
  resubmit once). **No LLM fallback for trades.**

## 5. WebSocket (mix) payloads (UNVERIFIED channel strings — verify)
Single connection, signed login, then subscribe:
```jsonc
// 1. connect wss://ws.bitget.com/mix/v1/stream
// 2. login (pbl = private/business auth)
{
  "op": "login",
  "args": ["<api_key>", "<timestamp>", "<base64 hmac of (timestamp+key+passphrase-secret)>"]
}
// 3. subscribe public tickers (mark price)
{
  "op": "subscribe",
  "args": ["tickers.BTCUSDT", "tickers.ETHUSDT"],
  "instType": "UMCBL"
}
// 4. subscribe private (orders / positions / account)
{
  "op": "subscribe",
  "args": ["orders", "positions", "account"],
  "instType": "UMCBL"
}
```
- **Heartbeat:** server `ping` → client `pong` (mirror MexcWS). Reset dead-man TTL.
- **Public `tickers.{SYM}`** → mark price into cache; `mark_price(sym)` port method.
- **Private `orders`** → `on_fill` with real `fee` (BUG A). **`positions`** → `on_position` with
  real `liquidationPrice` (BUG B). **`account`** → `on_wallet` (USDT available).
- Reconnect: jittered backoff (cap 30s); `on_ready` on first successful subscribe, `on_lost` on
  exhaustion.

## 6. Fee model (single source, inherit MEXC §7)
- Bitget maker/taker ~ `0.0002`/`0.0006` (VIP0 USDT-M) — **verify exact values** in probe.
- `FixedRiskSizer.net = gross − (open_fee + close_fee)`.

## 7. Mapping to the existing repos (what to copy verbatim)
| Need | Copy from | File |
|------|-----------|------|
| Adapter port + DI | bybit | `interfaces/ports.py`, `exchanges/bybit.py` structure |
| Signing/rounding centralized + tested | bybit | `BybitAuth`, `round_qty/round_price` |
| BUG A/B/C handling | bybit + mexc | `get_fills` execFee, real liqPrice, never-cancel-reduce-only |
| `validate_order` + `RepairOrder` | mexc | `exchanges/repair.py`, `mexc.py` |
| WS login+filter+heartbeat+reconnect | mexc | `streaming/mexc_ws.py` (adapt channel strings) |
| Web UI + WS events + ShadowLedger | mexc | `web/app.py`, `eval/shadow_ledger.py` |
| DB multi-bot + persisted paper flag | hyperliquid | `config/db.py`, `orchestrator.py` `/toggle` |
| Management command fast-path | binance | `orchestrator.py` `_handle_tp1_booked` etc. |

## 8. Open questions to resolve in the probe (must be GREEN before go-live)
1. Exact V2 host for `UMCBL` testnet (or test-flag mechanism).
2. Exact instrument-spec field names (`sizeIncrement` vs `minTradeSize`, etc.).
3. Exact side enum strings (`open_long`/`close_short` vs other casing).
4. Whether `presetTakeProfitPrice`/`presetStopLossPrice` attach at entry, or a separate
   `place-plan` is required (drives SL/TP strategy §5).
5. Whether `set-auto-cancel` exists for `UMCBL` (dead-man's switch native path).
6. Real `fee` field name on fills (`execFee`? `fee`?).
7. Geo-block status from Sera's Jakarta IP.

## 9. Delivery checklist for the build
- [ ] `bitget_api_probe.py` written and GREEN (answers §8).
- [ ] `bitget_auth.py` unit-tested against the V2 algorithm (like bybit's sign test).
- [ ] `bitget.py` adapter: place/cancel/leverage/positions/balance/fills, all over `_signed_request`.
- [ ] `bitget_ws.py`: login + subscribe + heartbeat + reconnect + callbacks.
- [ ] `validate_order` + `RepairOrder` (no LLM fallback).
- [ ] Web UI + `/ws/events` + ShadowLedger.
- [ ] Postgres `fatty_config` row + persisted `paper` flag + `/toggle`.
- [ ] ≥81 fakes-only tests, zero network.
- [ ] `deploy.sh` blocks deploy if `[Unreleased]` empty or tests fail (copy bybit guard).
