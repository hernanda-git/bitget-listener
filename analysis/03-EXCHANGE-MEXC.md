# 03 — MEXC USDⓈ-M Futures (V1): REST + WebSocket (as implemented)

Source: `mexc-listener/src/exchanges/mexc.py`, `mexc_auth.py`, `streaming/mexc_ws.py`,
plus `docs/CONCRETE-SPEC.md` + `cloudflare/mexc-proxy`.

> Settlement = USDT. MEXC splits its API across two hosts:
> - **Unified + futures REST:** `https://api.mexc.com` (`/api/v1/...`, `/api/v1/private/...`,
>   `/api/v1/contract/...`).
> - **Futures WebSocket:** `wss://contract.mexc.com/edge` (the old `/ws` path returns HTTP 301
>   → broken; `/edge` is correct).
>
> **Jakarta geo-block:** from Sera's IP, `contract.mexc.com` (WS + futures REST) is geo-filtered.
> A **Cloudflare Worker proxy** (`mexc-proxy.m-hernanda95.workers.dev`) tunnels REST (`/api/*`)
> and WS (`/ws` → `wss://contract.mexc.com/edge`) from Cloudflare's non-ID edge. The bot sets
> `MEXC_WS_URL`/`MEXC_BASE_URL` to the Worker when deployed.

## 1. Auth (`mexc_auth.py`) — fresh, NOT reused from Bybit/Binance
```
sign = HMAC-SHA256(secret, accessKey + Request-Time + paramString)
headers: ApiKey, Request-Time (ms string), Signature
```
- **GET:** `paramString = sorted(query_params, "&")` — i.e. `k1=v1&k2=v2` sorted by key.
- **POST:** `paramString = raw JSON body string` (compact `separators=(",",":")`, **no sorting**).
  The body is sent as the exact string that was signed (`content=body_str`), so the server-side
  signature matches what was transmitted.
- `Recv-Window` optional; server clock-skew tolerance ±10s (max 60s).
- **Token bucket:** 300 req/min.
- Response envelope: `{"success": bool, "code": int (0=ok), "data": {...}}`.

## 2. REST endpoints used (`mexc.py`)
### Public (no auth)
| Endpoint | Function | Notes |
|----------|----------|-------|
| `GET /api/v1/contract/detail?symbol=` | `_fetch_detail` | Instrument spec: `priceUnit`/`volUnit` (→ tick/lot), `contractSize`, `minVol`, `maxVol`, `maxLeverage`. Keyed `BTC_USDT` (underscore!). |
| `GET /api/v1/contract/ticker?symbol=` | `get_mark` | Live last/mark price (detail endpoint has NO live price). |
| `GET /api/v1/contract/ticker` | `get_all_symbols` | All perps + lastPrice (dashboard autocomplete). Cached 60s. |

### Signed
| Method & path | Function | Notes |
|---------------|----------|-------|
| `POST /api/v1/private/position/change_leverage` | `set_leverage` | Body `{symbol, leverage, openType(1=cross,2=isolated), positionType:2}`. |
| `POST /api/v1/private/order/create` | `place_order` (OPENING) | See §4. |
| `POST /api/v1/private/order/submit` | `place_order` (PROTECTIVE) / `close` | reduceOnly stop/tp and close. |
| `GET /api/v1/private/position/open_positions` | `get_position(s)` | Real `liquidatePrice` (F7). |
| `GET /api/v1/private/account/assets` | `get_balance` | Per-currency `equity`/`availableBalance`; summed to USDT. |
| `GET /api/v1/private/order/openOrders` | (planned) open orders | |

## 3. Symbol conventions (critical)
- **REST symbol:** `BTCUSDT` (no separator).
- **WS symbol:** `BTC_USDT` (underscore).
- Centralize `to_ws_symbol("BTCUSDT") -> "BTC_USDT"` and reverse. The probe initially looked for
  `BTCUSDT` on the WS/detail and gave a **false RED** — real shape is underscore.
- **1000× / contract multiplier (F11):** same hazard class as Binance `1000PEPE`. Registry built
  from `/contract/detail`; `contract_size` applied to qty math (1 contract = `contractSize` base
  units; BTC=0.0001, INJ=1). `_vol_contracts()` converts base qty → integer contract count.

## 4. Order building (`place_order` / `close`)
MEXC uses **integer `side`** and **contract `vol`**, not string side + base qty:
```
side semantics (confirmed via ccxt):
  1 = open long,  2 = close short,  3 = open short,  4 = close long
positionType: 1 = long, 2 = short
openType:     2 = isolated, 1 = cross
type:        1 = limit, 2 = market
vol:         number of CONTRACTS (not base qty)
leverage:    int (per-order)
```
- **Opening LIMIT:** `side = 1 (BUY→long) | 3 (SELL→short)`, `type=1`, `price` = rounded mark.
- **Opening MARKET:** `type=2`, **still requires a price reference** (else `2007`) → send live
  mark so the market order is accepted.
- **Protective STOP/TP:** route to `/order/submit` with `reduceOnly=true`, `timeInForce=GTC`,
  both `stopPrice` + `price`.
- **Close:** `/order/submit`, `side = 4 (SELL→close long) | 2 (BUY→close short)`, `reduceOnly=true`,
  `type=2` (market) or `type=1` (limit if price given).

## 5. Rounding / validation (Edge C, CONCRETE-SPEC §5)
- `round_price(symbol, price)`: step **DOWN** to `tickSize`; clamp **UP** to `minPrice` only if
  `minPrice <= rounded*10` (never inflate a $0.16 coin to $100).
- `round_qty(symbol, qty, price_ref)`: step to `lotSize`, then **loop increasing qty** until
  `qty*price_ref >= minNotional` (seed `minNotional/price`). Clip to `[minVol, maxVol]`.
- `validate_order`: tickSize step, minQty/maxQty, `qty*price>=minNotional`,
  `minPrice<=price<=maxPrice` → else `VALIDATION_SKIP`.
- Idempotency: `client_order_id` UNIQUE in DB + `get_active_for_decision()` guard →
  `DUPLICATE_SKIPPED` before placing.

## 6. WebSocket (`mexc_ws.py` — `MexcWS`)
MEXC has **no listenKey** — flow is `login` then `personal.filter`:
1. Connect to `wss://contract.mexc.com/edge`.
2. Send `{"method":"login","param":{"apiKey","signature","reqTime"}}`. Login signature =
   `auth.sign("", req_time)` (paramString empty for login).
3. Send `{"method":"personal.filter","param":{"filters":[
     {"filter":"order"},{"filter":"order.deal"},
     {"filter":"position","rules":["BTC_USDT",...]},
     {"filter":"asset"}]}}`.
4. `rs.login` / `rs.personal.filter` → `success` mark feed LIVE (`on_ready`).
5. **Heartbeat:** MEXC sends `{"method":"ping"}`; bot must reply `{"method":"pong"}` or the link
   drops. Bot also sends its own ping every `heartbeat_interval` (default 20s). `ping`/`pong`
   reset the dead-man watchdog (`on_heartbeat`).
6. Channels dispatched:
   - `push.personal.order.deal` → `on_fill` (`fee`→`Fill.fee`, F6; `feeCoin`; real pnl).
   - `push.personal.position` → `on_position` (`liquidatePrice`→`Position.liq_price`, F7).
   - `push.personal.order` → `on_order`.
   - `push.personal.asset` → `on_wallet`.
7. Reconnect: jittered exponential backoff (capped 30s), `max_retries=10`; on exhaustion →
   `on_lost` (feed DOWN).

## 7. Safety / resilience specifics
- **No native `scheduleCancel`** → a watchdog (`src/services/deadman.py`) cancels all open orders
  + flattens if no heartbeat within TTL (e.g. 60s) in live mode.
- **Ban-aware backoff** (`BanBackoff`): back off, don't hammer.
- **Geo-block workaround:** Cloudflare Worker proxy in front of `contract.mexc.com`.
- **Fee model (single source):** maker `0.0002`, taker `0.0005` (VIP0); `net_usd = gross − (open_fee + close_fee)`.

## 8. Status
P0–P3 + Items 1–7 code-complete; **128 fakes-only unit tests + 5 gated real-DB integration,
green**. NOT go-live; paper-by-default. Probe (`mexc_api_probe.py`) requires real KYC keys
(MEXC futures order permission needs KYC; unbound keys expire 90d).

## 9. Strengths / weaknesses
**Strengths:** fresh, correct MEXC auth (not cargo-culted); richest execution rigor
(`validate_order` + deterministic `RepairOrder`, no LLM fallback); ShadowLedger self-correcting
per-pair win-rate; Web UI as primary surface; explicit dead-man switch for an exchange with no
native `scheduleCancel`.
**Weaknesses:** youngest repo — only unit-tested against fakes, never hit live MEXC; geo-block
means it *depends* on a Cloudflare Worker being up; not go-live, so real-fill/liq behaviour is
still theoretically unverified end-to-end.
