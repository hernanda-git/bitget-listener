# bitget-listener

Autonomous build kit for a **Bitget USDⓈ-M Futures** signal-execution bot — the 5th sibling after
`learnernoearner-listener` (Binance), `bybit-listener`, `mexc-listener`, and
`hyperliquid-fatty-trader`. Executes `@fattyfatclub` Telegram signals on Bitget USDT perpetuals,
paper-by-default, hardened go-live gate.

This repo is **self-contained**: it carries the analysis of the 4 siblings, the blueprint, and a
master prompt that an autonomous agent can execute start-to-finish on another machine.

## Layout
```
MASTER-PROMPT.md          # THE GOAL — read first; self-contained objective for the agent
run-autonomous.sh         # bootstrap on the other machine: extract siblings, install Hermes, launch
AUDIT-PLAN.md             # phased self-healing checkpoint (P0–P10, ⬜/✅) + lock file rules
analysis/                 # 8 docs: per-exchange REST+WS, comparative, Bitget design + impl notes
  ├─ 00-INDEX.md
  ├─ 01-EXCHANGE-BINANCE.md
  ├─ 02-EXCHANGE-BYBIT.md
  ├─ 03-EXCHANGE-MEXC.md
  ├─ 04-EXCHANGE-HYPERLIQUID.md
  ├─ 05-COMPARATIVE-ANALYSIS.md
  ├─ 06-BITGET-DESIGN.md
  └─ 07-BITGET-IMPLEMENTATION-Notes.md   # Bitget V2 specifics — UNVERIFIED, confirm with probe
sibling-repos/
  └─ siblings.tar.gz      # the 4 existing repos (vendored; extract to read real code)
src/  tests/  scripts/  docs/   # <-- the bot is built here by the autonomous agent
```

## How to run it (other machine already has Hermes on Telegram)
This repo is built to be **pasted, not scripted**. The other machine already runs Hermes wired
to a Telegram bot channel — so:

1. Open that Hermes Telegram channel.
2. **Paste the entire contents of `MASTER-PROMPT.md`** into the chat.
3. Hermes clones this repo, extracts the sibling repos, reads the analysis, and builds the bot
   autonomously (paper-by-default; no real order without the go-live gate).

`MASTER-PROMPT.md` is self-contained: it includes the clone/extract step, the reading order, the
hard rules, the build sequence, and the definition of done. No shell bootstrap needed.

## Hard rules (see MASTER-PROMPT.md)
Paper by default · verify Bitget V2 against a live probe before order code · never auto-cancel
reduce-only protects · record real fee + real liq price · no LLM fallback for trades · secrets in
`.env` only.

## Status
Analysis + blueprint complete. Bot code is **not yet written** — that is the autonomous agent's
job, executed from `MASTER-PROMPT.md`.
