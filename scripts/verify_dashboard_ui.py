#!/usr/bin/env python3
"""Live UI verification: boots the real dashboard, seeds real ledger rows, and
drives a REAL headless Chromium at 390px.

Checks the things a screenshot alone cannot prove:
  1. no horizontal scroll at 390px (scrollWidth <= clientWidth),
  2. rendered DOM VALUES match the /api/state JSON (not just "elements exist"),
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
        await page.wait_for_function("document.querySelector('#net').textContent !== '–'",
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
            net: document.querySelector('#net').textContent.trim(),
            fees: document.querySelector('#fees').textContent.trim(),
            closed: document.querySelector('#closed').textContent.trim(),
            nopen: document.querySelector('#nopen').textContent.trim(),
            posRows: [...document.querySelectorAll('#positions tr')].map(
                r => [...r.children].map(c => c.textContent.trim())),
            tradeRows: [...document.querySelectorAll('#trades tr')].map(
                r => [...r.children].map(c => c.textContent.trim())),
            gate: document.querySelector('#gate').textContent,
            skips: document.querySelector('#skips').textContent.trim(),
        })""")

        check("mode badge matches API", dom["mode"] == state["mode"],
              f'dom={dom["mode"]!r} api={state["mode"]!r}')
        expect_net = f'{state["net_usd"]:.4f} USDT'
        check("net P&L value matches API", dom["net"] == expect_net,
              f'dom={dom["net"]!r} api={expect_net!r}')
        expect_fees = f'{state["fees_usd"]:.4f} USDT'
        check("fees value matches API", dom["fees"] == expect_fees,
              f'dom={dom["fees"]!r} api={expect_fees!r}')
        check("closed-trade count matches API",
              dom["closed"] == str(state["closed_trades"]),
              f'dom={dom["closed"]!r} api={state["closed_trades"]}')
        check("open-position count matches API",
              dom["nopen"] == str(len(state["positions"])),
              f'dom={dom["nopen"]!r} api={len(state["positions"])}')
        check("position row count matches API",
              len(dom["posRows"]) == len(state["positions"]),
              f'dom={len(dom["posRows"])} api={len(state["positions"])}')
        check("trade row count matches API",
              len(dom["tradeRows"]) == len(state["trades"]),
              f'dom={len(dom["tradeRows"])} api={len(state["trades"])}')

        # a real liq price must be shown, never 0.00 (BUG B visible in the UI)
        liq_cell = dom["posRows"][0][6] if dom["posRows"] else ""
        check("liquidation price rendered as the real value (not 0.00)",
              liq_cell not in ("0.00", "0", "–") and
              abs(float(liq_cell) - state["positions"][0]["liq"]) < 0.01,
              f'dom={liq_cell!r} api={state["positions"][0]["liq"]}')

        # net per trade must equal gross - fees, as rendered
        row = dom["tradeRows"][0]
        api_row = state["trades"][0]
        # Index by data-label rather than column position, so a column reorder
        # cannot make this assertion silently check the wrong cell.
        labelled = await page.evaluate("""() => {
            const tr=document.querySelector('#trades tr');
            const o={}; tr.querySelectorAll('td').forEach(td=>{
                o[td.dataset.label]=td.textContent.trim();}); return o;}""")
        check("rendered trade net matches fee-aware API net",
              labelled["Net"] == f'{api_row["net_usd"]:.4f}',
              f'dom={labelled["Net"]!r} api={api_row["net_usd"]:.4f}')
        check("rendered fees column is non-zero (BUG A visible)",
              float(labelled["Fees"]) > 0, f'fees_cell={labelled["Fees"]!r}')
        check("closed trade shows its size",
              float(labelled["Size"]) == api_row["qty"],
              f'dom={labelled["Size"]!r} api={api_row["qty"]}')

        check("timestamps rendered in Asia/Jakarta (WIB)",
              labelled["Closed"].endswith("WIB")
              and labelled["Closed"] == api_row["closed_at"],
              f'dom={labelled["Closed"]!r} api={api_row["closed_at"]!r}')

        check("unmet go-live conditions are surfaced in the UI",
              "blocked by" in dom["gate"] and len(state["gate"]["unmet"]) == 5,
              f'unmet={len(state["gate"]["unmet"])}')
        check("skipped signals are shown (all failures logged)",
              "DOGEUSDT" in dom["skips"], dom["skips"][:80])

        shot = "/tmp/bitget-dashboard-390.png"
        await page.screenshot(path=shot, full_page=True)
        print(f"\nscreenshot: {shot}")

        # The check my first pass MISSED: a cell can be visually clipped even when
        # the page itself does not scroll. Compare each cell's content width to its
        # rendered box, and assert every cell sits inside the viewport.
        clipped = await page.evaluate("""() => {
            const bad=[];
            document.querySelectorAll('#positions td, #trades td').forEach(td=>{
                const r=td.getBoundingClientRect();
                const inner=td.querySelector('span');
                const overflowX = td.scrollWidth > td.clientWidth + 1;
                const offscreen = r.right > window.innerWidth + 1 || r.left < -1;
                const innerClip = inner ? inner.scrollWidth > inner.clientWidth + 1 : false;
                if(overflowX||offscreen||innerClip) bad.push({
                    label: td.dataset.label, text: td.textContent.trim(),
                    right: Math.round(r.right), vw: window.innerWidth,
                    scrollW: td.scrollWidth, clientW: td.clientWidth});
            });
            return bad;}""")
        check("no clipped or off-screen table cells at 390px", not clipped,
              clipped[:4] if clipped else "every cell fully visible")

        header_clip = await page.evaluate("""() => {
            const bad=[];
            document.querySelectorAll('h1,h2,.v,.k,footer,#gate li').forEach(e=>{
                const r=e.getBoundingClientRect();
                if(r.right > window.innerWidth + 1) bad.push(e.textContent.trim().slice(0,40));
            });
            return bad;}""")
        check("no text block overflows the viewport", not header_clip,
              header_clip[:4] if header_clip else "none")

        tap = await page.evaluate("""() => {
            const els=[...document.querySelectorAll('a,button,input')];
            return els.filter(e=>{const r=e.getBoundingClientRect();
                return r.width>0 && (r.width<44||r.height<44);}).length;}""")
        check("no undersized tap targets", tap == 0, f"{tap} elements < 44px")
        await browser.close()
        return shot


def main() -> int:
    app, orch = build_app()
    serve(app)

    status, state = http("/api/state")
    check("monitoring /api/state loads with NO auth", status == 200, f"HTTP {status}")
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

    print(f"\n--- /api/state (raw) ---\n{json.dumps(state, indent=2)[:1200]}\n")
    asyncio.run(browser_checks(state))

    print("\n" + "=" * 62)
    if FAILURES:
        print(f"FAILED {len(FAILURES)} check(s): " + ", ".join(FAILURES))
        return 1
    print("ALL UI CHECKS PASSED (real headless Chromium @ 390x844)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
