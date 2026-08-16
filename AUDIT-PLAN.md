# AUDIT-PLAN — phased autonomous execution (self-healing loop)

This mirrors the operator's standing preference for long multi-axis tasks: per-phase prompt
files + an index/status checkpoint (⬜/✅), a recurring cadence that resumes from the checkpoint
after a failure, and a lock file so concurrent ticks don't collide.

## Checkpoint index
| Phase | Goal | Status | Verification |
|-------|------|--------|--------------|
| P0 | Scaffold repo: `src/`, `tests/`, `Dockerfile`, `systemd/`, `deploy.sh`, `CHANGELOG.md` | ⬜ | `python -m compileall src` clean; `pytest` importable |
| P1 | Domain (frozen dataclasses: Instrument, Signal, Fill, Position, OrderRequest) + `interfaces/ports.py` + fakes | ⬜ | fakes instantiate; no network |
| P2 | Signal ingestion: `sources/parser.py` (SymbolMap, mgmt fast-path), `sources/telegram.py` + FakeSignalSource | ⬜ | parser unit tests green |
| P3 | Bitget REST (testnet): **`bitget_api_probe.py` FIRST**, then `bitget_auth.py` + `bitget.py` | ⬜ | **probe GREEN**; signing unit-tested; fill shows real `execFee` |
| P4 | Bitget WS (testnet): `bitget_ws.py` login+subscribe+heartbeat+reconnect+callbacks | ⬜ | WS loop connects on testnet; marks/fills parsed |
| P5 | Order+position+real-time stops: `services/order.py`, `position.py`, `repair.py` | ⬜ | reduce-only never cancelled; real liq price |
| P6 | Fee-aware ledger: `store/ledger.py` (net = gross − fees) | ⬜ | trade row has open_fee/close_fee |
| P7 | Reflexivity + ShadowLedger: `eval/shadow_ledger.py`, per-pair win-rate | ⬜ | weak-pair flag/downsize logic tested |
| P8 | Learning loop: drift detection + param rotation (confirmation-gated) | ⬜ | no auto-apply to live |
| P9 | Notifier + go-live: `web/app.py` (FastAPI + WS), `notifier/bot.py`, GoLiveGate, `/toggle` | ⬜ | dashboard serves; `/live` gated |
| P10 | Replay + gate: `replay_real.py`, `replay_mgmt.py`; `deploy.sh` guard | ⬜ | zero crashes; deploy guard blocks on empty `[Unreleased]` |

## Lock file
`.audit.lock` records `{phase, utc_ts}` of the in-flight tick. A tick that finds a fresh lock
(<30 min old, same phase) skips to avoid collision; a stale lock self-heals (overwritten).

## Resume rule
On start, read this file. Jump to the first ⬜ phase. On failure, write `BLOCKED.md` (what
credential/secret is missing) and stop — do NOT fabricate progress. On success of a phase, flip
⬜→✅, commit, and continue.

## Cadence
Re-run every 5 minutes while not blocked (operator preference). Each tick is idempotent: it
re-derives state from the checkpoint, not from memory.
