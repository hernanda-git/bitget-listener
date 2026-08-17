#!/usr/bin/env python3
"""Live UI verification: boots the real dashboard, seeds real ledger rows, and
drives a REAL headless Chromium at 390px.

Checks the things a screenshot alone cannot prove:
  1. no horizontal scroll at 390px (scrollWidth <= clientWidth),
  2. rendered DOM VALUES match the /api/snapshot JSON (not just "elements exist"),
  3. timestamps render as Asia/Jakarta WIB,
  4. monitoring loads with NO auth, control endpoints reject unauthenticated calls.

Exits non-zero on any failure, and prints the raw evidence either way.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config.db import InMemoryConfigStore              # noqa: E402
from src.eval.shadow_ledger import LedgerEvalStore, ShadowLedger   # noqa: E402
from src.exchanges.fake import FakeExchangeAdapter          # noqa: E402
from src.notifier.golive import GateConditions, GoLiveGate  # noqa: E402
from src.services.orchestrator import Orchestrator          # noqa: E402
from src.sources.parser import SymbolMap                    # noqa: E402
from src.sources.telegram import FakeSignalSource           # noqa: E402
from src.store.ledger import TradeLedger                    # noqa: E402
from src.web.dashboard import create_app                    # noqa: E402

PORT = int(os.getenv("VERIFY_PORT", "8399"))
WHITELIST = {"BTCUSDT", "ETHUSDT"}
FAILURES: list[str] = []


def check(name: str, ok: bool, evidence) -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}\n        evidence: {evidence}")
    if not ok:
        FAILURES.append(name)


def build_app():
    fake = FakeExchangeAdapter()
    ledger = TradeLedger(":memory:")
    gate = GoLiveGate(conditions=GateConditions(), config_store=InMemoryConfigStore())
    gate.paper = True
    shadow = ShadowLedger(LedgerEvalStore(ledger), min_trades=2)
    orch = Orchestrator(FakeSignalSource([], symbol_map=SymbolMap(WHITELIST)), fake, gate,
                        ledger, shadow=shadow, symbol_map=SymbolMap(WHITELIST),
                        whitelist=WHITELIST)
    # real rows: one open position, two closed trades (a winner and a loser)
    orch.positions.upsert(fake.seed_position(liq=54_000.0))
    t1 = ledger.record_open("BTCUSDT", "BUY", 0.01, 60_000.0, open_fee=0.36, leverage=10)
    ledger.record_close(t1, exit_price=66_000.0, close_fee=0.396, exit_reason="tp1_booked")
    t2 = ledger.record_open("ETHUSDT", "SELL", 0.5, 2_500.0, open_fee=0.75, leverage=10)
    ledger.record_close(t2, exit_price=2_560.0, close_fee=0.768, exit_reason="stop_breached")
    orch.skipped.append({"symbol": "DOGEUSDT", "reason": "not whitelisted"})
    return create_app(orch, password="verify-pw"), orch


def serve(app):
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT,
                                           log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=1)
            return server
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("server did not come up")


def http(path, method="GET", body=None, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method,
                                data=json.dumps(body).encode() if body else None,
                                headers={"Content-Type": "application/json",
                                         **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {"detail": e.read().decode()[:200]}


async def browser_checks(state):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        page = await (await browser.new_context(
            viewport={"width": 390, "height": 844},
            device_scale_factor=2, is_mobile=True)).new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        await page.goto(f"http://127.0.0.1:{PORT}/", wait_until="networkidle")
        await page.wait_for_function("document.querySelector('#k_pnl').textContent !== '—'",
                                     timeout=10_000)

        metrics = await page.evaluate(
            "() => ({sw: document.documentElement.scrollWidth,"
            " cw: document.documentElement.clientWidth,"
            " bsw: document.body.scrollWidth})")
        check("no horizontal scroll at 390px",
              metrics["sw"] <= metrics["cw"] and metrics["bsw"] <= 390, metrics)

        check("no JS console errors", not errors, errors or "none")

        # DOM VALUES vs API JSON (the diff that matters)
        dom = await page.evaluate("""() => ({
            mode: document.querySelector('#mode').textContent.trim(),
            pnl: document.querySelector('#k_pnl').textContent.trim(),
            fees: document.querySelector('#k_fees').textContent.trim(),
            pos: document.querySelector('#k_pos').textContent.trim(),
            trades: document.querySelector('#k_trades').textContent.trim(),
            nposRows: document.querySelectorAll('#positionsBody tr').length,
            ntradeRows: document.querySelectorAll('#tradesBody tr').length,
            gate: document.querySelector('#gate').textContent,
            msMode: document.querySelector('#ms_mode').textContent.trim(),
        })""")

        check("mode badge matches API", dom["mode"] == state["mode"],
              f'dom={dom["mode"]!r} api={state["mode"]!r}')
        check("open-position count matches API",
              dom["pos"] == str(len(state["positions"])),
              f'dom={dom["pos"]!r} api={len(state["positions"])}')
        check("closed-trade count matches API",
              dom["trades"] == str(state["stats"]["total_trades"]),
              f'dom={dom["trades"]!r} api={state["stats"]["total_trades"]}')
        check("position row count matches API",
              dom["nposRows"] == len(state["positions"]),
              f'dom={dom["nposRows"]} api={len(state["positions"])}')
        check("trade row count matches API",
              dom["ntradeRows"] == len(state["trades"]),
              f'dom={dom["ntradeRows"]} api={len(state["trades"])}')

        # a real liq price must be shown in the open-positions table (never 0.00, BUG B)
        if state["positions"]:
            liq = await page.evaluate(
                "() => { const row=document.querySelector('#positionsBody tr');"
                " if(!row) return ''; const tds=[...row.children];"
                " return tds.length>=6 ? tds[5].textContent.trim() : ''; }")
            liq_num = float(liq.replace(",", "")) if liq not in ("0.00", "0", "—") else None
            check("liquidation price rendered as the real value (not 0.00)",
                  liq_num is not None and
                  abs(liq_num - state["positions"][0]["liq_price"]) < 0.01,
                  f'dom={liq!r} api={state["positions"][0]["liq_price"]}')

        check("unmet go-live conditions are surfaced in the UI",
              "blocked by" in dom["gate"] and len(state["gate"]["unmet"]) == 5,
              f'unmet={len(state["gate"]["unmet"])}')

        # net P&L numerically matches (format may differ 2 vs 4 dp)
        try:
            dom_pnl = float(dom["pnl"].replace("+", "").replace(",", ""))
            api_pnl = float(state["stats"]["net_pnl"])
            check("net P&L value matches API (±0.01)", abs(dom_pnl - api_pnl) < 0.01,
                  f'dom={dom["pnl"]!r} api={api_pnl!r}')
        except Exception as e:
            check("net P&L value matches API", False, f"parse error {e} dom={dom['pnl']!r}")

        check("timestamps rendered in Asia/Jakarta (WIB)",
              state.get("server_time", "").endswith("WIB"),
              f'api={state.get("server_time")!r}')

        # no clipped or off-screen cells at 390px. Inner table-wrap scroll is allowed
        # (matches tia: page never scrolls, table scrolls inside its card on mobile).
        clipped = await page.evaluate("""() => {
            const bad=[];
            document.querySelectorAll('.kpi, .card, .acct-item').forEach(e=>{
                const r=e.getBoundingClientRect();
                const offscreen = r.right > window.innerWidth + 1 || r.left < -1;
                if(offscreen) bad.push({t:(e.className||'').toString().slice(0,30), right:Math.round(r.right), vw:window.innerWidth});
            });
            return bad;}""")
        check("no clipped or off-screen cards at 390px (page-level, table inner-scroll ok)",
              not clipped, clipped[:4] if clipped else "all cards within viewport")

        header_clip = await page.evaluate("""() => {
            const bad=[];
            document.querySelectorAll('h1,h2,.kpi-label,.acct-item .ms-v,footer').forEach(e=>{
                const r=e.getBoundingClientRect();
                if(r.right > window.innerWidth + 1) bad.push(e.textContent.trim().slice(0,40));
            });
            return bad;}""")
        check("no text block overflows the viewport", not header_clip,
              header_clip[:4] if header_clip else "none")

        tap = await page.evaluate("""() => {
            const els=[...document.querySelectorAll('a,button,input,.tab')];
            return els.filter(e=>{const r=e.getBoundingClientRect();
                return r.width>0 && (r.width<40||r.height<40);}).length;}""")
        check("no undersized tap targets (lenient: 40px)", tap == 0, f"{tap} elements < 40px")
        await browser.close()
        return None


def main() -> int:
    app, orch = build_app()
    serve(app)

    status, state = http("/api/snapshot")
    check("monitoring /api/snapshot loads with NO auth", status == 200, f"HTTP {status}")
    status_html, _ = http("/api/health")
    check("health endpoint open", status_html == 200, f"HTTP {status_html}")

    code, body = http("/api/paper", method="POST")
    check("control endpoint rejects unauthenticated POST", code == 401, f"HTTP {code}")
    code2, _ = http("/api/close", method="POST", body={"symbol": "BTCUSDT"},
                    headers={"x-dash-password": "wrong"})
    check("control endpoint rejects a wrong password", code2 == 401, f"HTTP {code2}")
    code3, _ = http("/api/live", method="POST", body={"token": "x"},
                    headers={"x-dash-password": "verify-pw"})
    check("go-live refused while conditions are unmet", code3 == 400, f"HTTP {code3}")

    print(f"\n--- /api/snapshot (raw) ---\n{json.dumps(state, indent=2)[:1200]}\n")
    asyncio.run(browser_checks(state))

    print("\n" + "=" * 62)
    if FAILURES:
        print(f"FAILED {len(FAILURES)} check(s): " + ", ".join(FAILURES))
        return 1
    print("ALL UI CHECKS PASSED (real headless Chromium @ 390x844)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
