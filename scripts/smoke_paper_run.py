#!/usr/bin/env python3
"""smoke_paper_run.py — NO-CREDS paper-mode end-to-end smoke against the REAL
Bitget demo product.

This is the test that originally broke the build (it is not covered by the
fakes-only suite): it proves a paper run works WITHOUT any Bitget API keys, by
translating ``BTCUSDT`` -> ``SBTCSUSDT`` and tracking the position locally.

Usage:
    python scripts/smoke_paper_run.py            # live demo product, temp ledger
    LEDGER_PATH=/tmp/paper.db python scripts/smoke_paper_run.py
Exits 1 on any assertion failure.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.main import build_config, build_runtime   # noqa: E402
from src.sources.parser import parse_message        # noqa: E402


async def main() -> int:
    ledger = os.getenv("LEDGER_PATH", tempfile.mktemp(suffix=".db"))
    os.environ.update(
        PAPER="1",
        LEDGER_PATH=ledger,
        BITGET_PRODUCT_TYPE="SUSDT-FUTURES",
        SYMBOL_WHITELIST="BTCUSDT,ETHUSDT",
        LOG_LEVEL="WARNING",
    )
    cfg, store = build_config()
    orch = await build_runtime(cfg, store, use_fake_source=True)

    assert orch.gate.paper is True, "must boot in paper"
    assert orch.adapter.is_demo is True, "smoke targets the demo product"

    # 1) entry — must open on the demo product with no credentials
    r = await orch.handle(parse_message(
        "BTCUSDT LONG entry 60000 sl 57000 tp 66000 lev 10", message_id=1))
    assert r["status"] == "PAPER", r
    op = orch.ledger.open_trades()
    assert len(op) == 1 and op[0]["open_fee"] > 0, op
    assert orch.positions.get("BTCUSDT") is not None, "position tracked locally"

    # 2) management + full close, fee-aware net
    await orch.handle(parse_message("sl to entry"))
    await orch.handle(parse_message("close full"))
    closed = orch.ledger.closed_trades()
    assert len(closed) == 1, closed
    row = closed[0]
    expected_net = row["gross_usd"] - (row["open_fee"] + row["close_fee"])
    assert abs(row["net_usd"] - expected_net) < 1e-6, (row["net_usd"], expected_net)

    # 3) go-live must stay blocked (conditions unmet). Clear any prior probe/test
    # status stamp so this asserts the genuine baseline, not whatever a previous
    # run may have written into .golive_status.json.
    from src.notifier.golive_status import DEFAULT_PATH
    try:
        import os as _os
        if _os.path.exists(DEFAULT_PATH):
            _os.remove(DEFAULT_PATH)
    except Exception:
        pass
    gate = orch.gate.status()
    assert gate["paper"] is True and gate["conditions_met"] is False
    # The blocking conditions (b), (d1), (d2) must remain unmet in a no-creds run.
    unmet = gate["unmet"]
    assert any("(b) fee-aware expectancy" in u for u in unmet), unmet
    assert any("(d1) a fill with a non-zero REAL fee" in u for u in unmet), unmet
    assert any("(d2) a REAL liquidation price" in u for u in unmet), unmet

    # 4) a fresh ledger can read the persisted rows (db thread/process safe)
    from src.store.ledger import TradeLedger
    assert len(TradeLedger(ledger).closed_trades()) == 1

    await orch.stop()
    print(f"smoke OK — paper entry+close on demo product, fee-aware net "
          f"{row['net_usd']:.4f} USDT, go-live still blocked")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
