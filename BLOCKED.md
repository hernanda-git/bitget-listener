# Audit remediation blockers

Checked: 2026-08-24 11:45 WIB (`Asia/Jakarta`)

## Bitget chart vision live activation

Blocked because the deployed service environment does not define a validated:

- `CHART_VISION_ENABLED`
- `CHART_VISION_API_URL`
- `CHART_VISION_API_KEY`
- `CHART_VISION_MODEL`

The remediation keeps chart vision fail-closed and paper-testable. No endpoint, model, or credential was guessed.

## Production trace storage

The SQLite paper/fake ledger has durable receipt, stage, and pending-setup storage with regression coverage. The live service uses `PostgresLedger`; a production `signal_receipts`, `trace_events`, and pending-setup migration plus repository read-back still requires schema approval before rollout.

## Safe rollout

The running services were not restarted. A paper/demo rollout must be separately authorized and performed one bot at a time after the Postgres migration and dashboard/read-back checks exist.

## Shared package versioning and migration gate

Continuation code adds `bc_signal_receipts`, `bc_trace_events`, and `bot_common.repos.Traces` under `/opt/bots/bot_common`. The directory is now a local Git repository at commit `e0bcbf26b02ff5d3628f2fa7d90e6a7661e5e5e3`, but it has no configured remote or deployment release integration. The DDL and repository code compile and fake repository tests pass, but the live Postgres schema has not been migrated. Do not restart or promote the funded service until this shared package is attached to the deployment's versioned release mechanism, the migration is reviewed, and a paper/demo startup proves receipt and stage read-back.

## Continuation safety work completed locally

- Generic conditional prose is deterministic and fail-closed; pullbacks preserve a LIMIT trigger.
- Raw direction conflicts produce `DIRECTION_CONFLICT` and terminal trace stages.
- Live close accounting requires authoritative fill and position read-back; unavailable read-back is `UNKNOWN`.
- Entry and close order builders explicitly use `crossed` margin mode.
- Repeatable 100-message fake composition-root replay passes.
- Dashboard mobile/browser verification passes in an isolated paper server; no funded service was restarted.

## Code-review remediation completed locally

- Catch-up receipts are durable before parsing.
- Invalid pending setups and pending direction conflicts fail closed.
- LLM direction is fail-closed and raw/model direction metadata is persisted.
- Partial close ledger legs preserve remaining open quantity.
- Close read-back retries and zero/oversized rounded closes are rejected.
- psycopg2 compatibility and canonical duplicate trace IDs are covered.
- Trace endpoint requires the dashboard password.
- Fresh Bitget probe is `GREEN=11 WARN=2 RED=0` after accepting the current ticker snapshot heartbeat response.
- Bitget branch latest pushed commit: `006ac721f1b927ec0189509ba23107803107cfef`.

## Complete evidence chain

- Receipts preserve raw text, hashes, reply context, media metadata, process/host identity, schema version, and timestamps.
- Trace read-back includes the receipt row plus ordered parser, LLM, vision, decision, gate, order, fill, protection, position, close, bookkeeping, and evaluation evidence where available.
- Downloaded chart media is hashed and recorded with path and byte length. Production must use durable access-controlled media storage instead of `/tmp` before relying on media replay.
- Latest pushed Bitget commit: `f01ba384b573c0f60bd3960a9ab3a0723b354aac`.
