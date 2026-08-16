# project-newage — Analysis & Documentation Set

> Purpose: learn everything from the 4 private `hernanda-git` listener bots, document each
> exchange's REST + WebSocket surface **as implemented in the code**, compare them on
> quality, and design a 5th sibling: **Bitget USDⓈ-M Futures** (`bitget-listener`).

## Source repos (all cloned to `C:\Workspace\project-newage`)
| Repo | Exchange | Settlement | Maturity | Method |
|------|----------|------------|----------|--------|
| `learnernoearner-listener` | Binance USDⓈ-M Futures | USDT | **Most mature / production** (v115+, deployed Fly.io) | Signal → Regex → LLM(brain) → Gates → Binance REST/WS |
| `bybit-listener` | Bybit V5 USDT perps (linear) | USDT | **Highest code-quality / cleanest SOLID** (81 fakes-only tests) | Signal → Gate1 → FastPath/LLM → Gate2 → Bybit REST/WS |
| `hyperliquid-fatty-trader` | Hyperliquid on-chain perps | USDC | **Most advanced infra** (DB multi-bot, WS-first, EIP-712) | Signal → Normalizer → Validator → OrderService → HL REST/WS |
| `mexc-listener` | MEXC USDⓈ-M Futures | USDT | **Youngest / design-phase** (P0–P3 done, not go-live) | Fork bybit + learner ABC + HL watchdog |

## Document map (this set)
| File | Contents |
|------|----------|
| `00-INDEX.md` | This file. |
| `01-EXCHANGE-BINANCE.md` | Binance REST + WS as implemented in `learnernoearner-listener/src/exchange/binance.py` + `ws_stream.py`. |
| `02-EXCHANGE-BYBIT.md` | Bybit V5 REST + WS as implemented in `bybit-listener/src/exchanges/bybit.py` + `streaming/bybit_ws.py`. |
| `03-EXCHANGE-MEXC.md` | MEXC Futures V1 REST + WS as implemented in `mexc-listener/src/exchanges/mexc.py` + `streaming/mexc_ws.py`. |
| `04-EXCHANGE-HYPERLIQUID.md` | Hyperliquid REST (info/exchange) + WS as implemented in `hyperliquid-fatty-trader/src/exchanges/hyperliquid.py` + `streaming/hyperliquid_ws.py`. |
| `05-COMPARATIVE-ANALYSIS.md` | Scored comparison: architecture, exchange integration, safety, testing, what's better, gaps. |
| `06-BITGET-DESIGN.md` | Full design + implementation blueprint for `bitget-listener` (Bitget V2 REST/WS, USDⓈ-M perps). |
| `07-BITGET-IMPLEMENTATION-NOTES.md` | Concrete Bitget V2 signing/endpoints/WS payloads + a `bitget_api_probe.py` spec (the equivalent of `mexc_api_probe.py`). |

> **Provenance rule (from the repos themselves):** docs are derived from the *code* in these
> repos. Where the Bitget section relies on external API knowledge (web tools were unavailable
> during this analysis), it is explicitly marked `⚠ UNVERIFIED — confirm with probe` and a
> `bitget_api_probe.py` script is specified to verify before any order code is written, exactly
> like `mexc_api_probe.py` did for MEXC.

## Common pipeline (all 4 share it)
```
Telegram @fattyfatclub (Telethon user client)
   └─ RawSignal (text + optional chart image)
        ├─ Management command? (sl to entry / tp1 booked / full) ─▶ transform (no LLM, ~200ms)
        └─ Trade/Conditional signal ─▶ Gate1 (idemp/cooldown/whitelist)
              └─ LLM brain (or FastPath) ─▶ TradeDecision
                    └─ Gate2 (fee-aware clamps: size/lev/SL-dir/min-notional/daily-loss)
                          └─ OrderService.build_entry ─▶ Exchange adapter (REST place)
                                └─ WS fills/positions ─▶ PositionManager (real liq price, fee-aware)
                                      └─ Ledger + ShadowLedger + Notifier (Web UI WS + Telegram)
```
