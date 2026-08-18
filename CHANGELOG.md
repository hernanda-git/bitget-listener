# CHANGELOG

## [Unreleased]

### Ops audit 2026-08-18 (Sera production review)
- `requirements.txt` / `requirements-dev.txt` added. The production venv had no
  `pytest-asyncio`, so the suite reported **93 failed / 221 passed** purely
  because `pytest.ini` sets `asyncio_mode = auto` and the plugin was absent.
  With it: **314 passed**. Re-verified from a clean venv built only from the new
  manifests: **314 passed**.
- Telegram notifier wired (`TG_BOT_TOKEN`, `TG_CHAT_ID`, `TG_OPERATOR_ID` in the
  git-ignored `.env`). Before this the bot logged `[notifier:no-credentials]` and
  dropped every alert. `scripts/verify_notifier.py` proves real delivery
  (`send_returned: True`) and the operator gate (operator `True`,
  stranger `False`).
- `RUNBOOK.md` corrected: `WEB_ACTION_PASSWORD` -> `DASH_PASSWORD`,
  `$WEB_PORT`/8080 -> `$PORT`/8092, plus the real nginx + systemd topology.

#### NOT done, and why (no fabrication)
- **Symbol whitelist was NOT widened.** `SYMBOL_WHITELIST=BTCUSDT,ETHUSDT,XRPUSDT`
  is already the maximum. Live query of
  `GET /api/v2/mix/market/contracts?productType=SUSDT-FUTURES` returns exactly
  **3** contracts — `SBTCSUSDT`, `SETHSUSDT`, `SXRPSUSDT`. Signals such as
  `SNDKUSDT` are therefore correctly skipped; adding them would create orders for
  instruments the demo product does not list. Widening requires real keys and
  `BITGET_PRODUCT_TYPE=USDT-FUTURES`, which remains a documented go-live blocker.

### P11 — demo symbol translation + paper-no-creds fixes (regression-pinned)
- `src/exchanges/bitget.py` + `src/streaming/bitget_ws.py`: added
  `venue_symbol()` / `canonical_symbol()` so the demo product's `SBTCSUSDT`
  (not `BTCUSDT`) is used for requests/subscriptions while the domain keeps seeing
  `BTCUSDT`. VERIFIED against `/contracts`.
- `src/services/orchestrator.py`: paper mode now tracks the position locally instead
  of calling the signed `positions.sync()`; a failed LIVE sync keeps the local view
  instead of losing the open position.
- `scripts/run_tests.sh`: now also runs the no-creds paper smoke against the real
  demo product.
- `tests/test_phase11_demo_symbols_and_paper.py`: 16 regression tests pinning both
  bugs (found only by the live run, not by the fakes-only suite).

### P12 — self-healing lock-file discipline (MASTER-PROMPT)
- `src/services/audit_lock.py`: `AuditLock` writes `.audit.lock`
  (`{phase, utc_ts, pid, host}`); a fresh lock owned by a live process blocks a
  concurrent tick; a stale / dead-owned lock self-heals.
- `scripts/self_heal_locks.py`: watchdog that inspects and reclaims a stale lock.
- `src/main.py`: `cmd_run` / `--web` acquire the lock and release on exit.
- `tests/test_phase12_lockfile_discipline.py`: 4 tests pinning the discipline.

### Docs
- `README.md`, `RUNBOOK.md`, `CHANGELOG.md` written; `AUDIT-PLAN.md` ledger updated
  to P0–P12 ✅ (go-live still ⛔ blocked on real keys + live Telethon session).

## [0.1.0] — scaffold
- Repo scaffold, sibling analysis, MASTER-PROMPT, AUDIT-PLAN.
