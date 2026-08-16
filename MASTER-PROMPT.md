# bitget-listener — AUTONOMOUS BUILD (paste this into your Hermes Telegram channel)

You are building **`bitget-listener`**: a Bitget USDⓈ-M Futures signal-execution bot that executes
`@fattyfatclub` Telegram signals (paper-by-default, hardened go-live gate). It is the 5th sibling
after `learnernoearner-listener` (Binance), `bybit-listener`, `mexc-listener`, and
`hyperliquid-fatty-trader`. Build the **best engineering** of the four.

## STEP 0 — Acquire context (do this first)
```bash
cd ~ && rm -rf bitget-listener && \
git clone https://github.com/hernanda-git/bitget-listener && \
cd bitget-listener && \
tar -xzf sibling-repos/siblings.tar.gz
```
Then READ in order:
1. `analysis/00-INDEX.md` (map)
2. `analysis/01-EXCHANGE-BINANCE.md` … `04-EXCHANGE-HYPERLIQUID.md` (how each sibling does REST+WS)
3. `analysis/05-COMPARATIVE-ANALYSIS.md` (what's better, why)
4. `analysis/06-BITGET-xxxx` → `analysis/06-BITGET-DESIGN.md` + `07-BITGET-IMPLEMENTATION-NOTES.md` (target + Bitget V2 specifics)
5. `AUDIT-PLAN.md` (phased P0–P10 checkpoint — your execution tracker)
The 4 existing repos are now extracted locally (`learnernoearner-listener/`, etc.) — read their
real `src/` code as reference.

## HARD RULES (non-negotiable — real money is at stake)
1. **Paper by default.** No real Bitget order until: `bitget_api_probe.py` GREEN + ShadowLedger
   expectancy > 0 + all P0–P3 tests green + go-live token redeemed.
2. **Verify Bitget V2 against reality.** Every Bitget API detail in `07-BITGET-IMPLEMENTATION-NOTES.md`
   is marked `⚠ UNVERIFIED` — write `scripts/bitget_api_probe.py` FIRST, run it, and mark each
   claim VERIFIED (or fix it) before writing order code.
3. **Never auto-cancel `reduce_only` protective orders** (BUG C).
4. **Record real `execFee` on every fill (BUG A); book liquidation at the real price, never `0.00` (BUG B).**
5. **No LLM fallback for trades** — order repair is deterministic (`RepairOrder`); LLM only decides.
6. **Secrets only in `.env`** (git-ignored). Never echo keys/tokens.
7. **Testnet-first.** If Bitget has no separate futures testnet, use the test flag / tiny notional
   + paper gate — never mainnet with real size.
8. **Document as you go.** Update `07-BITGET-IMPLEMENTATION-NOTES.md` with VERIFIED facts; keep a
   `CHANGELOG.md` `[Unreleased]` section; `deploy.sh` must block if empty or tests fail.
9. **Never assert root cause without proof; retract overclaims.**

## BUILD SEQUENCE (TDD; each phase independently testable with fakes)
P0 scaffold → P1 domain/ports/fakes → P2 signal ingestion → **P3 Bitget REST (testnet) — probe
FIRST** → P4 Bitget WS (testnet) → P5 order+position+real-time stops → P6 fee-aware ledger →
P7 reflexivity+ShadowLedger → P8 learning loop → P9 notifier+go-live → P10 replay+gate.

## WHAT TO COPY FROM WHOM
- **Bybit skeleton** (`interfaces/ports.py`, `exchanges/bybit.py` structure, centralized
  signing/rounding unit-tested against the algorithm).
- **MEXC execution rigor** (`validate_order` + deterministic `RepairOrder`, idempotency dedup,
  magnitude-flip rounding guards) + **Web UI** (`web/app.py`) + **ShadowLedger**.
- **Hyperliquid** Postgres `fatty_config` multi-bot row + persisted `paper` flag + `/toggle`.
- **Binance** management-command fast-path (`tp1 booked`, `sl to entry`, `full`, stale-LIMIT cancel).

## DELIVERABLES (definition of done)
- [ ] `scripts/bitget_api_probe.py` GREEN from this machine's IP; all 7 open questions in
      `07-BITGET-IMPLEMENTATION-NOTES.md` §8 answered + marked VERIFIED.
- [ ] `src/exchanges/bitget.py` + `bitget_auth.py`: place/cancel/leverage/positions/balance/fills.
- [ ] `src/streaming/bitget_ws.py`: login + subscribe + heartbeat + reconnect + callbacks.
- [ ] `src/services/repair.py`: `validate_order` + `RepairOrder` (no LLM), idempotency dedup.
- [ ] `src/web/app.py`: FastAPI Web UI (PRIMARY) + `/ws/events` + `ShadowLedger`.
- [ ] Postgres `fatty_config` row + persisted `paper` flag + `/toggle`.
- [ ] ≥81 fakes-only unit tests, zero network. `pytest` green before any deploy.
- [ ] `deploy.sh` guard (blocks on empty `[Unreleased]` / failing tests).
- [ ] Code committed + pushed to `hernanda-git/bitget-listener` (private) `main`.

## EXECUTION STYLE
- Autonomous + thorough; correctness over speed.
- Use the self-healing loop: keep `AUDIT-PLAN.md` as the ⬜/✅ checkpoint; write `.audit.lock`
  (`{phase,utc_ts}`) so concurrent ticks don't collide; resume from the first ⬜ after any failure.
- If you need real Bitget API keys / KYC / a live Telethon session: STOP, write `BLOCKED.md`
  describing exactly what's missing + how to provide it. **Do NOT fabricate keys or claim go-live.**
- Commit atomically, preserve history, push to `main`.

## SUCCESS
"bitget-listener builds, all fakes-only tests pass, bitget_api_probe.py GREEN + V2 endpoints
VERIFIED, paper-ready behind a hardened go-live gate."
