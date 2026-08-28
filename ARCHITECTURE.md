# Bitget-Listener — Architecture & E2E Operations Doc

> Verified live 2026-08-28 on Sera. Branch `main` @ `843ff69`, systemd `active`, LIVE mode,
> HIGH risk profile, real `USDT-FUTURES` balance ~$8.57.

## 1. High-level architecture

```
                         Telegram @fattyfatclub (signal channel)
                                      │  (Telethon user session, live + catch-up)
                                      ▼
        ┌─────────────────────────────────────────────────────────┐
        │  TelethonSource (src/sources/telegram.py)                │
        │   • long-polls channel; enqueues RawSignal              │
        │   • startup catch-up (management-only, no stale ENTRY)  │
        │   • durable intake → bc_signal_receipts (RECEIVED)      │
        └───────────────────────────┬─────────────────────────────┘
                                     │ queue
                                     ▼
        ┌─────────────────────────────────────────────────────────┐
        │  Orchestrator._loop (src/services/orchestrator.py)       │
        │   handle(rs):                                            │
        │    1. rate-limit breaker open?  → skip                   │
        │    2. guard(): deadman / whitelist / concurrent /        │
        │              daily-loss / tradability                    │
        │    3. parser (SymbolMap) → side/entry/sl/tp              │
        │    4. risk.sizer.size_for_signal (HIGH-risk logic)       │
        │    5. OrderService.open_from_signal                     │
        │    6. BitgetAdapter.place_order (signed V2 REST)         │
        │    7. deterministic repair (1 retry) → PAPER gate       │
        │    8. _after_open → ledger.record_open + notify         │
        └───────┬───────────────────────────┬──────────────────────┘
                │                           │
   ENTRY orders │                           │ management signals
                ▼                           ▼
        ┌──────────────┐          ┌────────────────────────────────┐
        │ BitgetAdapter│          │ handle_management():            │
        │ (REST+WS)    │          │  sl_to_entry / tp_modify /      │
        │ src/exchanges│          │  partial / full_close / close   │
        │ /bitget.py   │          └────────────────────────────────┘
        └──────┬───────┘
               │ fills / positions / account pushes
               ▼
        ┌──────────────────────────────────────────────────────────┐
        │ BitgetWS (src/streaming/bitget_ws.py)                     │
        │   public (ticker/liveness sentinel BTCUSDT + open symbols)│
        │   private (orders/positions/account)                      │
        │   → on_fill / on_position / on_tick                       │
        │   → PositionManager.sync + _protection_sampler (30s)      │
        │   → bot-side SL/TP monitor (only real protection)         │
        └──────────────────────────────────────────────────────────┘

  Durable state (Postgres fatty_config, role app, bot='bitget-listener'):
    bc_signal_receipts  – every inbound message (durable intake + trace_id)
    bc_trace_events     – pipeline stages (RECEIVED→PARSED→DECISION→…→TERMINAL)
    bc_trades           – executed trades (fee-aware, BUG A/B/C enforced)
    bc_positions        – open position cache (exchange-synced)
    bc_armed_stops      – persisted bot-side SL/TP (survives restart)
    bc_events           – activity log (open/close/management)
    bc_equity           – wallet-equity samples (dashboard chart)
    fatty_config.params – paper flag, risk_profile, sizing params
```

## 2. Data flow: a signal, end to end

1. **Intake.** Telethon receives a `@fattyfatclub` message. `bc_signal_receipts` records it
   durably (raw text, message_id, channel, source ts, receive ts, payload hash, trace_id).
   Stage `RECEIVED` → `bc_trace_events`.
2. **Parse.** `parser.parse_signals()` extracts one `RawSignal` per symbol: side (LONG/SHORT),
   entry, SL, TP, action (open/partial/sl_to_entry/close). Stage `PARSER`/`PARSED`/`VISION`.
   Chatter with no symbol → `skipping signal: no symbol parsed`.
3. **Decision.** `Orchestrator.guard()` checks: dead-man switch alive, symbol on whitelist/tradable,
   concurrent < `max_concurrent`, daily-loss rail not breached, `TradabilityChecker` (live spec+balance).
   `DECISION ACCEPTED` or rejected with a named reason (e.g. "SL is mandatory — refusing entry",
   "min notional 90.0 exceeds max_position_usd", "max concurrent positions reached").
4. **Size.** `RiskLimits.size_for_signal()` (src/services/risk.py). In HIGH-risk mode the floor is
   `high_risk_min_margin_usd (3.0) × leverage (≥30)` = **$90 minimum notional** for ordinary coins;
   large coins (BTC/ETH/SOL/BNB/LTC) use their venue max leverage. `risk_per_trade_usd` and
   `max_position_usd` from `fatty_config.params` cap the notional.
5. **Order.** `OrderService.open_from_signal()` → `BitgetAdapter.place_order()` (signed V2 REST,
   `P_PLACE`). Rate-limit bucket `order` (1/s) delays, not drops. On repairable code (40762/40774/…)
   it re-rounds and resubmits ONCE. PAPER gate: if `paper=true`, no real order.
6. **Open bookkeeping.** `_after_open()` → `ledger.record_open()` (BUG A: real fee; BUG B: real
   liq price; BUG C: never cancel protective). Notifier pushes `format_open`.
7. **Live management (WS).** BitgetWS pushes fills/positions. `PositionManager.sync` folds real
   protection (venue `stopLoss`/`takeProfit` or pending plan orders). `_protection_sampler` (30s)
   refreshes real SL/TP because the private WS carries none.
8. **Management signals.** `$WLD tp1 booked` → `handle_management` → `partial` close of fraction of
   REAL exchange size. `sl to entry` / `tp modify` → re-arm bot-side monitor (and best-effort venue
   attach, which silently drops on this account). `NO_POSITION` → silent correct no-op.
9. **Close.** `OrderService.close_position()` carries `position.margin_mode` (fix for the old
   margin-mode mismatch). `round_qty(reduce_only=True)` skips min-notional inflation (fix for 40804).
   `_book_close()` → `ledger.record_close()` with `exit_reason` (full/partial/tp1_booked/sl_hit/…).
   Realtime push `format_tp_hit`/`format_sl_hit`/`format_manual_tp`/`format_liquidation`.
10. **Dead-man switch.** If no WS tick within `DEADMAN_TTL_S` (60s), trading halts (`deadman_halted`).
11. **Go-live gate.** `GoLiveGate` (src/notifier/golive.py): `mint_token()` requires conditions
    (a) probe GREEN on real key, (b) expectancy>0, (c) tests green, (d1) real fee seen, (d2) real liq
    price seen. `redeem(token)` writes `paper=false` to `fatty_config`. The running bot mints via
    `POST /api/mint_go_live` (DASH_PASSWORD-gated), not the CLI.

## 3. State, persistence & the ledger

- **Authoritative ledger = Postgres `bc_*`**, NOT the orphaned SQLite `bitget_listener.db`
  (deleted 2026-08-23). Reading SQLite would be wrong.
- `paper` is persisted in `fatty_config` and survives restart — a restart cannot silently flip mode.
- `bc_armed_stops` is the ONLY protection that reliably works (venue `place-tpsl-order` WRITE path
  silently drops plan orders on this account). It is persisted so a restart re-arms the monitor.
- Trace spine = `bc_signal_receipts` (intake) + `bc_trace_events` (stages). A failed entry should log
  a `TERMINAL`/`ORDER_FAILED` stage — **known gap**: the WLD 16056 failure logged the exception but
  did not record the terminal stage, so the trace shows only up to `DECISION ACCEPTED`.

## 4. Safety invariants (never regress)

- **BUG A (fees):** real fill fee recorded (Bitget reports negative → normalized positive). Never 0.
- **BUG B (liquidation):** `liquidationPrice` kept real; `None` if absent, never coerced to `0.00`.
- **BUG C (protective orders):** `cancel_all` never cancels `reduceOnly`/plan orders.
- **Rate-limit gate:** token bucket per category; 5 consecutive 429s → circuit breaker PARKS the bot
  (`rate_limit_breaker_open`). Operator clears with `rate_limit.reset()`.
- **Daily-loss kill-switch:** reads LIVE equity (`get_balance_usdt()`), not `EQUITY_USD` (which is a
  floor fallback). On this account the real balance ~$0.08 makes the rail effectively tight.

## 5. Runtime surface

- **Process:** `/opt/bots/bitget-listener/.venv/bin/python src/main.py --web`, User root,
  `Restart=on-failure`, `PYTHONPATH=/opt/bots/bitget-listener:/opt/bots`.
- **Dashboard:** FastAPI on `:8092`, nginx → `bitget.warga-digital.com` (Cloudflare orange-cloud).
- **Read-only APIs:** `/api/health` (`ok`, `mode`, `deadman_halted`), `/api/state` (full state JSON).
- **Operator Telegram commands** (prefix `bitget_`, e.g. `/bitget_status`, `/bitget_positions`,
  `/bitget_pnl`, `/bitget_close`, `/bitget_paper`, `/bitget_live`). State-changing ones require
  `TG_OPERATOR_ID` and `DASH_PASSWORD` where noted.
- **Env facts:** `BITGET_PRODUCT_TYPE=USDT-FUTURES` (LIVE/real; `SUSDT-FUTURES` is the sandbox),
  `PAPER=0` in unit but boot mode from DB, `EQUITY_USD=0.08` (floor fallback only),
  `DEADMAN_TTL_S=60`, `SYMBOL_WHITELIST=` (empty = all tradable), `ARM_SL_TP=ORDIUSDT:4.045:5.18`.

## 6. Known open issues (honest)

1. **HIGH-risk live on ~$8.57** → every entry rejected (`40762` / "min notional 90.0 exceeds
   max_position_usd"). Trading is effectively blocked until funded or profile changed.
2. **Venue `place-tpsl-order` WRITE path unreliable** — accepts `holdSide=buy` but silently drops the
   plan. Bot-side `bc_armed_stops` monitor is the dependable fallback.
3. **Trace stage gap** — handler exception not always recorded as `TERMINAL`/`ORDER_FAILED`.
4. **2 pytest failures** (environmental): credential-refusal tests overridden by live `.env` creds.
5. **Stale `bc_armed_stops` rows** for closed symbols would re-arm a monitor on restart — reconcile
   vs open positions before restarting over a flat account.

## 7. Repo layout (key files)

```
src/main.py                 entrypoint, build_runtime, load_env
src/config/db.py            BotConfig, PostgresConfigStore (params defaults)
src/domain/model.py         OrderRequest, Position, margin_mode
src/exchanges/bitget.py      BitgetAdapter (signing, place_order, get_spec, round_qty)
src/exchanges/bitget_auth.py request signing
src/exchanges/rate_limit.py  token-bucket + circuit breaker
src/sources/telegram.py     TelethonSource (catch-up, management-only)
src/sources/parser.py        SignalMap / parse_signals (R-dialect)
src/services/orchestrator.py  pipeline, guard, _loop, _periodic_report, _protection_sampler
src/services/order.py        OrderService (open/close, margin_mode, round_qty)
src/services/position.py     PositionManager + ArmedStops facade
src/services/risk.py         RiskLimits.size_for_signal (HIGH-risk floor)
src/services/tradability.py  TradabilityChecker (live spec+balance)
src/services/deadman.py      DeadManSwitch
src/services/repair.py       deterministic order repair
src/notifier/telegram.py     TelegramNotifier (send, format_* cards)
src/notifier/bot_poller.py    command router (bitget_* commands)
src/notifier/fmt.py          card formatting (no trading logic)
src/notifier/golive.py       GoLiveGate
src/store/pg_ledger.py        PostgresLedger (fee-aware)
src/streaming/bitget_ws.py    BitgetWS (public+private)
bot_common/                  shared package (repos, db, ensure_schema) — SEPARATE, copy on migrate
```
