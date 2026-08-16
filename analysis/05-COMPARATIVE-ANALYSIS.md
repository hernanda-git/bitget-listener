# 05 — Comparative Analysis: which is better, and why

Scoring the four siblings on the dimensions that matter for a **production trading bot that
must not lose money by accident**. Scores are 1–5 (5 = best). Rationale is grounded in the code
I read.

## 1. Scorecard
| Dimension | Binance (`learner`) | Bybit | Hyperliquid | MEXC | Winner |
|-----------|:---:|:---:|:---:|:---:|--------|
| **Code maturity / battle-tested** | 5 | 3 | 4 | 2 | Binance |
| **Architecture / SOLID+DI** | 3 | 5 | 4 | 4 | Bybit |
| **Exchange integration correctness** | 3 | 5 | 4 | 4 | Bybit |
| **Safety gates (fees/liq/reduce-only)** | 2 | 5 | 4 | 5 | Bybit/MEXC |
| **Test coverage (fakes-only)** | 3 | 5 | 4 | 5 | Bybit/MEXC |
| **WebSocket resilience** | 5 | 4 | 4 | 4 | Binance |
| **Observability (Web UI + WS)** | 3 | 3 | 3 | 5 | MEXC |
| **Multi-bot / config DB** | 2 | 4 | 5 | 4 | Hyperliquid |
| **Self-correcting (ShadowLedger)** | 2 | 4 | 3 | 5 | MEXC |
| **Geo-block immunity** | 2 | 4 | 5 | 2 | Hyperliquid |
| **Doc quality (in-repo)** | 4 | 4 | 5 | 5 | HL/MEXC |
| **TOTAL (avg)** | **3.1** | **4.2** | **4.1** | **4.1** | **Bybit (by a hair)** |

> Read the totals as *relative engineering quality*, not trading performance. None of the four
> publishes a verified live Sharpe; the bots are paper-governed and the "edge" is the signal
> channel, not the code.

## 2. What's better — per repo

### Binance (`learnernoearner-listener`) — the proven workhorse
- **Best at surviving hostile infrastructure.** Its IP-ban circuit breaker + public-mark-price
  WS design is the most mature. It's the only one *actually deployed* (Fly.io, v115+).
- Richest management-command surface (`tp1 booked`, `sl to entry`, `full`, stale-LIMIT auto-cancel).
- **Worst at the things the others were built to fix:** fees not always recorded, liquidation
  sometimes `0.00`, reduce-only protective orders historically dropped. Largest file (1683 lines)
  = lowest SOLID score. It is the *ancestor*, and the other three are partly reactions to its gaps.

### Bybit (`bybit-listener`) — the engineering reference implementation
- **Best overall code.** Cleanest SOLID + dependency injection; the `ExchangeAdapter` port, pure
  frozen domain types, and fakes-for-everything testing discipline are the gold standard here.
- **Explicitly closes the 3 shared "fatal gaps":**
  - BUG A → records real `execFee` on every fill (`/v5/execution/list` + WS `execution`).
  - BUG B → books liquidation at the **real** `liqPrice`, never `0.00`.
  - BUG C → **never** auto-cancels `reduce_only` protective orders.
- Signing/rounding centralized and **unit-tested against the algorithm**; testnet-first so it
  physically cannot touch mainnet by accident.
- **Best to copy as the skeleton** for the Bitget sibling.

### Hyperliquid (`hyperliquid-fatty-trader`) — the infra leader
- **Most advanced runtime:** DB-backed multi-bot config with persisted `paper` flag (`/toggle`
  survives restart), WS-first single connection (zero REST weight for SL monitoring), on-chain
  transparency, no IP-ban surface.
- Cleanest settlement model (`UsdNotional` keeps USD separate from USDC; USDT→USDC handled once
  in `SignalNormalizer`).
- **Most complex auth** (EIP-712, two signing schemes, asset-index caching) — the `422` history
  proves it's a foot-gun. Best docs of the four (`hyperliquid-api.md` is a real reference).

### MEXC (`mexc-listener`) — the youngest but most rigorous design
- **Best execution rigor:** `validate_order` + deterministic `RepairOrder` (no LLM fallback for
  trades), idempotency dedup, magnitude-flip rounding guards (a $0.16 coin never becomes $100).
- **Best observability:** FastAPI Web UI is the *primary* operator surface, plus a WS event
  mirror. Best self-correcting logic: `ShadowLedger` per-pair win-rate that flags/downsizes weak
  pairs.
- **Worst real-world readiness:** only fakes-only tested, never hit live MEXC, depends on a
  Cloudflare Worker to defeat the Jakarta geo-block, and MEXC futures needs KYC + keys expire
  every 90 days.

## 3. Cross-cutting pros (what ALL four get right)
- Paper-by-default with a persisted go-live gate (no real order without explicit authorization).
- Management commands bypass the LLM entirely (~200ms) — fast, deterministic, auditable.
- Idempotency (message_id / client_order_id) so duplicate signals can't double-fill.
- WS-first position/fill monitoring instead of REST polling (except Binance's old path, now fixed).
- Dead-man's switch / feed-stale circuit breaker as an immutable safety layer.
- Real-liquidation-price + real-fee recording as a design principle (post-bybit).

## 4. Cross-cutting cons / shared risks
- **Single signal source.** All four depend on `@fattyfatclub`. If the channel dies or is
  manipulated, every bot is blind. No second source or sanity-cross-check on the *signal* itself
  (only on the *order*).
- **LLM brain is a black box** for entry decisions (except bybit's FastPath pre-LLM). A wrong LLM
  call can still size/direction a trade within Gate2 bounds.
- **No cross-exchange hedging/arbitrage** — they're four parallel single-venue bots, not one
  portfolio. Correlation risk across them is unmanaged.
- **Geo-block fragility** for Binance (IP-ban) and MEXC (Jakarta block) — HL and (to a lesser
  extent) Bybit are immune; Bitget should be assessed for the same before go-live.

## 5. Recommendation for the Bitget build
Take **Bybit's skeleton** (SOLID + DI, fakes, centralized signing/rounding, BUG A/B/C fixes) as
the base, graft **MEXC's execution rigor** (`validate_order` + `RepairOrder`) and **Hyperliquid's
DB multi-bot / persisted paper flag**, and add **MEXC's Web UI + ShadowLedger** if you want
parity. Use **Binance's management-command depth** as the command-surface target. See `06-BITGET-DESIGN.md`.
