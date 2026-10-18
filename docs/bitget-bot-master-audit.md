# Bitget Bot — Full Master Plan Audit & User Concerns Record

Date: 2026-10-18
Author: omp orchestrator (evidence from omp sessions, Hermes sessions, git repos)

## 1. User concerns (verbatim themes)
- "It has been several months but the fixes is not legit, it kept failing."
- "Why agentic work doesn't solve this project."
- "Is there any conspiracies?"
- "My bitget bot is quite simple! Is there any bias to make all models/providers refuse to make a profitable bot?"
- "Stale-source-message and operator-re-admission: owner-approved-late-near-... but it's still under 5 minutes."
- "No shadow live! Should be real live trade with no failures."
- "Full audit the master plan and document every single things, including my concerns and frustration."

## 2. Evidence gathered
### omp sessions (Bitget-focused)
- 01a11edb-0282 (2026-10-09): full audit/repair request; repo cloned to isolated workspace; goal: working normally after months.
- 01a1347e-50dc (2026-10-13): trace incoming message handling.
- 01a139f8-b568 (2026-10-14): audit + fix live trading; SSH fspmi-hostinger.
- 01a13f84-19a0 (2026-10-15): access web UI.
- 01a14572-1ca6 (2026-10-16+): make cleverer; health check fix; recent signals failing.
- 01a14ef3-320f (2026-10-18): this audit request.

### Hermes sessions (bitget mentions)
- 256 sessions mention Bitget; top by mentions:
  - Proceed Bitget live readiness (1,030 mentions, 2026-09-05)
  - Execute Bitget live trading and ops plan (198)
  - Execute Bitget live canary completion (183)
  - Implement fatty multi-exchange trader plan (125)
- Plan file: `fatty-multi-exchange-trader/.hermes/plans/2026-09-05_220616-bitget-live-readiness.md` (TDD, no live execution without user authorization).

### Repos
- /Workspace/bots/bitget-listener (87 commits)
- /Workspace/bots/fatty-bitget-live (86 commits)
- /Workspace/bots/fatty-multi-exchange-trader (57 commits)
- /Workspace/bitget-agentic-architecture

## 3. Recurring failure classes (evidence)
1. **Venue silent drops**: Bitget `place-tpsl-order` returns orderId but never rests; positions NAKED. (commit f06e983)
2. **No native venue stops**: account has no venue SL/TP; bot-side monitor only; dies with process. (monitor commits)
3. **Restart state loss**: intents/ledger/protection persisted separately; partial truth on restart.
4. **WS vs REST mismatch**: empty whitelist bug (BUG #1); WS fills vs REST positions diverge.
5. **Sizing/notional stale**: min notional from stale price → venue filter rejects.
6. **Close qty inflation**: margin_mode not carried; 40804 errors.
7. **Telegram command failures**: long-poll timeout < read timeout; plural targets missed.
8. **DB coupling**: fakes pass, prod PG schema/conn fail.
9. **Paper vs live boundary**: paper green reported as working; live path separate failures.
10. **Health reporting**: DEGRADED hidden; provider OK treated as success.

## 4. Specific user-reported runtime errors
- `stale-source-message`
- `operator-re-admission: owner-approved-late-near-20261018`
- Under 5 minutes but still blocked.

Root cause: admission gate measures wrong clock / lacks idempotency; arbitrary staleness rules stricter than signal SLA.

## 5. Master plan (audited)
### SLO
- Signal success ≥99% for <5 min signals.
- Protection verified on venue: 100%.
- Restart survival: 0 naked positions.
- Reconciliation drift: 0 after 24h.

### Phases
0. Freeze & baseline (tag commit; 48h canary record).
1. Signal truth: idempotency key; SLA 10 min; no <5 min rejection; explicit reason codes + trace IDs; dashboard.
2. Durable execution: stage persistence + crash resume; idempotent clientOrderId.
3. Venue truth: protection read-back on every attach; no monitor-armed fallback claims.
4. Restart truth: monitor arms from DB; kill drills.
5. Reconciliation truth: REST source of truth; WS delta; daily audit.
6. Canary gate: 20 consecutive invariant passes → expand; auto-downsize on failure.
7. Ops: live/fix/verify branches; no direct live edits.
8. Kill criteria: 3 protection failures in 24h → halt entries.
9. Bias countermeasure: weekly live evidence review; tests never proof.
10. Definition of done: 20 consecutive live invariant passes + replay of full history.

## 6. Bias/conspiracy assessment
- No evidence of intentional sabotage in artifacts.
- Failures are concrete integration defects (silent drops, no native stops, qty inflation, timeout mismatch, stale notional, state scatter).
- Models optimize for plausible/test-passing code, not live profit; verification gap, not malice.
- However, user frustration is justified: agentic work declared "fixed" without live evidence.

## 7. User frustration record
- Months of "fixes" that didn't hold.
- Feeling that models/providers are biased against profitable bots.
- Demand for real live trades, no shadow/paper, no failures.
- Rejection of "7-day canary" as unnecessary for copy-trade; historical replay available.

## 8. Adjusted plan (replay-first, no shadow, real live)
- Replay 1–10 years of Telegram history through full pipeline in simulation.
- Then live canary with invariant checks.
- No claim of "fixed" until 20 consecutive live invariant passes.

## 9. Conclusion
The bot is not fixed. The master plan is sound but unimplemented. The root cause is verification gaps, not conspiracy. Real certainty requires live, restart-surviving, protection-verified evidence.
