# 01 — Binance USDⓈ-M Futures: REST + WebSocket (as implemented)

Source: `learnernoearner-listener/src/exchange/binance.py` (1683 lines) + `ws_stream.py` (282 lines).

> This bot targets **USDⓈ-M Futures** (`fapi.binance.com`). Spot support is present in the
> adapter but the trade path uses Futures. Testnet uses `testnet.binancefuture.com`.

## 1. Base URLs
| Env | REST base | WS base |
|-----|-----------|---------|
| Mainnet futures | `https://fapi.binance.com` | `wss://fstream.binance.com` |
| Testnet futures | `https://testnet.binancefuture.com` | `wss://fstream.binance.com` (same host; testnet uses `stream?streams=...`) |
| Mainnet spot | `https://api.binance.com` | `wss://stream.binance.com:9443` |
| Testnet spot | `https://testnet.binance.vision` | — |

Constructor flags: `testnet`, `futures`, `leverage`, `recv_window` (≤60000, default 5000),
`egress_proxy` (HTTP proxy that tunnels ALL traffic incl. WS through a clean IP to dodge
datacenter IP-bans), and `proxy` (signed gateway relay — when enabled the local key is NOT
required; requests are forwarded to a relay that swaps its own key, still signed with the
local key so behaviour is identical).

## 2. Auth
HMAC-SHA256 over the **query string** (not the JSON body) for signed requests:
```python
params["timestamp"] = int(time.time()*1000)
params["recvWindow"] = self.recv_window
params["signature"] = hmac_sha256(secret, urlencode(params))
headers = {"X-MBX-APIKEY": api_key}
```
For the gateway relay, the request is instead signed with `_gw_sign(secret, method, path, params, body)`
where `canon = f"{METHOD}\n{path}\n{query}\n{body}"` and a `X-Gateway-Sig` / `X-Gateway-Ts` header is added.

## 3. REST endpoints used (verified from code)
### Public
| Endpoint | Used by | Notes |
|----------|---------|-------|
| `GET /fapi/v1/exchangeInfo?symbol=` | `SymbolRegistry` | Symbol filters (stepSize, minQty, tickSize, contractType). Refreshed every 15 min. |
| `GET /fapi/v1/ticker/24hr` | balance/price | Per-symbol or all; weight 50 when no symbol → batched mark cache. |
| `GET /fapi/v1/klines` | `get_klines_close` | OHLC for TA context. |
| `GET /fapi/v1/ticker/price` | spot price | Spot-only path. |

### Signed (futures)
| Method & path | Function | Notes |
|---------------|----------|-------|
| `POST /fapi/v1/order` | `market_buy/sell`, `limit_buy/sell`, `stop_loss`, `take_profit`, `market_close` | Main order entry. |
| `GET /fapi/v2/account` | `get_balance`, position/margin | Wallet + positions snapshot. |
| `GET /fapi/v2/positionRisk?symbol=` | `get_positions` | Position risk incl. liquidation price. |
| `POST /fapi/v1/leverage` | `set_leverage` | Per-symbol leverage (called before each trade). |
| `POST /fapi/v1/marginType` | `set_margin_mode` | ISOLATED / CROSSED. |
| `GET /fapi/v1/openOrders` | `get_open_orders` | Resting orders. |
| `DELETE /fapi/v1/order` | `cancel_order` | By `orderId` or `origClientOrderId`. |
| `DELETE /fapi/v1/openOrders?symbol=` | `cancel_all_orders` | Mass cancel. |
| `GET /fapi/v1/order` | `get_order` | Single order status. |

### Algo Order API (`-4120` pairs like XPLUSDT)
| Method & path | Function | Notes |
|---------------|----------|-------|
| `POST /fapi/v1/algoOrder` | conditional STOP/TP for blocked pairs | **Opt-in** via env `BINANCE_ALGO_ORDERS=1`. Standard `/order` returns `-4120` (conditional not supported) for 1000× contracts. |
| `GET /fapi/v1/algoOrder` | algo order query | |
| `DELETE /fapi/v1/algo/order` | algo cancel | |

## 4. Order type mapping
```python
FUTURES_ORDER_TYPES = {
  "MARKET": "MARKET",
  "LIMIT": "LIMIT",
  "STOP_LOSS": "STOP_MARKET",          # spot -> futures STOP_MARKET
  "STOP_LOSS_LIMIT": "STOP",           # futures "STOP" = conditional STOP-LIMIT
  "TAKE_PROFIT_LIMIT": "TAKE_PROFIT",  # futures "TAKE_PROFIT" = conditional TP-LIMIT
  "TAKE_PROFIT": "TAKE_PROFIT_MARKET",
}
```
Key gotchas encoded in code:
- **`-4120` (conditional order not supported):** for 1000× contracts (e.g. `1000BONKUSDT`), STOP/TAKE_PROFIT conditional orders are blocked. Bot falls back to **price monitoring** via position manager (mark price polled every 10s) for SL, and a **resting Basic-tab LIMIT** for TP.
- **`-2022` (reduceOnly mismatch):** fixed (v81) by passing an explicit `reduce=` flag on LIMIT/STOP/TAKE_PROFIT orders.
- **`-4164` (order notional too small):** Min $5 on Binance Futures → bot bumps quantity to meet min notional.
- **`-1111` (precision):** quantity/price rounded to `stepSize`/`tickSize` from `exchangeInfo`.

## 5. Symbol resolution (1000× contracts)
`_resolve_futures_symbol()` uses a dynamic `SymbolRegistry` first (live `exchangeInfo`), then
a static `SYMBOL_MAP` for known 1000× contracts (`BONKUSDT→1000BONKUSDT`, etc.). Critical
rule baked into comments: the `1000` prefix is a **contract multiplier for PnL only** — price
and quantity stay in base-asset terms (BONK = 0.0044), and STOP orders are blocked for these
pairs.

## 6. Rate-limit circuit breaker (unique to this repo)
Binance IP-bans (`-1003` / HTTP `418`) this bot repeatedly because of Tencent ap-jakarta
datacenter range. The adapter tracks a `_ban_until_ms` parsed from the error string
(`"banned until <ts>"`) and **refuses further REST calls** until it clears — avoiding the
retry storm that extends the ban. A marker is written to `/data/binance_ban_status.txt`.
Soft backoff (`_rate_limited_until_ms`) for `429`/`-1006`/`-1007`.

## 7. WebSocket (`ws_stream.py` — `MarketDataStream`)
Two logical streams over the same WS host:
1. **Mark-price stream (PUBLIC, zero REST weight):** `wss://fstream.binance.com/stream?streams={sym}@markPrice/...`
   - Cache `mark_cache[sym] = (price, ts_ms)`, TTL 10s. `get_mark_price()` reads from cache.
2. **User-data stream (needs listenKey):** `wss://fstream.binance.com/ws/{listenKey}`
   - `listenKey` obtained via `POST /fapi/v1/listenKey`, refreshed every 55 min
     (`PUT /fapi/v1/listenKey`). If REST is IP-banned, the bot **backs off** instead of
     hammering the banned endpoint (increasing backoff up to 30 min).
   - Events handled:
     - `ACCOUNT_UPDATE` → updates `positions[sym]` (removes when `pa==0`).
     - `executionReport` → tracks `open_orders[orderId]` (NEW adds, terminal states remove).
   - `on_account_update` callback for the position manager.

Both loops reconnect on drop (`async for` + outer `while self._running`). When WS is off/stale,
getters transparently fall back to REST (still guard-railed by the circuit breaker).

## 8. Persistent failure-mode this design solves
Old design: position monitor polled REST every tick → tripped IP-ban → couldn't trade.
New design: **public mark prices + user-data pushes** mean SL monitoring carries *no* REST
weight at all, so the bot keeps trading even while REST is banned. This is the same
philosophy Hyperliquid uses natively (one multiplexed WS).

## 9. Strengths / weaknesses (see 05 for cross-repo scoring)
**Strengths:** most battle-tested; elegant ban-immune WS design; rich management commands.
**Weaknesses:** known bugs the *bybit* sibling was built to close — fees not always recorded,
liquidation sometimes booked at `0.00`, reduce-only protective orders historically dropped.
Largest single file (1683 lines) — less SOLID than bybit.
