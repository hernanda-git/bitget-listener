# Bitget-Listener — FULL Documentation (Endpoints · Failures · Must-NOT · Future Dev)

> Verified live 2026-08-28 on Sera. `main` @ `e16311e`. LIVE mode, HIGH risk profile,
> real `USDT-FUTURES` balance ~$8.57. Authored for migration + future development.

---

## PART A — Working Bitget V2 REST endpoints

All base: `https://api.bitget.com`. Signed calls need `ACCESS-KEY` / `ACCESS-SIGN` /
`ACCESS-TIMESTAMP` / `ACCESS-PASSPHRASE` headers (HMAC-SHA256 over `timestamp+method+path+body`).
Source: `src/exchanges/bitget.py`.

| Constant | Path | Method | Category | Used by | Status |
|---|---|---|---|---|---|
| `P_CONTRACTS` | `/api/v2/mix/market/contracts` | GET | public | `get_spec` | ✅ works (public) |
| `P_TICKER` | `/api/v2/mix/market/ticker` | GET | public | `get_mark_price`, min-notional | ✅ works |
| `P_PLACE` | `/api/v2/mix/order/place-order` | POST | order | `place_order` (entry + close) | ✅ works (rejects on size/balance) |
| `P_TPSL` | `/api/v2/mix/order/place-tpsl-order` | POST | order | `place_protective`, `set_trading_stop` | ⚠️ ACCEPT-but-DROP (see Part B) |
| `P_CANCEL` | `/api/v2/mix/order/cancel-order` | POST | order | `cancel_order` | ✅ works |
| `P_OPEN_ORDERS` | `/api/v2/mix/order/orders-pending` | GET | signed | `get_open_orders` | ✅ works |
| `P_PLAN_ORDERS` | `/api/v2/mix/order/orders-plan-pending` | GET | signed | protection read | ✅ works (needs `planType` param) |
| `P_FILLS` | `/api/v2/mix/order/fills` | GET | signed | `get_fills` (BUG A fee) | ✅ works |
| `P_POSITIONS` | `/api/v2/mix/position/all-position` | GET | account | `get_positions` | ✅ works |
| `P_ACCOUNTS` | `/api/v2/mix/account/accounts` | GET | account | `get_balance_usdt` | ✅ works (returns `available` as STRING) |
| `P_SET_LEVERAGE` | `/api/v2/mix/account/set-leverage` | POST | account | `set_leverage` | ✅ works |
| `set-trading-stop | `/api/v2/mix/order/set-trade-stop` (alias path used by adapter) | POST | order | legacy stop attach | ⚠️ superseded by `P_TPSL` |

**Read endpoints that are READ-ONLY and safe:** `P_CONTRACTS`, `P_TICKER`, `P_OPEN_ORDERS`,
`P_PLAN_ORDERS`, `P_FILLS`, `P_POSITIONS`, `P_ACCOUNTS`. None of these place orders.

**Signed header gotcha:** `ACCESS-SIGN` is computed over the **request body** for POST and over
the query string for GET. A bare `BitgetAdapter(...)` constructed in a `python - <<EOF` heredoc
dies with `40009 sign signature error` even with correct creds — always use `build_runtime()`
(from `src.main`) or the proper signing path. Never hand-roll the adapter in a heredoc.

---

## PART B — Failures / not-working / not-correct

### B1. `place-tpsl-order` (P_TPSL) ACCEPTS but SILENTLY DROPS the plan  ⚠️ CRITICAL
- **Symptom:** call returns `200` + a fake `orderId`, but `orders-plan-pending` stays empty and the
  position `stopLoss`/`takeProfit` remain unset. The position is **NAKED**.
- **Root cause (proven):** `holdSide` must be `"buy"`/`"sell"` (the POSITION side), NOT `"long"`/`"short"`
  (those → `43011`). Even with the correct body, isolated-margin positions drop the plan. Crossed
  mode also drops it on this account.
- **Code hardening:** `set_trading_stop()` now VERIFIES via `get_protection()` and RAISES if the
  venue doesn't echo the level — so the bot escalates loudly instead of believing it's protected.
- **Practical rule:** do NOT fire many live `place-tpsl-order` calls hoping one sticks. Stop after
  ~10 probes (rate-limit / IP-ban risk). The dependable protection is the **bot-side `bc_armed_stops`
  WS monitor** (survives restart, persisted in Postgres). For an OPEN position, tell the user to set
  TP/SL on the Bitget website. For NEW entries, preset SL/TP at entry via
  `presetStopLossPrice`/`presetStopSurplusPrice` (also known to be unreliable on this account).

### B2. HIGH-risk live on tiny balance → every entry rejected  ⚠️ CURRENT BLOCKER
- **Codes:** `40762 The order amount exceeds the balance` (WLD, 09:13) and
  `min notional 90.0 exceeds max_position_usd` (INJ, 22:55).
- **Root cause:** `risk_profile=high` forces `min_notional = max(10, high_risk_min_margin_usd 3 × lev)`.
  With `high_risk_min_leverage=30` and `max_leverage` capped at 10, lev=10 → floor $30; large coins
  use venue max (BTC/ETH/SOL/BNB/LTC). The **real `USDT-FUTURES` balance is ~$8.57**, so even $30
  notional needs more margin than available, and `max_position_usd=10` is below the $90 floor the
  sizer raises. Every new entry is structurally impossible.
- **Decision needed (user left as-is):** fund the real account to ~$100+, or switch `risk_profile`
  back to `normal` and lower `min_notional_usd`/`max_position_usd` to fit $8.57, or accept that no
  entries place. This is a CONFIG decision, not a code bug.

### B3. `/close` margin_mode mismatch (FIXED 2026-08-20, still must-respect)
- Close `OrderRequest` MUST carry `position.margin_mode`. If omitted it inherits model default
  `isolated`, but the account is `crossed` → Bitget rejects, NO order placed, bot reported a close
  that never happened. Verified live. Keep `margin_mode` threaded through every close path.

### B4. `round_qty` min-notional inflation on close (FIXED 2026-08-21, code `1bc44f2`)
- `round_qty(spec, qty, price, reduce_only=False)` SKIPS the inflation loop when `reduce_only=True`.
  A close qty is derived from the position itself; inflating it caused `40804 The number of closed
  positions cannot exceed the number of positions held`. Never call `round_qty` with `reduce_only=False`
  on a close size.

### B5. Empty `SYMBOL_WHITELIST` starves the public WS (FIXED 2026-08-20, BUG #1)
- Empty subscribe frame = `{"op":"subscribe","args":[]}` → Bitget rejects `30002`, `last_msg_ts`
  goes stale, dead-man trips. Fix: `BitgetWS` subscribes the `BTCUSDT` liveness sentinel when symbols
  empty. Verify after any WS change: `journalctl | grep subscribed` shows
  `WS .../public subscribed ['BTCUSDT']` and `deadman_halted False`.

### B6. `/api/state` showed SL/TP as null (FIXED 2026-08-20, BUG #3)
- `get_protection()` now reads position-level `stopLoss`/`takeProfit` + live plan orders; a plan
  order wins when both exist. `PositionManager.sync` folds real protection in. A `null` in
  `/api/state` is now the TRUTH (naked), not a display bug.

### B7. Trace stage gap (OPEN, minor)
- WLD signal `16056` shows `RECEIVED→PARSED→DECISION(ACCEPTED)` but NO `TERMINAL`/`ORDER_FAILED`
  stage in `bc_trace_events`. The `handler_error` exception logs but never records the failure stage.
  A rejected entry therefore looks incomplete in the trace spine. Fix: record `ORDER_FAILED` in the
  `_loop` except handler (mirror `bc_trace_events` write used by `BOOKKEEPING`/`MANAGEMENT`).

### B8. Venue state can break a correct order (margin mode / posMode)
- `posMode` must be `one_way_mode` or Bitget hard-rejects every order with
  `40774 The order type for unilateral position must also be the unilateral position type`.
  `preflight_venue()` repairs `hedge_mode→one_way_mode` and `isolated→crossed` at startup, but ONLY
  when there are zero open positions. After every first fill, assert `liquidationPrice` is on the
  correct side of the stop. Querying attached SL/TP REQUIRES `planType` (`profit_loss`) or `400172`.

### B9. 2 pytest failures (environmental, not regressions)
- `test_telethon_source_refuses_to_start_without_credentials` and
  `test_notifier_without_credentials_does_not_send` assert credential-refusal behavior, but the live
  `.env` loads real creds so the refusal path is never taken. They fail ONLY because real creds are
  present; they pass on a clean CI without `.env`. Do not "fix" by loosening — run CI without `.env`.

---

## PART C — Error code evaluation (taxonomy)

Bitget V2 wraps everything: `{"code":"00000","msg":"success","data":...}`. Non-zero → `BitgetError`.

| Code | Meaning | Bot behavior | Repairable? |
|---|---|---|---|
| `00000` | success | — | — |
| `40006` | invalid ACCESS_KEY | auth error, stop | NO |
| `40009` | sign signature error | signing path bug (heredoc) | NO |
| `40762` | order amount exceeds balance | refuse entry (B2) | NO (config) |
| `40774` | size precision / unilateral type | re-round + resubmit (BUG B3/B8) | YES |
| `40804` | closed qty > held | round_qty reduce_only (B4) | YES (code) |
| `40808` | param verification | re-round | YES |
| `43011` | holdSide long/short invalid | use buy/sell (B1) | YES (code) |
| `43012` | insufficient (size) | re-round | YES |
| `43023` | Insufficient position (place-tpsl) | B1 silent drop | NO (venue) |
| `45110` | below min amount | re-round / skip | YES |
| `429` | too frequent | rate-limit bucket + breaker | DELAY |
| `30002` | empty WS subscribe | B5 sentinel | YES (code) |
| `NO_SPEC` | no contract for symbol | wrong product_type/symbol | NO |
| `NO_CREDS` | no Bitget keys | paper-only | NO |
| `NO_OID` / `DUPLICATE_SKIPPED` | idempotency guard | safe skip | YES |

**Repairable set** (deterministic re-round + single resubmit in `order.py`):
`40762`(size/balance), `40774`, `45110`, `43012`, `40808`. Others (40006/40009/43023/NO_*) are NOT
auto-repaired.

---

## PART D — Rate-limit gate (anti-ban)

`src/exchanges/rate_limit.py` token buckets (per IP / per key), **delay** (not drop):

| Category | Limit | Used for |
|---|---|---|
| `public` | 20/s | market data (ticker/contracts) |
| `signed` | 10/s | default signed |
| `order` | 1/s | place/cancel/tpsl |
| `account` | 5/s | positions/accounts/leverage |
| `ws` | 8/s | WS messages |

- 5 consecutive `429`s → **circuit breaker PARKS the bot** (`rate_limit_breaker_open=True`); operator
  clears via `rate_limit.reset()`. The bot refuses NEW orders while open.
- `rate_limit.acquire("ws")` is `async` — fire via `call_soon_threadsafe(asyncio.ensure_future(...))`,
  never call synchronously (silent coroutine-never-awaited bug).

---

## PART E — Dashboard / HTTP API (read + control)

Base `127.0.0.1:8092` (nginx → `bitget.warga-digital.com`). Source: `src/web/dashboard.py`.

| Method | Path | Auth | Purpose | Status |
|---|---|---|---|---|
| GET | `/api/health` | none | `{ok,mode,deadman_halted}` | ✅ |
| GET | `/api/state` | none | full state JSON (gate, positions, skipped, breaker) | ✅ |
| GET | `/api/snapshot` | none | dashboard state (positions/open_orders/pending) | ✅ |
| GET | `/api/trace/{trace_id}` | none | trace-spine stages for a signal | ✅ |
| GET | `/` | none | HTML dashboard | ✅ |
| POST | `/api/close` | `x-dash-password` | market-close a symbol | ✅ (margin_mode threaded) |
| POST | `/api/paper` | `x-dash-password` | return to paper | ✅ |
| POST | `/api/mint_go_live` | `x-dash-password` | mint token into RUNNING gate | ✅ (use THIS, not CLI) |
| POST | `/api/live` | `x-dash-password` | redeem token → LIVE | ✅ |

**Auth:** `DASH_PASSWORD` header `x-dash-password`. Read endpoints are open (monitoring). State-changing
ones are operator-gated. Go-live requires mint (single-use, 5-min TTL) then redeem.

---

## PART F — Telegram operator command surface (17 commands)

Prefix `bitget_`. Source: `src/notifier/telegram.py` `COMMANDS`. Read-only = open; state-changing =
operator-gated (`TG_OPERATOR_ID`); router fails CLOSED when `TG_OPERATOR_ID` unset.

**Monitor (open):**
`/bitget_status` `/bitget_positions` `/bitget_pnl` `/bitget_balance` `/bitget_activity [n]`
`/bitget_health` `/bitget_price <SYM>` `/bitget_limit_orders` `/bitget_pairs` `/bitget_help`

**Trade (operator):**
`/bitget_open <SYM> <BUY|SELL> <sl> [tp] [usd]` — requires SL, refuses wrong-side stop.
`/bitget_close <SYM|all>` — market close (margin_mode threaded).
`/bitget_sl_to_entry <SYM>` — stop to break-even (re-arms bot-side monitor).

**Control (operator):**
`/bitget_toggle` — paper↔live.
`/bitget_highrisk [on|off]` — toggle HIGH-risk sizing profile (persists to `fatty_config`).
`/bitget_paper` — immediate paper.
`/bitget_live <token>` — redeem go-live token.

**Notes:** old names (`/close`,`/status`) still route for back-compat. `setMyCommands` publishes the
menu at startup — verify via `getMyCommands`, not by assuming. Long-poll `timeout=POLL_TIMEOUT_S+10`
explicitly (default 15s aborted client-side → metronomic ~20s "getUpdates failed" + masked 409).

---

## PART G — MUST NOT DO (hard rules)

1. **Never restart over an open position without reconciling live state first.** Prefer inserting
   `bc_armed_stops` for the symbol, then restart, so the bot-side monitor survives. "jangan rusak
   posisi yang ada."
2. **Never assume a `place-tpsl-order` 200 means protection is live.** Always verify via
   `get_protection()`; the position is naked until the venue echoes SL/TP.
3. **Never edit `/root` and push to deploy** — `/root` is often stale/behind `origin/main`. Deploy
   from `/opt/bots/bitget-listener`, push `main`, then `git pull --ff-only` + restart. If you must
   edit `/opt` directly, push from `/opt` (clean FF) then resync `/root`.
4. **Never put `.venv/bin/python`, `pytest`, `pip`, `.py` script paths in a raw terminal command on
   Sera** — Hermes' lifecycle guard crashes (`embedded null byte` / `no home dir`). Use `uv`, or
   `execute_code` → `subprocess` with `HOME=/root`, or write the script to a neutral path.
5. **Never hand-roll `BitgetAdapter` in a `python - <<EOF` heredoc** — `load_dotenv()` + signing die
   with `40009`. Use `build_runtime()` or pass an explicit `.env` path.
6. **Never flip `PAPER` via the unit's `Environment=PAPER=1`** to change mode — boot reads `paper`
   from `fatty_config`. Use `/api/paper` or the DB.
7. **Never leave `bc_armed_stops` rows for CLOSED symbols** — a restart re-arms a monitor for a flat
   account. Reconcile vs open positions; `DELETE` stale rows + truncate `armed_stops.json` + restart.
8. **Never run the fakes-only test suite with `PGPASSWORD` exported** to mask a regression — if a
   fakes-only test fails with `psycopg.OperationalError`, it's a REAL regression (a component opened a
   live DB it shouldn't). Fix the code (in-memory fallback), don't mask it.
9. **Never trust `bc_signals`** for this bot — the durable intake is `bc_signal_receipts` (59 rows).
   `bc_signals` is empty. Journald retention is short; Postgres `bc_*` is the only durable history.
10. **Never "fix" the 2 credential-refusal pytest failures by loosening assertions** — they fail only
    because real creds are present. Run CI without `.env`.
11. **Never call `round_qty(..., reduce_only=False)` on a close size** — causes `40804`.
12. **Never hammer `place-tpsl-order`** hoping one sticks — stop after ~10 probes (rate-limit/IP-ban).
13. **Never deploy a feature branch to live unknowingly** — always `git branch --show-current` in
    `/opt` first. A feature checkout = unmerged code running LIVE (seen: `feat/chart-vision-plugin`).
14. **Never reuse the `SUSDT-FUTURES` sandbox balance as "the account"** — real funds are on
    `USDT-FUTURES`. The bot is LIVE there with ~$8.57; the $3000 sandbox is demo only.
15. **Never let a generic watchdog restart/kill Hermes, the bot, Postgres, or unrelated services** —
    monitor RAM/swap/disk/inodes and bound heavy work; park, don't kill.

---

## PART H — Future development context

**Architecture recap:** `TelethonSource` (intake + catch-up) → `Orchestrator._loop` (guard → parse →
size → `OrderService` → `BitgetAdapter`) → `BitgetWS` (fills/positions, bot-side monitor) →
`PostgresLedger` (`bc_*`) + realtime Telegram pushes (`fmt.py`). Shared `bot_common` package holds
`repos`/`db`/`ensure_schema` (separate dir, no remote — migrate it).

**Open engineering items (priority order):**
1. Fix B7 trace stage gap (record `ORDER_FAILED` in `_loop` except handler).
2. Make `place-tpsl-order` reliable OR formally deprecate it and rely solely on bot-side monitor +
   preset-at-entry; document the limitation in the UI so "protected" never lies.
3. HIGH-risk sizing vs tiny balance: add a hard pre-flight that refuses to boot LIVE when
   `min_notional > equity` and warns instead of rejecting every entry silently.
4. `bc_armed_stops` auto-prune for closed symbols on `full_close` (currently manual).
5. Realtime push for rejected entries (so operator sees the 40762 immediately, not buried in journal).
6. Multi-bot attribution: shared `NIBIRU`/fattyfatclub channel alerts may come from
   learnernoearner/mexc — confirm `bot_name` + balance before attributing.

**Where to add code:**
- New endpoint → `src/exchanges/bitget.py` (add `P_*` constant + adapter method; keep signing in
  `_signed`).
- New safety rail → `src/services/orchestrator.py` `guard()` / `deadman.py`.
- New Telegram command → `src/notifier/telegram.py` `COMMANDS` + `bot_poller.py` router.
- New dashboard card → `src/notifier/fmt.py` (formatting ONLY, no logic) + `src/web/dashboard.py`.
- New durable record → `bot_common/repos.py` + `bot_common/db.py` DDL (apply as `postgres`, grant
  `app`).

**Testing contract:** fakes-only suites (`test_phase5/7/8/10/11_*`) must be GREEN with zero Postgres.
Run via `execute_code` → `subprocess` (venv python list-form), never a raw `pytest` terminal call.
Full suite green target: ~505 passed.

**Migration:** see `MIGRATION.md` (tarball on Sera includes encrypted `secrets.gpg`, bot_common, pg
dump, systemd, nginx). Everything is on `main`.
