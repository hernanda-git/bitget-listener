# 02 — Bybit V5 (USDT Perpetuals, linear): REST + WebSocket (as implemented)

Source: `bybit-listener/src/exchanges/bybit.py` (404 lines) + `streaming/bybit_ws.py` (241 lines).

> Category = `linear` (USDT perpetuals). Settlement = USDT. The bot is built testnet-first
> (`api-testnet.bybit.com`); mainnet is never contacted when `testnet=True` (hard safety rule).

## 1. Base URLs
| Env | REST | Public WS | Private WS |
|-----|------|-----------|------------|
| Testnet | `https://api-testnet.bybit.com` | `wss://stream-testnet.bybit.com/v5/public/linear` | `wss://stream-testnet.bybit.com/v5/private` |
| Mainnet | `https://api.bybit.com` | `wss://stream.bybit.com/v5/public/linear` | `wss://stream.bybit.com/v5/private` |

Overridable via `rest_url` / `private_ws` / `public_ws` config (used by fakes in tests).

## 2. Auth (REST) — V5 HMAC-SHA256
```python
ts   = str(int(time.time()*1000))
if GET:  param_str = "&".join(f"{k}={v}" for k,v in sorted(params.items()))
else:    param_str = json.dumps(body, separators=(",",":"))   # compact, sorted keys NOT required
sign = hmac_sha256(api_secret, ts + api_key + recv_window + param_str)
headers = {
  "X-BAPI-API-KEY": api_key,
  "X-BAPI-TIMESTAMP": ts,
  "X-BAPI-RECV-WINDOW": recv_window,
  "X-BAPI-SIGN": sign,
  "Content-Type": "application/json",
}
```
- `recv_window` default 5000 ms.
- **Token bucket** rate limiter: `rate_limit_per_s` (default 10) — `_acquire()` sleeps to stay
  under it.
- **Retry/backoff** on retCode `10006` (rate limit), `10018`, `170151`: exponential backoff up
  to 8s, 5 attempts; non-retryable errors raised verbatim (`BybitRateLimitError` with retMsg +
  retExtInfo). Testnet is the arbiter — field drift is surfaced, not hacked around.

## 3. REST endpoints used (verified from code)
| Method & path | Function | Notes |
|---------------|----------|-------|
| `GET /v5/market/instruments-info` | `refresh_instruments` | `category=linear`, `limit=1000`. Caches per-symbol `lotSizeFilter`/`priceFilter` for rounding + tradable set. |
| `GET /v5/market/tickers?symbol=` | `ticker` | Last/mark/24h. |
| `GET /v5/market/time` | `server_time_ms` | Clock sync. |
| `POST /v5/order/create` | `place_order` | Central `_order_body` builds the request. |
| `POST /v5/order/cancel` | `cancel_order` | By `orderLinkId`. |
| `POST /v5/order/cancel-all` | `cancel_all` | By symbol or whole category. |
| `GET /v5/order/realtime?openOnly=1` | `get_open_orders` | Raw order list → `OrderRequest` reconstruction. |
| `GET /v5/position/list?settleCoin=USDT` | `get_positions` | `size==0` rows skipped. |
| `GET /v5/execution/list` | `get_fills` | **Fallback**; WS `execution` stream is primary. Returns `execFee` (BUG A fix). |
| `GET /v5/account/wallet-balance?accountType=UNIFIED` | `get_balance_usdt` | Sums `availableBalance` of USDT coin. |
| `POST /v5/position/set-leverage` | `set_leverage` | `buyLeverage`/`sellLeverage`. `110012` (already set) is ignored. |
| `POST /v5/position/set-trading-stop` | `set_trading_stop` | Attaches/modifies SL/TP (`stopLoss`/`takeProfit`, `slTriggerBy`). |

## 4. Order building (`_order_body`)
```python
body = {
  "category": "linear",
  "symbol": sym,
  "side": order.side,                       # BUY / SELL
  "orderType": order.order_type,           # Market / Limit / Stop / ...
  "qty": str(round_qty(sym, order.qty)),
  "timeInForce": order.time_in_force,      # GTC / IOC / FOK
  "positionIdx": order.position_idx or 0,  # 0 = one-way mode
}
if client_order_id: body["orderLinkId"] = ...
if price:            body["price"] = str(round_price(...))
if reduce_only:      body["reduceOnly"] = True
if trigger_price and STOP/StopLimit: body["triggerPrice"] = ...; body["triggerDirection"] = ...
# attached SL/TP at create time (atomic):
if sl is not None: body["stopLoss"] = str(round_price(...)); body["slTriggerBy"] = ...
if tp is not None: body["takeProfit"] = str(round_price(...)); body["tpTriggerBy"] = ...
if sl or tp: body["tpslMode"] = order.tpsl_mode
```
- **Rounding** is centralized and applied to *every* order path (the explicit guard against the
  Hyperliquid asset-index latent bug). `round_qty` snaps to `basePrecision` and clips to
  `[minOrderQty, maxOrderQty]`; `round_price` snaps to `tickPrecision` and clips to
  `[minPrice, maxPrice]`.

## 5. WebSocket (`bybit_ws.py` — `BybitWS`)
Two separate connections:
### Private (`/v5/private`) — auth then subscribe
- Auth op: `{"op":"auth","args":[api_key, expires, sign]}` where
  `sign = HMAC-SHA256(secret, "GET/realtime" + expires)`, `expires = now_ms + 5000`.
- Subscribe: `{"op":"subscribe","args":["order","position","wallet","execution"]}`.
- Dispatch:
  - `execution` → `on_fill` with **real `execFee`** (BUG A fix), `execPrice`, `execQty`,
    `orderLinkId`, `closedPnl`, `feeCurrency`.
  - `position` → `on_position`; `liqPrice` passed through so liquidation is booked at the **real
    price** (BUG B fix), never `0.00`.
  - `order` → `on_order`.
  - `wallet` → `on_wallet` (USDT `availableBalance`).
### Public (`/v5/public/linear`)
- Subscribe per symbol: `tickers.{S}`, `orderbook.50.{S}`, `publicTrade.{S}`, `liquidation.{S}`.
- `tickers.{S}` → mark price into `_marks[sym]`; `on_mark(sym, price)` callback.
- `mark_price(symbol)` port method serves the cache for the position manager.

Callbacks run off the event loop (supports both sync lambdas in tests and async handlers).
`last_heartbeat_s` drives the dead-man's switch (no WS message for `ws_heartbeat_timeout_s`
→ flatten + halt).

## 6. Safety philosophy (from AGENTS.md — non-negotiable)
- Never place a real order to test → use **TESTNET** for all live-API checks.
- `paper` (dry gate) persisted in Postgres, read before acting.
- Never cancel `reduce_only` protective orders during management (BUG C fix carried from design).
- Dead-man's switch flattens + halts on heartbeat loss.
- Secrets only in `.env`; never echo keys.

## 7. Strengths / weaknesses
**Strengths:** cleanest SOLID + DI; instrumentation/rounding centralized and unit-tested against
the algorithm; explicitly closes the 3 shared bugs (fees, liq price, reduce-only protects);
testnet-first so it can't touch mainnet by accident; 81 fakes-only tests = highest confidence.
**Weaknesses:** youngest of the *production-shaped* bots; not yet go-live; Bybit V5 nuance
(one-way vs hedge mode `positionIdx`, `tpslMode`) must be kept aligned with account settings.
