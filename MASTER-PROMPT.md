# MASTER PROMPT — Build `bitget-listener` (Bitget USDⓈ-M Futures signal-execution bot)

> This file is the **single source of truth / goal** for the autonomous agent on the other
> machine. It is self-contained: everything needed is vendored in this repo. The agent reads
> this file first and executes until the goal is met or hits a hard blocker it cannot clear.

## ROLE & OBJECTIVE
You are building **`bitget-listener`**: a 5th sibling trading bot that executes `@fattyfatclub`
Telegram signals on **Bitget USDⓈ-M Futures** (USDT-settled perpetuals). It must be the **best
engineering** of the four existing siblings (Binance, Bybit, MEXC, Hyperliquid) — combining the
cleanest parts of each. The bot is **paper-by-default** and must NEVER place a real order until
a hardened go-live gate passes.

## INPUTS ALREADY PROVIDED (do not re-fetch; they are in this repo)
- `analysis/00-INDEX.md` → start here. Read ALL of `analysis/0*.md` before writing code.
- `analysis/01-EXCHANGE-BINANCE.md` … `04-EXCHANGE-HYPERLIQUID.md` → how each sibling integrates
  its exchange (REST + WebSocket). These are the reference implementations to learn from.
- `analysis/05-COMPARATIVE-ANALYSIS.md` → what's better and why (copy Bybit's skeleton, MEXC's
  execution rigor + Web UI, Hyperliquid's DB multi-bot, Binance's management commands).
- `analysis/06-BITGET-DESIGN.md` → the target architecture, module map, safety gates, go-live gate.
- `analysis/07-BITGET-IMPLEMENTATION-NOTES.md` → concrete Bitget V2 signing / endpoints / WS
  payloads. **Every Bitget API detail is marked `⚠ UNVERIFIED` — you MUST verify with the probe.**
- `sibling-repos/siblings.tar.gz` → the 4 existing repos (already cloned). Extract to read real code:
  `tar -xzf sibling-repos/siblings.tar.gz` (lands `learnernoearner-listener/`, `mexc-listener/`,
  `bybit-listener/`, `hyperliquid-fatty-trader/`).

## HARD RULES (non-negotiable — trading real money is at stake)
1. **Paper by default.** No real Bitget order until `bitget_api_probe.py` is GREEN, ShadowLedger
   expectancy > 0, and all P0–P3 tests are green, AND a go-live token was redeemed.
2. **Verify the Bitget API against reality.** Web docs may be wrong/stale — the `bitget_api_probe.py`
   script (spec in `analysis/06-BITGET-DESIGN.md` §9) is the arbiter. Write it FIRST, run it, fix
   the UNVERIFIED claims in `07-BITGET-IMPLEMENTATION-NOTES.md` to VERIFIED, then write order code.
3. **Never auto-cancel `reduce_only` protective orders** (BUG C).
4. **Record real `execFee` on every fill (BUG A) and book liquidation at the real price, never
   `0.00` (BUG B).**
5. **No LLM fallback for trades.** Order repair is deterministic (`RepairOrder`); the LLM only
   decides, never fixes an order.
6. **Secrets only in `.env`** (git-ignored). Never echo keys/tokens to chat/logs.
7. **Testnet-first.** If Bitget has no separate futures testnet, use the test flag / tiny-notional
   + paper gate — never mainnet with real size.
8. **Document as you go.** Update `07-BITGET-IMPLEMENTATION-NOTES.md` with VERIFIED facts. Keep a
   `CHANGELOG.md` with an `[Unreleased]` section; `deploy.sh` must block if it's empty or tests fail
   (copy bybit's guard).
9. **Never assert root cause without proof; retract overclaims.**

## BUILD SEQUENCE (TDD, each phase independently testable with fakes)
P0 scaffold → P1 domain/ports/fakes → P2 signal ingestion → **P3 Bitget REST (testnet) —
including `bitget_api_probe.py` FIRST** → P4 Bitget WS (testnet) → P5 order+position+real-time
stops → P6 fee-aware ledger → P7 reflexivity+ShadowLedger → P8 learning loop → P9 notifier+go-live
→ P10 replay+gate.

## DELIVERABLES (definition of done)
- [ ] `bitget_api_probe.py` GREEN from the target deploy IP; answers all 7 open questions in
      `07-BITGET-IMPLEMENTATION-NOTES.md` §8 and they are marked VERIFIED.
- [ ] `src/exchanges/bitget.py` + `bitget_auth.py`: place/cancel/leverage/positions/balance/fills.
- [ ] `src/streaming/bitget_ws.py`: login + subscribe + heartbeat + reconnect + callbacks.
- [ ] `src/services/repair.py`: `validate_order` + `RepairOrder` (no LLM fallback), idempotency dedup.
- [ ] `src/web/app.py`: FastAPI Web UI (PRIMARY) + `/ws/events` + `ShadowLedger`.
- [ ] Postgres `fatty_config` row + persisted `paper` flag + `/toggle` (copy Hyperliquid).
- [ ] ≥81 fakes-only unit tests, zero network. `pytest` green required before any deploy.
- [ ] `deploy.sh` guard (blocks on empty `[Unreleased]` / failing tests).
- [ ] Repo pushed to `hernanda-git/bitget-listener` (private) with this prompt + docs + working code.

## EXECUTION STYLE
- Be autonomous and thorough; prioritize correctness over speed.
- Use a phased self-healing loop: per-phase prompt files + an index/status checkpoint (⬜/✅),
  recurring cadence to resume from the checkpoint after a failure, and a lock file so ticks don't
  collide (per the operator's standing preference).
- When blocked (e.g. needs real Bitget API keys / KYC / a live Telethon session), STOP and write a
  clear `BLOCKED.md` describing exactly what secret/credential is missing and how to provide it —
  do NOT fabricate keys or pretend go-live succeeded.
- Commit atomically and preserve history. Push to the `main` branch of this private repo.

## SUCCESS SENTENCE
"bitget-listener builds, all fakes-only tests pass, bitget_api_probe.py is GREEN and the V2
endpoints are VERIFIED, and it is paper-ready behind a hardened go-live gate."
