# 07 — Bitget V2 implementation notes (concrete payloads)

> ⚠ **UNVERIFIED — confirm with `bitget_api_probe.py` (see 06 §9) before writing order code.**
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
