# 04 — Hyperliquid (on-chain USDC perps): REST + WebSocket (as implemented)

Source: `hyperliquid-fatty-trader/src/exchanges/hyperliquid.py` (491 lines) +
`streaming/hyperliquid_ws.py` (165 lines) + `docs/hyperliquid-api.md`.

> **On-chain venue.** Trading is on the Hyperliquid L1 (HyperCore); order entry + data go
> through the API backed by the chain. **Settlement is always USDC.** No API-key IP-ban problem
> (unlike Binance) — reachable from any IP including Tencent/cloud ranges. This is why the bot
> doesn't need the egress-proxy gymnastics the Binance/MEXC siblings do.

## 1. Endpoints
| Purpose | URL (mainnet) | Testnet |
|---------|---------------|---------|
| Info (read) | `https://api.hyperliquid.xyz/info` | `https://api.hyperliquid-testnet.xyz/info` |
| Exchange (write) | `https://api.hyperliquid.xyz/exchange` | `https://api.hyperliquid-testnet.xyz/exchange` |
| WebSocket | `wss://api.hyperliquid.xyz/ws` | `wss://api.hyperliquid-testnet.xyz/ws` |

## 2. Info endpoint (`_info`, JSON-RPC-style POST) — `type` values used
| `type` | Used by | Returns |
|--------|---------|---------|
| `meta` | `_asset_index` | `universe[].name` (e.g. `BTC`, `ETH`) → maps symbol → **integer asset index**. |
| `allMids` | `get_mark_price` | Latest mid price per coin (USDC). |
| `clearinghouseState` | `get_positions` | Positions, marginSummary, withdrawable for an address. |
| `frontendOpenOrders` | `get_open_orders` | Open orders for an address. |
| `userFills` | `get_fills` | Recent fills. |

## 3. Exchange endpoint (`_post_exchange`) — signed actions
Order entry uses the **generic `Exchange`/`SignedAction` scheme** (NOT the old JSON-string +
concatenated-hex scheme that produced `422 Failed to deserialize`):
- Action serialized as the raw **object**; signature computed over the **msgpack-encoded action**
  (SDK scheme: chainId 1337, `Agent` EIP-712 typed message). Wire shape:
  `signature = {"r","s","v"}`.
- Payload: `{"action": <object>, "nonce": <ms>, "signature": {...}, "vaultAddress": null, "expiresAfter": null}`.

### Actions used
| Action | Code path | Notes |
|--------|----------|-------|
| `order` | `place_order`, `open_market`, `close_position_market` | Orders array. See §5 for order shape. |
| `cancel` | `cancel_order` | `{"a": symbol, "o": client_order_id}`. |
| `updateLeverage` | `set_leverage`, `open_market` | `{"asset", "isCross", "leverage"}`. HL requires leverage set before opening. |
| `scheduleCancel` | `set_scheduled_cancel` | Dead-man's switch: auto-cancel all open orders if not refreshed in `hours`. HL takes **seconds** in `cancelsAt`. |
| `usdClassTransfer` | `usd_class_transfer` | Move USDC internally Spot ↔ Perp. **Own EIP-712 domain** (`HyperliquidSignTransaction` / `HyperliquidTransaction:UsdClassTransfer`); signing with the order signer yields 422. |

## 4. Auth
EVM wallet signer (private key). Two distinct signing schemes (both implemented):
1. `_sign_l1(action, nonce)` — for `order`/`cancel`/leverage/scheduleCancel. msgpack(action) +
   nonce(8B) + vault flag + optional expiresAfter → keccak → `Agent` typed message, chainId 1337.
2. `_sign_usd_class_transfer(action)` — dedicated `HyperliquidSignTransaction` domain, chainId
   `0x66eee`.
- `_DOMAIN_MAINNET = {name:"Exchange", version:"1", chainId:42161}`,
  `_DOMAIN_TESTNET = {name:"Exchange", version:"1", chainId:998}`.
- Sanity check: derived address from `private_key` must equal configured `account_address`.

## 5. Order shape (`_hl_order`)
HL serializes the asset as its **integer index** (e.g. BTC mainnet == 0), NOT the symbol string:
```python
{
  "a": <asset_index>,
  "b": "b" if BUY else "s",          # side as string "b"/"s"
  "p": "<price>" or "0" (market),
  "s": "<size in coin units>",       # e.g. BTC size, formatted .8g
  "r": <reduce_only bool>,
  "c": <client_order_id>,            # idempotency
  "t": { ... }                       # order type
}
```
- **Limit:** `t = {"limit": {"tif": "Gtc"}}` (or `"Alo"` post-only/maker for rebate, or `"Ioc"`).
- **Market:** `t = {"limit": {"tif": "Ioc"}}`, price `"0"`.
- **Trigger (SL/TP):** `t = {"trigger": {"triggerPx": "...", "isMarket": true, "tpsl": "sl"|"tp"}}`.

Amounts: prices in USDC; size `s` in **base coin units** (the `OrderService` already did the
USD→coin conversion using the live mid from `allMids`/`bbo`). The `UsdNotional` domain type keeps
USD separate from the settlement asset — this is the whole point of the architecture.

## 6. WebSocket (`hyperliquid_ws.py`)
One connection carries **both** market data and user events — no listenKey/REST-refresh dance.
### Public (`allMids`)
- `subscribe` → `{"method":"subscribe","subscription":{"type":"allMids"}}`.
- Pushes `{"channel":"allMids","data":{SYM: price, ...}}`; `mark_price(sym)` serves the cache.
- Covers every symbol's mid in one stream.

### Private (`webData2`)
- `subscribe` → `{"method":"subscribe","subscription":{"type":"webData2","user":"0xADDRESS"}}`.
- Pushes `clearinghouseState` (positions) + `fills` in real time.
- Parsed:
  - `fills[]` → `on_fill` (`px`, `sz`, `fee`, `time`, side `B`/`S`).
  - `clearinghouseState.assetPositions[]` → `on_position` (`coin`, `szi` signed size, `entryPx`,
    `leverage.value`, `marginUsed`, `unrealizedPnl`, `liquidationPx`).

Reconnect: jittered exponential backoff (cap 30s) on both streams. On reconnect the orchestrator
refetches `clearinghouseState` + `frontendOpenOrders` to reconcile local state.

## 7. Settlement note
All amounts (entry, sl, tp, margin, PnL) are in **USDC**; coin size `s` is base-asset units.
The signal source quotes USDT (e.g. `@fattyfatclub` calls) → `SignalNormalizer` converts USDT→USDC
via a `StablecoinPegGuard` (rejects if `|peg-1| > threshold`). The USDT/USDC mismatch is handled
**once**, in `SignalNormalizer`, not scattered as `* 1.0` hacks.

## 8. Dead-man's switch
Two layers:
1. HL-native `scheduleCancel` (armed via `set_scheduled_cancel`) — auto-cancels stale orders.
2. Process-level watchdog on WS heartbeat loss.

## 9. Strengths / weaknesses
**Strengths:** WS-first single connection (near-zero REST weight); no IP-ban surface; on-chain
transparency; DB-backed multi-bot config with persisted `paper` flag (`/toggle`); cleanest
settlement model (USDC-native, `UsdNotional`). Most advanced infra of the 4.
**Weaknesses:** EIP-712 signing is fiddly (the `422` history shows it); asset-index lookup must
be cached carefully (latent bug class the bybit sibling explicitly guards against); on-chain
venue means fills depend on HL sequencer liveness; smallest documentation of REST error codes
(no `-1003`-style taxonomy because there isn't one).
