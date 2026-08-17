# bitget-listener

A **Bitget USDⓈ-M Futures** signal-copy trading bot. It executes `@fattyfatclub`
Telegram signals (currently paper-by-default) behind a hardened go-live gate. It is
the 5th sibling after `learnernoearner-listener` (Binance), `bybit-listener`,
`mexc-listener`, and `hyperliquid-fatty-trader`, and aims to be the best engineering
of the four.

> **Status: PAPER-READY, go-live BLOCKED.** The bot boots in paper on Bitget's demo
> product (`SUSDT-FUTURES`) with **no API keys**, runs the full entry→manage→close
> loop, and tracks P&L fee-aware. It cannot place a real order until a human supplies
> real keys and satisfies the 5 go-live conditions + redeems a single-use token.

## Run modes

```bash
python -m src.main                # paper run (default) — demo product, no keys
python -m src.main --web           # bot + monitoring dashboard on $PORT (default 8080)
python -m src.main --probe         # non-destructive Bitget API probe (public surface)
python -m src.main --go-live       # mint a one-time go-live token IF conditions hold
python -m src.main --fake-source   # drive signals from a fake source (no Telethon)
```

| Flag | What it does |
|------|--------------|
| *(none)* | Paper loop against `SUSDT-FUTURES`, monitoring via dead-man switch. |
| `--web` | Also serves the FastAPI dashboard at `0.0.0.0:$PORT`. |
| `--probe` | Runs `scripts/bitget_api_probe.py` (REST + WS public surface). |
| `--go-live` | Mints a 5-minute single-use TTL token only when all 5 gate conditions hold. |
| `--fake-source` | Uses `FakeSignalSource` — no live Telegram/Telethon session needed. |

## Environment variables

All secrets go in `.env` (git-ignored). See `.env.example`. Key vars:

- `BITGET_API_KEY` / `BITGET_API_SECRET` / `BITGET_PASSPHRASE` — empty in paper.
- `BITGET_PRODUCT_TYPE` — `SUSDT-FUTURES` (demo, default) or `USDT-FUTURES` (live).
- `PAPER` — `1` (default) boots paper; the persisted flag in the config store wins.
- `SYMBOL_WHITELIST` — comma list, e.g. `BTCUSDT,ETHUSDT,SOLUSDT`.
- `LEDGER_PATH` — trade ledger DB (default `bitget_listger.db`).
- `PORT` / `DASH_PASSWORD` — dashboard port (default 8080) + the password for
  state-changing endpoints only (monitoring loads OPEN, no login gate). Sent via the
  `x-dash-password` header; every control attempt is logged server-side at ERROR.
- `DEADMAN_TTL_S` — feed-staleness halt window (default 60s).
- `PGHOST` / `PGDATABASE` / … — if set, the bot uses the shared `fatty_config` Postgres
  row for the persisted `paper` flag; otherwise an in-memory store is used.

## Demo symbol translation (VERIFIED)

The demo product lists `SBTCSUSDT` / `SETHSUSDT` / `SXRPSUSDT`, **not** `BTCUSDT`.
The bot translates canonical↔venue symbols internally (`venue_symbol` /
`canonical_symbol` on both REST and WS adapters) so the domain always sees `BTCUSDT`.

## Go-live procedure

1. Supply real keys in `.env` (do **not** commit them; `.gitignore` excludes `.env`).
2. Run `python -m src.main --probe` → must be GREEN on the real key.
3. Run the test suite (`bash scripts/run_tests.sh`) → all green.
4. Build ShadowLedger expectancy history (paper) so condition (b) can be met.
5. `python -m src.main --go-live` mints a token **only if** all 5 conditions hold.
6. Redeem the token via the dashboard `/api/live` (password-gated) within 5 minutes.

The gate's 5 conditions are: (a) probe GREEN on the real key, (b) fee-aware
expectancy > 0, (c) P0–P3 tests green, (d1) a fill with a non-zero real fee (BUG A),
(d2) a real liquidation price captured (never `0.00`, BUG B). The persisted `paper`
flag means a restart cannot silently resurrect live trading.

## Hard rules (see MASTER-PROMPT.md)

Paper by default · verify Bitget V2 against a live probe before order code · never
auto-cancel `reduce_only` protective orders (BUG C) · record real fee + real liq
price · no LLM fallback for trades · secrets in `.env` only.

## Tests / verification

- `bash scripts/run_tests.sh` — compile check + 314 fakes-only tests (zero network) +
  the no-creds paper smoke against the real demo product.
- `python scripts/verify_dashboard_ui.py` — boots the dashboard and drives a REAL
  headless Chromium at 390x844, diffing rendered DOM VALUES against `/api/state`
  (24 checks: no horizontal scroll, real liq price, fee-aware net, WIB timestamps).
- `python scripts/self_heal_locks.py` — watchdog for the `.audit.lock` self-healing
  discipline (concurrent-tick guard).

## Layout

```
MASTER-PROMPT.md        # authoritative spec (read first)
AUDIT-PLAN.md           # P0–P12 checkpoint ledger (source of truth)
RUNBOOK.md              # ops: monitor, go live safely, revert to paper
CHANGELOG.md            # [Unreleased] change log
src/                    # the bot: adapters, services, web, store
tests/                  # fakes-only regression suite
scripts/                # probe, smoke, UI verify, self-heal
analysis/ sibling-repos/  # sibling reference material (vendored)
```
