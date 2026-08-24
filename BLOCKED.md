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

Continuation code adds `bc_signal_receipts`, `bc_trace_events`, and `bot_common.repos.Traces` under `/opt/bots/bot_common`. The directory is not a Git checkout, so the change is not represented by the pushed Bitget commit `efa90358cafb87292283836c989db6a69acc6724`. The DDL and repository code compile and fake repository tests pass, but the live Postgres schema has not been migrated. Do not restart or promote the funded service until this shared package is placed under the deployment's versioned release mechanism, the migration is reviewed, and a paper/demo startup proves receipt and stage read-back.

## Continuation safety work completed locally

- Generic conditional prose is deterministic and fail-closed; pullbacks preserve a LIMIT trigger.
- Raw direction conflicts produce `DIRECTION_CONFLICT` and terminal trace stages.
- Live close accounting requires authoritative fill and position read-back; unavailable read-back is `UNKNOWN`.
- Entry and close order builders explicitly use `crossed` margin mode.
- Repeatable 100-message fake composition-root replay passes.
- Dashboard mobile/browser verification passes in an isolated paper server; no funded service was restarted.
