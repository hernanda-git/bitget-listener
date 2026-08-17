# CHANGELOG

## [Unreleased]

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
