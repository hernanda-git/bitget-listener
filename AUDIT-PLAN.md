# AUDIT-PLAN.md — bitget-listener build ledger

Status of every MASTER-PROMPT checkpoint. ✅ = done & verified by real execution,
⬜️ = not started, ⛔ = hard-blocked. **This is the source of truth for `git log`
review** — every ✅ below is backed by a test or a live run, not a claim.

---

## P0 — Scaffold, ports, fakes, determinism ✅
- `src/` package with dependency-injected **ports** (`interfaces/ports.py`): `ExchangeAdapter`,
  `SignalSource`, `Notifier`, `PriceStream`, `ConfigStore`, `PositionStore`, `EvalStore`, `LockFile`.
- 6 **fakes** (`exchanges/fake.py`, `streaming/fake_stream.py`, `sources/telegram.py`,
  `config/db.py`, `store/ledger.py`, `services/position.py`) — zero network for the suite.
- Deterministic `FakeClock`, `IdentityGenerator`, no wall-clock/uuid in core.
- ✅ `tests/test_phase0_domain.py` (35 tests) green.

## P1 — Domain model ✅
- `Instrument` (USDT/FUTURES, from_token), `OrderRequest`, `Fill`, `Position`
  (liq_price kept REAL, never 0.00), `Signal` (whitelist-enforced), `Bias`/`Decision`.
- ✅ domain invariants tested (liq never 0.00, fee normalization).

## P2 — Signal parsing ✅
- `parse_message` (3 inline variants + Jaccard selector), `SymbolMap` (Bitget
  multiplier-contract whitepaper: BTC→100x etc., ETH→50x), `FakeSignalSource`.
- ✅ 16 parser/whitelist/bias tests green.

## P3 — Bitget REST + signing + fee-aware ✅
- `BitgetAuth` (VERIFIED signature shape: HMAC-SHA256 of `timestamp+method+path+body`,
  base64'd; `X-CHANGE-*` headers), `BitgetAdapter` parses the **VERIFIED** v2 contract
  shape (`pricePlace`/`volumePlace` → steps, `sizeMultiplier`, `minTradeUSDT`).
- Fees are REAL (BUG A): Bitget reports negative → normalized to cost.
- ✅ 11 REST/parse/round tests green (against the live /contracts shape).

## P4 — Bitget WebSocket ✅
- `BitgetWS` public ticker + **private** order/fill/position/wallet channels
  (VERIFIED: `instType`/`channel`/`instId` object args, `ping`/`pong` heartbeat,
  auto-reconnect w/ backoff). Frames → typed callbacks; real-time **stop monitor**
  (SL/TP/liq hits → close).
- ✅ 29 WS/frame tests green (fake connector, no network).

## P5 — Order service + fills ✅
- `OrderService.open_from_signal`, `close_position` (partial + full), `cancel_pending`
  (KEEPS protective reduce-only orders — the margin-lock fix). Fee-aware ledger writes.
- ✅ order/fill tests green.

## P6 — Position manager + real-time stops ✅
- `PositionManager` (dict store) with management commands copied from siblings:
  `sl to entry`, `tp1 booked`, `breakeven sl`, `scale in/out`, `close full/partial`.
- ttl/SL/TP enforcement; protective orders preserved.
- ✅ position/stop tests green.

## P7 — ShadowLedger reflexivity ✅
- Grafted from mexc-listener (richest reflexivity). Per-pair win-rate → **auto flag +
  downsize** weak pairs (2 deliberate conservatism rules copied verbatim).
- ✅ reflexivity tests green.

## P8 — Orchestrator, dead-man switch, go-live gate, config ✅
- `Orchestrator` (every inbound msg → management fast-path first, **no LLM**; else
  rules+gates), `DeadManSwitch` (heartbeat → halt on stale feed), `GoLiveGate`
  (paper until 5 conditions + single-use TTL token), `ConfigStore` (persisted paper flag).
- ✅ orchestrator/notifier/go-live/config tests green (the persisted paper flag means
  a restart can't silently resurrect live mode).

## P9 — Boot, persistence, wiring ✅
- `main.py` (paper/live/--probe/--web), `.env`/`ConfigStore` wiring, file-backed ledger.
- **2 real bugs found only by a live run, now fixed + regression-pinned:**
  1. Demo product lists `SBTCSUSDT`, not `BTCUSDT` → added `venue_symbol`/`canonical_symbol`
     translation on REST **and** WS (VERIFIED against `/contracts`).
  2. Paper mode called a signed endpoint (`positions.sync()`) → NO_CREDS; now tracks
     locally with zero credentials.
- ✅ `scripts/smoke_paper_run.py` runs the full entry→close loop against the REAL demo
  product with **no API keys** and exits 0.

## P10 — Dashboard (operator UX) ✅
- `web/dashboard.py`: **monitoring loads OPEN** (no login gate on page or `/api/state`),
  only state-changing endpoints require `DASH_PASSWORD`, every control logged at ERROR,
  timestamps **Asia/Jakarta (UTC+7)** server-side.
- ✅ `scripts/verify_dashboard_ui.py` drives a **REAL headless Chromium @ 390x844**,
  diffing rendered DOM VALUES vs `/api/state` (not element counts). 24/24 checks pass,
  including no-horizontal-scroll, no-clipped-cells, real liq price, fee-aware net, WIB
  timestamps. Vision review drove a fix (stacked cards) + 2 new checks.

---

## Definition of Done — current state
| Criterion | State |
|---|---|
| No hardcoded-secrets leak | ✅ (`settings.py` redacts; `.env` is git-ignored) |
| Adapters isolated behind ports | ✅ |
| Paper mode safe (no real orders) | ✅ **verified live, no creds required** |
| Go-live gate hard (5 cond + token) | ✅ enforced; persisted paper flag |
| Audit ledger maintained | ✅ this file |
| Reproducible build | ✅ venv + `pip freeze` in `requirements.txt` |
| Tests green | ✅ **310 tests + 24 UI checks + live smoke** |
| Self-healing lock-file discipline | ✅ `src/services/audit_lock.py` (`.audit.lock` = `{phase,utc_ts,pid,host}`) + `scripts/self_heal_locks.py`; wired into `cmd_run`/`--web` in `main.py`; 4 tests pin it |
| **Real keys / KYC** | ⛔ **HARD BLOCKER — not attempted, would fabricate** |
| **Live Telethon session** | ⛔ **HARD BLOCKER — would need a live @fattyfatclub login** |

**Bot is PAPER-READY behind a hardened go-live gate.** It cannot go live until a human
supplies real Bitget API keys and runs `bitget_api_probe.py` GREEN, satisfies the
remaining gate conditions, and redeems a `--go-live` token. I have **not** fabricated
any credentials or declared go-live.
