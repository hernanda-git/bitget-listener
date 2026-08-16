# 06 — bitget-listener: design & implementation blueprint (USDⓈ-M Futures)

Goal: a 5th sibling that executes `@fattyfatclub` Telegram signals on **Bitget USDⓈ-M
Futures** (USDT-settled perpetuals), with the *best* engineering from the four existing repos.

> ⚠ **Provenance:** Bitget API specifics in this doc + `07-BITGET-IMPLEMENTATION-NOTES.md` are
> from established knowledge of the Bitget **V2** API. Web tools were unavailable during this
> analysis, so every Bitget endpoint/payload is marked **UNVERIFIED** and MUST be confirmed with
> a `bitget_api_probe.py` (spec in §9) before any order code ships — exactly like
> `mexc_api_probe.py` did for MEXC.

## 1. Why Bitget, and what to copy from whom
- **Base skeleton = Bybit repo.** Its SOLID+DI `ExchangeAdapter` port, frozen domain types,
  fakes-for-everything test discipline, and centralized signing/rounding are the cleanest.
- **Execution rigor = MEXC repo.** Adopt `validate_order` + deterministic `RepairOrder`
  (no LLM fallback) and idempotency dedup from day 1.
- **Config/runtime = Hyperliquid repo.** Postgres `fatty_config` multi-bot row with persisted
  `paper` flag (`/toggle`), reachable from the VPS over TCP.
- **Observability = MEXC repo.** FastAPI Web UI as primary surface + WebSocket event mirror +
  `ShadowLedger` per-pair win-rate.
- **Management commands = Binance repo.** `tp1 booked`, `sl to entry`, `full`, stale-LIMIT
  auto-cancel.
- **Safety = all of them.** BUG A/B/C fixes (real fee, real liq price, never auto-cancel
  reduce-only), paper-by-default go-live gate, dead-man's switch.

## 2. Target architecture (mirror bybit's module map)
```
bitget-listener/
├── main.py                      # composition root; run modes (--check-tg, --live-dry,
│                                #   --dashboard, --go-live, --all-bots, --status, ...)
├── src/
│   ├── domain/                 # frozen: Instrument, Signal, Fill, Position, OrderRequest
│   │                            #   Fill.fee REAL; Position.liq_price REAL (BUG A/B)
│   ├── interfaces/ports.py      # SignalSource, ExchangeAdapter, MarketStream, Notifier,
│   │                            #   RiskPolicy, PegSource, AttachmentFetcher
│   ├── exchanges/
│   │   ├── bitget.py            # BitgetAdapter — V2 REST (UNIFIED/MIX). reads BITGET_API_KEY/SECRET/PASSPHRASE
│   │   ├── bitget_auth.py       # BitgetAuth — V2 HMAC-SHA256 over timestamp+method+path+body
│   │   └── fake.py              # FakeExchangeAdapter (records fills w/ non-zero fees)
│   ├── streaming/
│   │   ├── bitget_ws.py         # BitgetWS — mix private (orders/positions) + public (tickers)
│   │   └── fake.py              # FakeMarketStream
│   ├── sources/
│   │   ├── parser.py            # signal parser + SymbolMap + to_ws_symbol (REST BTCUSDT ⇄ WS BTCUSDT)
│   │   ├── telegram.py          # TelethonSource + FakeSignalSource
│   │   └── llm_signal.py        # optional LLM escalation (FastPath → LLM)
│   ├── services/
│   │   ├── normalizer.py        # SignalNormalizer + StablecoinPegGuard
│   │   ├── validator.py         # Gate1 (pre-LLM) + Gate2 (post-LLM) + FastPath
│   │   ├── risk.py              # FixedRiskSizer — fee-aware (net = gross - fees)
│   │   ├── order.py             # OrderService — entry + mgmt transforms; never cancel reduce_only
│   │   ├── position.py          # PositionManager — real liq price (BUG B), fee-aware close
│   │   ├── repair.py             # IdempotencyGuard + RepairOrder (deterministic, no LLM) [from MEXC]
│   │   └── deadman.py           # watchdog → cancel-all+flatten on heartbeat loss
│   ├── notifier/bot.py          # TelegramNotifier + GoLiveGate (2-step)
│   ├── web/app.py               # FastAPI Web UI (PRIMARY) + REST + /ws/events [from MEXC]
│   ├── eval/shadow_ledger.py    # per-pair win-rate; flag/auto-downsize [from MEXC]
│   ├── config/db.py             # ConfigStore — psycopg; reuses fatty_config; env-var NAMES only
│   └── orchestrator.py          # pipeline coordinator; enforces paper gate BEFORE acting
├── scripts/
│   ├── bitget_api_probe.py      # VERIFY V2 reality before any order code (§9)
│   ├── configure_bot.py         # create/update fatty_config row
│   ├── replay_real.py / replay_mgmt.py
│   └── run_bots.sh
├── tests/                       # fakes-only, zero network; target ≥81
├── docs/  CHANGELOG.md  AGENTS.md  SECURITY.md
└── deploy.sh  Dockerfile  systemd/bitget-listener.service
```

## 3. Bitget domain specifics to handle up front
- **Product family = `UMCBL`** (USDT-settled perpetual / "mix" contract). Bitget's V2 splits
  endpoints by product: `SPBL` (spot), `UMCBL` (USDT-M futures), `DMCBL` (coin-M), `SMCBL`
  (USDC-M). We only touch `UMCBL`.
- **Symbol format:** `BTCUSDT` (no separator) for both REST *and* WS on Bitget (simpler than
  MEXC's underscore split — verify in probe).
- **Settlement = USDT** (like Binance/Bybit/MEXC, unlike Hyperliquid's USDC), so no USDT→USDC
  normalizer needed — but keep the `StablecoinPegGuard` as a no-op safety net.
- **Passphrase:** Bitget V2 requires an API **passphrase** (`ACCESS-PASSPHRASE`) in addition to
  key+secret. Capture it in `.env` as `BITGET_PASSPHRASE` and treat it like a secret.

## 4. Safety gates (inherit CONCRETE-SPEC §3 numbers)
| Param | Default | Hard bound |
|-------|---------|-----------|
| `risk_per_trade_usd` | 5 | — |
| `max_position_usd` | 100 | — |
| `max_concurrent` | 2 | — |
| `max_daily_loss_pct` | 30 | immutable kill switch |
| `max_leverage` | 20 | `min(config, pair_max)` |
| `min_notional_usd` | 5 | exchange filter |
| `cooldown_s` | 30 | — |
| `whitelist_only` | true | — |

## 5. SL/TP strategy on Bitget
Bitget V2 supports **plan/trigger orders** and **tpsl orders** attached to a position. Two viable
approaches (decide after probe):
- **Attached TP/SL at entry** (`/api/v2/mix/order/place-plan` with `presetTakeProfitPrice` /
  `presetStopLossPrice`, or `autoTpSl` on the opening order). Cleanest; mirrors Bybit's atomic
  attach.
- **Separate plan orders** (`/api/v2/mix/order/place-plan`) referencing the position, `reduceOnly`.
  Mirrors MEXC's protective `/order/submit`.
- **Never auto-cancel reduce-only protective orders** (BUG C). Dead-man's switch uses
  Bitget's `cancel-all` (`/api/v2/mix/order/cancel-all`) + `setAutoCancel` (the Bitget-native
  equivalent of HL `scheduleCancel`) if available.

## 6. WebSocket plan (Bitget mix)
Bitget WS uses a **single signed connection** (`wss://ws.bitget.com/mix/v1/stream`) with a
`login` frame, then `subscribe` frames carrying `instType: "UMCBL"` and channel lists:
- **Public:** `tickers.BTCUSDT`, `market.btcusdt.tickers` (mark price), optionally
  `liquidation`.
- **Private (`pbl` auth):** `orders`, `positions`, `account` — pushed fills/positions/balance.
- **Heartbeat:** server sends `ping` → client replies `pong` (like MEXC). Reset dead-man TTL.
- Reconnect: jittered backoff (cap 30s), `on_ready`/`on_lost` callbacks (copy MexcWS shape).

## 7. Geo-block risk assessment (do BEFORE go-live)
Bitget historically **does not** geo-block Jakarta the way MEXC does, and is a normal CEX (so
not IP-ban-prone like Binance). Still: run `bitget_api_probe.py` from Sera's IP; if blocked,
reuse the **Cloudflare Worker proxy pattern** from `mexc-proxy` (route `/api/*` REST + `/stream`
WS to Bitget). Keep `BITGET_REST_PROXY` / `BITGET_WS_URL` env overrides.

## 8. Go-live gate (hardened)
`--go-live` mints a one-time token → user `/live <token>` flips `paper=0` ONLY if:
(a) `bitget_api_probe.py` GREEN on real key, (b) ShadowLedger expectancy > 0, (c) all P0–P3
tests green, (d) testnet fill shows non-zero `execFee` (BUG A) and real liq price (BUG B).

## 9. `bitget_api_probe.py` — mandatory pre-order verification
Mirror `mexc_api_probe.py`. Non-destructive. Checks (each GREEN/RED):
1. **REST base reachable** from Sera IP (no geo-block) — `/api/v2/mix/market/contracts` (public).
2. **Auth works** — signed GET to a private endpoint (`/api/v2/mix/account/account` or
   `/api/v2/mix/position/open-positions`) returns 200, not 401/403.
3. **Instrument spec shape** — `/api/v2/mix/market/contracts?symbol=BTCUSDT` returns
   `minSize`/`maxSize`/`sizeIncrement`/`priceIncrement`/`maxLeverage` (confirm exact field
   names — UNVERIFIED).
4. **Fee shape** — a testnet fill (or `/api/v2/mix/account/account`) exposes `execFee`/`fee`.
5. **WS login + subscribe** — connect `wss://ws.bitget.com/mix/v1/stream`, `login`, subscribe
   `tickers.BTCUSDT`, receive a `pong`/tick. Report clean RED (no crash) if host rejects.
6. **1000× / contract-multiplier coverage** — confirm whether Bitget lists `1000PEPEUSDT`-style
   contracts and how size is expressed (base vs contracts).

> The probe is the single most important artifact: it converts "I think Bitget V2 works like
> this" into "verified from a real response." Write it FIRST, before `bitget.py`.

## 10. Phased build (TDD, bybit-style)
P0 scaffold → P1 domain/ports/fakes → P2 signal ingestion → P3 Bitget REST (testnet) →
P4 Bitget WS (testnet) → P5 order+position+real-time stops → P6 fee-aware ledger →
P7 reflexivity+ShadowLedger → P8 learning loop → P9 notifier+go-live → P10 replay+gate.
Each phase independently testable with fakes; `pytest` green required before `deploy.sh`.
