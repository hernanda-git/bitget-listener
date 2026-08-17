# RUNBOOK — bitget-listener operations

Concise, honest ops for a bot that is **paper-ready and go-live BLOCKED**.

## 1. How to monitor (read-only, harmless)

The dashboard loads **OPEN** — no login gate on the page or `/api/state`. Only
state-changing endpoints (`/api/paper`, `/api/live`, `/api/close`, …) require
`WEB_ACTION_PASSWORD`. Every control action is logged server-side at `ERROR`
("REFUSED: bad password", "AUTHORIZED", etc.), so all attempts are auditable.

- `GET /api/state` — current mode, positions, trades, fees, gate status, server time
  (rendered as **Asia/Jakarta / WIB**, UTC+7).
- `GET /api/health` — liveness.
- The Web UI is mobile-safe: **no horizontal scroll at 390px** (verified by a real
  headless browser, not a claim).

## 2. How to run

```bash
python -m src.main --web          # bot + dashboard on $WEB_PORT (default 8080)
python -m src.main                # bot only (paper loop)
```

Paper is the default and is persisted in the config store (Postgres `fatty_config`
row when `PGHOST` is set, else in-memory). A restart cannot silently flip to live.

## 3. Self-healing lock-file discipline

Only one live `run`/`--web` tick may hold the process. It writes `.audit.lock`
(`{phase, utc_ts, pid, host}`). A second live process that finds a *fresh* lock
(owned by a live PID, <30 min old) refuses to start. A stale or dead-owned lock is
self-healed on the next start.

Wire a watchdog (cron) to reclaim a stale lock if the process died without cleanup:

```cron
*/5 * * * *  cd /root/bitget-listener && python scripts/self_heal_locks.py >> /var/log/bitget-selfheal.log 2>&1
```

Exit `0` = clean/self-healed; `2` = a fresh lock is held by a live process (skip).

## 4. How to go live SAFELY (only with real keys + human present)

1. Put real `BITGET_API_KEY/SECRET/PASSPHRASE` in `.env` (never commit; `.gitignore`d).
2. `python -m src.main --probe` → GREEN on the real key.
3. `bash scripts/run_tests.sh` → all green.
4. `python -m src.main --go-live` → mints a **5-minute single-use** token only if all
   5 conditions hold: (a) probe GREEN on real key, (b) fee-aware expectancy > 0,
   (c) P0–P3 tests green, (d1) real non-zero fill fee, (d2) real liq price captured.
5. In the dashboard (password-gated) POST the token to `/api/live` within 5 minutes.

If any condition is unmet, `--go-live` refuses and returns non-zero. No token = no
live mode.

## 5. How to REVERT to paper

POST `{"paper": true}` to `/api/paper` (password-gated) flips the persisted flag back
to paper immediately; the next restart also boots paper. To fully stop live trading,
kill the process — it releases `.audit.lock` on exit.

## 6. Hard blockers (NOT attempted — would require fabrication)

- **Real Bitget API keys / KYC.** Paper mode runs with no keys by design. A live key
  must be supplied by a human in `.env`; the build never fabricates one.
- **Live Telethon session.** Reading `@fattyfatclub` needs an interactive user login.
  Use `--fake-source` for keyless runs; a real session is a documented blocker.

If either is genuinely required and missing, write `BLOCKED.md` describing exactly
what is needed and stop. **Do not fabricate keys, passphrases, or declare go-live.**

## 7. Failure playbook

| Symptom | Likely cause | Action |
|---|---|---|
| `refusing to start: another live tick holds .audit.lock` | second process / dead PID | check for a live process; else `python scripts/self_heal_locks.py` |
| `NO_SPEC` on a symbol | symbol not on demo product | confirm it is `BTC/ETH/XRP` (demo only lists S*USDT) |
| `NO_CREDS` in paper mode | adapter called a signed endpoint | must not happen — paper tracks positions locally; file a bug |
| dead-man halt (`deadman_halted=true`) | feed stale > `DEADMAN_TTL_S` | check WS connectivity; bot halts, does not trade blind |
| control endpoint `401` | missing/wrong `WEB_ACTION_PASSWORD` | supply `x-dash-password` header |

## 8. Verification evidence (built-in)

- `bash scripts/run_tests.sh` — 314 fakes-only tests + live no-creds paper smoke.
- `python scripts/verify_dashboard_ui.py` — 24 real headless-Chromium UI checks.
- `python scripts/bitget_api_probe.py` — Bitget V2 reality check (public surface).
