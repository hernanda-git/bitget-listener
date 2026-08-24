"""FastAPI operator dashboard for bitget-listener.

The visual design is a faithful 1:1 copy of the learnernoearner-listener (Binance)
dashboard at tia.warga-digital.com: same indigo palette, light/dark auto, topbar,
segmented tabs (Overview / Activity / Control), KPI grid, live-market grid,
positions/trades tables, activity timeline, control panel, footer. The markup and
CSS are identical; only the branding and the data-binding differ (bitget's
/api/state is mapped into the same DOM ids tia uses).

Standing UX rules for this operator:
- monitoring loads OPEN — no login gate on the read-only page or its JSON;
- every STATE-CHANGING action (go-live redeem, close, paper toggle) requires the
  ``DASH_PASSWORD`` header and is logged server-side at ERROR;
- all timestamps render in Asia/Jakarta (UTC+7), formatted server-side;
- mobile-first: no horizontal scroll at 390px (verified with a headless browser).
"""

from __future__ import annotations

import logging
import os
import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.encoders import jsonable_encoder

log = logging.getLogger(__name__)

JAKARTA = timezone(timedelta(hours=7), "WIB")


def jakarta(ms: int | None) -> str:
    """Format an epoch-ms into Asia/Jakarta (UTC+7, no DST)."""
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms / 1000, JAKARTA).strftime("%Y-%m-%d %H:%M:%S WIB")


def create_app(orchestrator, password: str | None = None) -> FastAPI:
    app = FastAPI(title="bitget-listener", docs_url=None, redoc_url=None)
    secret = password if password is not None else os.getenv("DASH_PASSWORD", "")

    def require_auth(supplied: str | None, action: str) -> None:
        """Gate state-changing actions only. Always logs the attempt at ERROR."""
        if not secret:
            log.error("control action %r REFUSED: DASH_PASSWORD is not configured", action)
            raise HTTPException(status_code=503, detail="control plane disabled (no password set)")
        if supplied != secret:
            log.error("control action %r REFUSED: bad password", action)
            raise HTTPException(status_code=401, detail="unauthorized")
        log.error("control action %r AUTHORIZED", action)   # ERROR by policy: always logged

    # ------------------------------------------------------------ read-only (open)
    @app.get("/api/state")
    async def state():
        snap = orchestrator.snapshot()
        snap["server_time"] = jakarta(int(datetime.now(timezone.utc).timestamp() * 1000))
        snap["trades"] = [
            {**row,
             "opened_at": jakarta(row.get("opened_at_ms")),
             "closed_at": jakarta(row.get("closed_at_ms"))}
            for row in orchestrator.ledger.closed_trades(50)
        ]
        snap["open_rows"] = [
            {**row, "opened_at": jakarta(row.get("opened_at_ms"))}
            for row in orchestrator.ledger.open_trades()
        ]
        return JSONResponse(snap)

    @app.get("/api/health")
    async def health():
        return {"ok": True, "mode": "PAPER" if orchestrator.gate.paper else "LIVE",
                "deadman_halted": bool(orchestrator.deadman.halted)
                if orchestrator.deadman else False}

    @app.get("/api/snapshot")
    async def snapshot():
        """tia-compatible snapshot shape so the copied UI JS binds unchanged.

        bitget's domain has no public market ticker or full trade stats, so the
        fields it does not produce are left as `—`/empty (tia renders the same
        placeholders when idle).
        """
        snap = orchestrator.snapshot()
        paper = bool(snap.get("gate", {}).get("paper", True))
        positions = [
            {"symbol": p["symbol"], "direction": str(p.get("side", "")).upper(),
             "size": p.get("size"), "entry": p.get("entry"),
             "sl": p.get("sl"), "tp": p.get("tp"),
             "mark": p.get("entry"), "liq_price": p.get("liq"),
             "pnl": p.get("upnl")}
            for p in snap.get("positions", [])
        ]
        trades = [
            {"symbol": t.get("symbol"), "pnl": t.get("net_usd"),
             "close_ts": int((t.get("closed_at_ms") or 0) / 1000)}
            for t in (snap.get("trades") or [])
        ]
        if not trades:
            # /api/snapshot derives trades from the ledger directly (snapshot() omits it)
            trades = [
                {"symbol": row.get("symbol"), "pnl": row.get("net_usd"),
                 "close_ts": int((row.get("closed_at_ms") or 0) / 1000)}
                for row in orchestrator.ledger.closed_trades(50)
            ]
        stats = {
            "net_pnl": snap.get("net_usd"),
            "open_positions": len(snap.get("positions", [])),
            "total_trades": snap.get("closed_trades", 0),
            "win_rate": None, "profit_factor": None, "expectancy": None,
            "max_drawdown": None, "wins": 0, "losses": 0,
        }
        return JSONResponse({
            "mode": snap.get("mode"), "paper": paper,
            "market": {},            # bitget paper/demo has no public ticker feed here
            "balance": {"equity": snap.get("net_usd"), "available": snap.get("net_usd")},
            "stats": stats,
            "positions": positions,
            "trades": trades,
            "activities": [],         # bitget logs control actions server-side at ERROR
            "gate": snap.get("gate"),
            "skipped": snap.get("skipped", []),
            "equity_history": snap.get("equity_history", []),
            "server_time": jakarta(int(datetime.now(timezone.utc).timestamp() * 1000)),
        })

    @app.get("/api/trace/{trace_id:path}")
    async def trace(trace_id: str, x_dash_password: str | None = Header(None)):
        """Read-only ordered cross-stage trace for operator/audit tooling."""
        require_auth(x_dash_password, f"trace {trace_id}")
        getter = getattr(orchestrator.ledger, "get_trace", None)
        if not callable(getter):
            raise HTTPException(status_code=503, detail="trace store unavailable")
        stages = await asyncio.to_thread(getter, trace_id)
        return JSONResponse({"trace_id": trace_id, "stages": stages})

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return HTMLResponse(INDEX_HTML)

    # -------------------------------------------------------- control (gated)
    @app.post("/api/close")
    async def close(payload: dict = Body(...), x_dash_password: str | None = Header(None)):
        require_auth(x_dash_password, f"close {payload.get('symbol')}")
        res = await orchestrator.positions.full_close(
            str(payload.get("symbol", "")).upper(), reason="dashboard")
        return JSONResponse(jsonable_encoder(res))

    @app.post("/api/paper")
    async def to_paper(x_dash_password: str | None = Header(None)):
        require_auth(x_dash_password, "to_paper")
        return {"message": orchestrator.gate.to_paper("dashboard")}

    @app.post("/api/mint_go_live")
    async def mint_go_live(x_dash_password: str | None = Header(None)):
        """Mint a token into the RUNNING gate (separate from the --go-live CLI,
        which mints in a throwaway process the live gate never sees)."""
        require_auth(x_dash_password, "mint_go_live")
        ok, msg = orchestrator.gate.mint_token()
        if not ok:
            log.error("go-live mint REFUSED via dashboard: %s", msg)
            raise HTTPException(status_code=400, detail=msg)
        return {"token": msg, "warning": "single-use, 5-min TTL; redeem via /api/live"}

    @app.post("/api/live")
    async def go_live(payload: dict = Body(...), x_dash_password: str | None = Header(None)):
        require_auth(x_dash_password, "go_live")
        ok, msg = orchestrator.gate.redeem(str(payload.get("token", "")))
        if not ok:
            log.error("go-live redeem REFUSED via dashboard: %s", msg)
            raise HTTPException(status_code=400, detail=msg)
        return {"message": msg}

    return app


INDEX_HTML = r"""<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<link rel="icon" href="data:,">
<title>bitget-listener · fattyfatclub → Bitget</title>
<style>
:root{
  --accent:#6366f1; --accent-600:#4f46e5; --accent-700:#4338ca;
  --accent-soft:#eef2ff; --accent-ring:rgba(99,102,241,.35);
  --pos:#16a34a; --pos-bg:#ecfdf5; --pos-line:#bbf7d0;
  --neg:#dc2626; --neg-bg:#fef2f2; --neg-line:#fecaca;
  --warn:#d97706; --warn-bg:#fffbeb; --warn-line:#fde68a;
  --bg:#f5f7fb; --bg-2:#eef2f8; --surface:#ffffff; --surface-2:#f7f9fc;
  --ink:#0e1726; --ink-soft:#475569; --mut:#64748b; --mut-2:#94a3b8; --line:#e6ecf3;
  --shadow:0 1px 2px rgba(15,23,42,.04),0 12px 30px rgba(15,23,42,.07);
  --shadow-sm:0 1px 2px rgba(15,23,42,.05),0 4px 12px rgba(15,23,42,.05);
  --shadow-pop:0 20px 50px rgba(15,23,42,.16);
  --r-xl:22px; --r-lg:18px; --r:14px; --r-sm:10px; --r-xs:7px;
  --s1:4px; --s2:8px; --s3:12px; --s4:16px; --s5:20px; --s6:24px; --s7:32px;
  --ff:'Inter',system-ui,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
  --mono:ui-monospace,SFMono-Regular,'JetBrains Mono',Menlo,Consolas,monospace;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#0b1120; --bg-2:#0e1626; --surface:#121b2e; --surface-2:#16203a;
    --ink:#e8edf6; --ink-soft:#aab6c8; --mut:#8190a6; --mut-2:#5b6b85; --line:#243149;
    --accent-soft:#1c2546; --accent-ring:rgba(129,140,248,.4);
    --pos-bg:#0f2a1e; --pos-line:#1c5238; --neg-bg:#2a1416; --neg-line:#5b2226;
    --warn-bg:#2a2010; --warn-line:#5c4318;
    --shadow:0 1px 2px rgba(0,0,0,.3),0 14px 34px rgba(0,0,0,.45);
    --shadow-sm:0 1px 2px rgba(0,0,0,.3),0 4px 12px rgba(0,0,0,.3);
    --shadow-pop:0 20px 50px rgba(0,0,0,.6);
  }
}
*{box-sizing:border-box}html,body{margin:0;padding:0}
body{font-family:var(--ff);background:var(--bg);color:var(--ink);font-size:14px;line-height:1.5;
  min-height:100vh;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;
  font-feature-settings:'cv11','ss01';letter-spacing:-.005em;overflow-x:hidden}
h1,h2,h3,p{margin:0}
a{color:var(--accent-600)}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.pos{color:var(--pos)!important}.neg{color:var(--neg)!important}
.bg{position:fixed;inset:0;z-index:-1;overflow:hidden;pointer-events:none;
  background:radial-gradient(1200px 600px at 12% -8%,rgba(99,102,241,.10),transparent 60%),
            radial-gradient(900px 500px at 100% 0%,rgba(236,72,153,.06),transparent 55%),
            linear-gradient(180deg,var(--bg) 0%,var(--bg-2) 100%)}
.topbar{position:sticky;top:0;z-index:30;display:flex;align-items:center;gap:var(--s4);flex-wrap:wrap;
  padding:var(--s3) var(--s5);background:color-mix(in srgb,var(--surface) 82%,transparent);
  backdrop-filter:blur(16px) saturate(140%);border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:var(--s3);min-width:0}
.brand-mark{width:40px;height:40px;border-radius:12px;flex:none;display:grid;place-items:center;
  background:linear-gradient(135deg,var(--accent) 0%,#8b5cf6 100%);color:#fff;
  box-shadow:0 6px 16px var(--accent-ring)}
.brand-mark svg{width:22px;height:22px}
.brand-text{min-width:0}.brand-text h1{font-size:1.02rem;font-weight:800;letter-spacing:-.02em;color:var(--ink);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.brand-text p{font-size:.68rem;margin-top:2px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.status-cluster{display:flex;align-items:center;gap:var(--s2);margin-left:auto;flex-wrap:wrap}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:.7rem;font-weight:700;
  padding:5px 10px;border-radius:999px;border:1px solid var(--line);background:var(--surface);color:var(--ink-soft);letter-spacing:.02em;white-space:nowrap}
.pill .dot{width:7px;height:7px;border-radius:50%;background:var(--mut-2)}
.pill.paper{background:var(--warn-bg);border-color:var(--warn-line);color:var(--warn)}
.pill.live{background:var(--pos-bg);border-color:var(--pos-line);color:var(--pos)}
.pill.live .dot{background:var(--pos);box-shadow:0 0 0 3px var(--pos-line)}
.pill.accent{background:var(--accent-soft);border-color:var(--accent-ring);color:var(--accent-600)}
.conn{display:inline-flex;align-items:center;gap:6px;font-size:.72rem;color:var(--mut);font-weight:600}
.conn .dot{width:8px;height:8px;border-radius:50%;background:var(--warn);transition:.2s}
.conn .dot.live{background:var(--pos);box-shadow:0 0 0 4px var(--pos-line)}
.tabs{display:flex;gap:var(--s2);max-width:1180px;margin:0 auto;padding:var(--s5) var(--s5) 0}
.tab{flex:1;padding:10px var(--s3);border:1px solid var(--line);background:var(--surface);color:var(--mut);
  border-radius:var(--r);font-weight:700;font-size:.82rem;cursor:pointer;transition:.18s;letter-spacing:-.01em;min-height:44px}
.tab:hover{color:var(--ink);border-color:var(--accent-ring);transform:translateY(-1px)}
.tab.active{background:var(--accent);color:#fff;border-color:var(--accent);box-shadow:0 8px 20px var(--accent-ring)}
.tab-pane{display:none;animation:fade .22s ease}
.tab-pane.active{display:block}
@keyframes fade{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.shell{max-width:1180px;margin:0 auto;padding:var(--s5) var(--s5) var(--s7)}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-lg);box-shadow:var(--shadow);overflow:hidden}
.card-head{display:flex;align-items:center;gap:var(--s2);padding:var(--s4) var(--s5) var(--s2)}
.card-head h2{font-size:.72rem;font-weight:800;color:var(--mut);text-transform:uppercase;letter-spacing:.08em}
.card-head .hint{margin-left:auto;font-size:.7rem;color:var(--mut-2);font-weight:600}
.card-body{padding:var(--s4) var(--s5) var(--s5)}
.section{margin-top:var(--s4)}
#tab-overview > .card:first-child{margin-top:var(--s4);margin-bottom:var(--s5)}
.kpi-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:var(--s4)}
.kpi:last-child{grid-column:span 2}
.kpi{display:flex;gap:var(--s3);align-items:flex-start;padding:var(--s4) var(--s5);position:relative;overflow:hidden;
  background:var(--surface);border:1px solid var(--line);border-radius:var(--r-lg);box-shadow:var(--shadow-sm);transition:.2s}
.kpi:hover{transform:translateY(-3px);box-shadow:var(--shadow);border-color:var(--accent-ring)}
.kpi-icon{width:42px;height:42px;border-radius:12px;flex:none;display:grid;place-items:center;
  background:var(--accent-soft);color:var(--accent-600)}
.kpi-icon svg{width:20px;height:20px}
.kpi-icon.green{background:var(--pos-bg);color:var(--pos)}
.kpi-icon.red{background:var(--neg-bg);color:var(--neg)}
.kpi-icon.amber{background:var(--warn-bg);color:var(--warn)}
.kpi-body{min-width:0;position:relative;z-index:1;display:flex;flex-direction:column;gap:3px}
.kpi-label{font-size:.66rem;font-weight:700;color:var(--mut);text-transform:uppercase;letter-spacing:.06em}
.kpi-value{font-size:1.55rem;font-weight:800;line-height:1.1;font-variant-numeric:tabular-nums;color:var(--ink);white-space:nowrap;margin:0}
.kpi-sub{font-size:.7rem;color:var(--mut-2);font-weight:500;margin:0;line-height:1.3}
.kpi-spark{margin-top:7px;height:26px}
.market-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:var(--s4)}
.mkt{padding:var(--s4);border-radius:var(--r);background:var(--surface-2);border:1px solid var(--line);transition:.2s}
.mkt:hover{border-color:var(--accent-ring);transform:translateY(-2px)}
.mkt-top{display:flex;align-items:center;gap:var(--s2)}
.mkt-sym{font-size:.82rem;font-weight:800;color:var(--ink);letter-spacing:.01em}
.mkt-chg{margin-left:auto;font-size:.7rem;font-weight:800;padding:3px 8px;border-radius:999px;font-variant-numeric:tabular-nums}
.mkt-chg.pos{background:var(--pos-bg);color:var(--pos)}.mkt-chg.neg{background:var(--neg-bg);color:var(--neg)}
.mkt-price{font-size:1.4rem;font-weight:800;margin-top:var(--s2);font-variant-numeric:tabular-nums;color:var(--ink);transition:color .15s}
.mkt-price.flash-up{animation:mktFlashUp .7s ease-out}
.mkt-price.flash-down{animation:mktFlashDown .7s ease-out}
@keyframes mktFlashUp{0%{color:var(--pos)}100%{color:var(--ink)}}
@keyframes mktFlashDown{0%{color:var(--neg)}100%{color:var(--ink)}}
.mkt-spark{margin-top:var(--s3);height:34px}
.mkt-spark .spark{display:block}
.mkt-flat{height:34px;border-radius:var(--r-xs);background:linear-gradient(90deg,var(--line),transparent,var(--line));opacity:.4}
.kpi-spark{margin-top:6px;height:26px}
.kpi-spark .spark{display:block}
.chart-tabs{display:flex;gap:var(--s2);margin-bottom:var(--s3);flex-wrap:wrap}
.chip{padding:6px 12px;border-radius:999px;border:1px solid var(--line);background:var(--surface-2);
  color:var(--ink-soft);font-size:.72rem;font-weight:700;cursor:pointer;transition:.15s;font-family:var(--ff);min-height:40px}
.chip:hover{border-color:var(--accent-ring);color:var(--ink)}
.chip.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.chart-wrap{width:100%;height:220px;background:var(--surface-2);border-radius:var(--r-sm);overflow:hidden}
.chart{width:100%;height:100%;display:block}
.chart .line{fill:none;stroke:var(--accent);stroke-width:2;vector-effect:non-scaling-stroke}
.chart .area{fill:url(#walletFill);opacity:.5}
.chart .axis{stroke:var(--line);stroke-width:1;vector-effect:non-scaling-stroke}
.chart .lbl{fill:var(--mut-2);font-size:11px;font-family:var(--mono)}
.kpi-value.muted{color:var(--mut-2);font-weight:700}
.grid2x2{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:var(--s4);margin-top:var(--s4)}
.grid2col{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,1fr) minmax(0,1fr) minmax(0,1fr);gap:var(--s4);margin-top:var(--s4)}
.grid2col>.card:first-child{grid-column:span 2}
.table-wrap{overflow:auto;max-height:360px;border-radius:var(--r-sm)}
table{width:100%;border-collapse:collapse;font-size:.78rem}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--mut);font-weight:700;font-size:.64rem;text-transform:uppercase;letter-spacing:.06em;
  background:var(--surface-2);position:sticky;top:0;z-index:1}
tbody tr:last-child td{border-bottom:0}tbody tr:hover{background:var(--surface-2)}
.sym{font-weight:700;color:var(--ink)}
.side{font-size:.64rem;font-weight:800;padding:3px 8px;border-radius:6px;text-transform:uppercase;letter-spacing:.02em}
.side.long,.side.buy{background:var(--pos-bg);color:var(--pos)}.side.short,.side.sell{background:var(--neg-bg);color:var(--neg)}
.empty{color:var(--mut-2);font-size:.8rem;padding:var(--s3) 0;display:flex;align-items:center;gap:var(--s2)}
.empty svg{width:16px;height:16px;opacity:.7;flex:none}
.empty .e-sub{color:var(--mut-2);font-size:.72rem;font-weight:500}
.account-card{position:relative;overflow:hidden}
.account-card::before{content:"";position:absolute;top:0;left:0;right:0;height:3px;
  background:linear-gradient(90deg,var(--accent),var(--accent-600));opacity:.92}
.acct-status{margin-left:auto;display:inline-flex;align-items:center;gap:7px;
  font-size:.7rem;font-weight:700;color:var(--mut);background:var(--surface-2);
  border:1px solid var(--line);padding:5px 12px;border-radius:999px;letter-spacing:.01em}
.acct-status .dot{width:8px;height:8px;border-radius:50%;background:var(--pos);
  box-shadow:0 0 0 3px var(--pos-bg);animation:pulse 1.9s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.4;transform:scale(.82)}}
.acct-body{display:grid;grid-template-columns:repeat(4,auto);justify-content:start;
  gap:0 var(--s6);padding:var(--s5) var(--s5) var(--s5)}
.acct-item{display:flex;flex-direction:column;gap:6px;padding-right:var(--s6);position:relative}
.acct-item:not(:last-child)::after{content:"";position:absolute;right:0;top:1px;bottom:1px;
  width:1px;background:var(--line)}
.acct-item .ms-k{font-size:.58rem;font-weight:700;color:var(--mut);
  text-transform:uppercase;letter-spacing:.07em}
.acct-item .ms-v{font-size:.98rem;font-weight:800;color:var(--ink);
  font-variant-numeric:tabular-nums;line-height:1.15}
.acct-item .ms-v.mono{font-family:var(--mono);font-size:.84rem;font-weight:700;color:var(--ink-soft)}
.acct-pill{display:inline-flex;align-items:center;gap:7px;font-size:.8rem;font-weight:800;
  padding:5px 13px;border-radius:999px;width:fit-content;letter-spacing:.02em}
.acct-pill.live{background:var(--pos-bg);color:var(--pos);border:1px solid var(--pos-line)}
.acct-pill.live::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--pos)}
.acct-pill.paper{background:var(--surface-2);color:var(--mut);border:1px solid var(--line)}
.stats-list{display:grid;gap:0}
.stat-row{display:flex;justify-content:space-between;align-items:center;padding:11px 4px;border-bottom:1px solid var(--line)}
.stat-row:last-child{border-bottom:0}
.stat-row .k{color:var(--mut);font-size:.78rem;font-weight:600}
.stat-row .v{font-weight:800;font-variant-numeric:tabular-nums;font-size:.88rem;color:var(--ink)}
.timeline{height:300px;overflow:auto;padding-right:4px}
.act-row{display:flex;gap:var(--s3);padding:var(--s3) 2px;border-bottom:1px solid var(--line);align-items:flex-start;border-radius:var(--r-xs)}
.act-row:last-child{border-bottom:0}
.act-dot{width:9px;height:9px;border-radius:50%;flex:none;margin-top:5px;background:var(--mut-2)}
.act-dot.sig{background:#f59e0b}.act-dot.ord{background:var(--accent)}.act-dot.fill{background:var(--pos)}
.act-dot.pos{background:#3b82f6}.act-dot.warn{background:var(--warn)}
.act-body{min-width:0;flex:1}
.act-head{display:flex;align-items:center;gap:var(--s2);margin-bottom:2px}
.act-msg{font-size:.76rem;color:var(--ink-soft);margin-top:3px;word-break:break-word;line-height:1.4}
.tag{font-size:.58rem;font-weight:800;padding:2px 7px;border-radius:6px;background:var(--surface-2);
  border:1px solid var(--line);color:var(--mut);text-transform:uppercase;letter-spacing:.05em;white-space:nowrap}
.tag.sig{color:#b45309;background:var(--warn-bg);border-color:var(--warn-line)}
.tag.ord{color:var(--accent-600);background:var(--accent-soft);border-color:var(--accent-ring)}
.tag.fill{color:var(--pos);background:var(--pos-bg);border-color:var(--pos-line)}
.tag.pos{color:#2563eb;background:#eff6ff;border-color:#bfdbfe}
.tag.warn{color:var(--warn);background:var(--warn-bg);border-color:var(--warn-line)}
.tag.err{color:var(--neg);background:var(--neg-bg);border-color:var(--neg-line)}
.act-dot.err{background:var(--neg)}
.act-row.fail{background:var(--neg-bg)}
.act-row.fail .act-msg{color:var(--neg);font-weight:700}
.log{height:300px;overflow:auto;background:#0b1120;color:#cbd5e1;border-radius:var(--r-sm);padding:var(--s3) var(--s4);
  font-family:var(--mono);font-size:.72rem;line-height:1.6}
.log-line{padding:2px 0;display:flex;gap:var(--s3)}
.log-line .t{color:#64748b;flex:none}.log-line .msg{color:#cbd5e1;word-break:break-word;min-width:0}
.log-line .sig{color:#fcd34d}.log-line .ord{color:#c4b5fd}.log-line .fill{color:#6ee7b7}
.log-line .pos{color:#93c5fd}.log-line .sid{color:#93c5fd}.log-line .warn{color:#fcd34d}.log-line .mut{color:#94a3b8}
.btn{padding:9px 16px;border-radius:var(--r-sm);border:1px solid var(--line);background:var(--accent);color:#fff;
  font-weight:800;font-size:.78rem;cursor:pointer;transition:.16s;font-family:var(--ff);letter-spacing:-.01em;min-height:44px}
.btn:hover{background:var(--accent-600);transform:translateY(-1px);box-shadow:0 8px 18px var(--accent-ring)}
.btn:active{transform:translateY(0)}.btn:focus-visible{outline:2px solid var(--accent-ring);outline-offset:2px}
.btn.ghost{background:var(--surface-2);color:var(--ink-soft);border-color:var(--line)}
.btn.ghost:hover{background:var(--surface);color:var(--ink);box-shadow:none}
.btn.danger{background:var(--neg);color:#fff;border-color:var(--neg)}
.btn.warn{background:var(--warn);color:#fff;border-color:var(--warn)}
.btn.sm{padding:6px 11px;font-size:.7rem}
.frm{display:grid;grid-template-columns:1fr 1fr;gap:var(--s3)}
.fld{display:flex;flex-direction:column;gap:5px}
.fld label{font-size:.64rem;font-weight:700;color:var(--mut);text-transform:uppercase;letter-spacing:.05em}
.fld input,.fld select{padding:9px 11px;border-radius:var(--r-sm);border:1px solid var(--line);
  background:var(--surface-2);color:var(--ink);font-size:.82rem;font-family:var(--mono);transition:.15s;min-height:44px}
.fld input:focus,.fld select:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-ring);background:var(--surface)}
.fld.full{grid-column:1 / -1}
.ctrl-actions{display:flex;gap:var(--s2);flex-wrap:wrap;margin-top:var(--s3);align-items:center}
.out{margin-top:var(--s3);font-size:.72rem;white-space:pre-wrap;font-family:var(--mono);
  background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-sm);padding:var(--s3);max-height:240px;overflow:auto}
.out.ok{color:var(--pos);border-color:var(--pos-line);background:var(--pos-bg)}
.out.err{color:var(--neg);border-color:var(--neg-line);background:var(--neg-bg)}
.foot{display:flex;align-items:center;gap:var(--s4);flex-wrap:wrap;margin-top:var(--s5);padding:var(--s4) var(--s5);
  color:var(--mut);font-size:.72rem;font-weight:500;border-top:1px solid var(--line)}
.foot .foot-brand{margin-left:auto;color:var(--mut-2)}
@media (max-width:1100px){.grid2col{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}.grid2col>.card:first-child{grid-column:span 2}}
@media (max-width:900px){.kpi-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:820px){.grid2x2{grid-template-columns:1fr}.grid2col{grid-template-columns:1fr}.grid2col>.card:first-child{grid-column:span 1}}
@media (max-width:640px){.shell{padding:var(--s4) var(--s3) var(--s6)}.tabs{padding:var(--s4) var(--s3) 0}
  .kpi{padding:var(--s3) var(--s4)}.kpi-value{font-size:1.3rem}
  .brand-text p{display:none}.topbar{padding:var(--s3)}.frm{grid-template-columns:1fr}.acct-body{grid-template-columns:1fr 1fr;gap:var(--s5) var(--s6);padding:var(--s4) var(--s4) var(--s5)}.acct-item:not(:last-child)::after{display:none}.acct-item{padding-right:0}}
@media (max-width:480px){.kpi-grid{grid-template-columns:1fr}.kpi:last-child{grid-column:auto}.tab{font-size:.74rem;padding:9px 0}}
</style>
</head>
<body>
<div class="bg" aria-hidden="true"></div>

<header class="topbar">
  <div class="brand">
    <div class="brand-mark" aria-hidden="true">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 17l6-6 4 4 6-7"></path><path d="M15 8h5v5"></path></svg>
    </div>
    <div class="brand-text">
      <h1>bitget-listener</h1>
      <p>@fattyfatclub → Bitget USDⓈ-M Futures · signal execution</p>
    </div>
  </div>
  <div class="status-cluster">
    <span id="mode" class="pill paper">PAPER</span>
    <span class="pill accent">@fattyfatclub</span>
    <span class="conn" id="conn"><span class="dot live"></span><span id="connText">live</span></span>
  </div>
</header>
<nav class="tabs" id="tabs">
  <button class="tab active" data-tab="overview">Overview</button>
  <button class="tab" data-tab="activity">Activity</button>
  <button class="tab" data-tab="control">Control</button>
</nav>

<main class="shell" id="shell">

  <!-- ===== OVERVIEW ===== -->
  <section class="tab-pane active" id="tab-overview">
    <section class="card account-card">
      <div class="card-head"><h2>Account</h2><span class="acct-status"><span class="dot"></span>Paper · Bitget USDⓈ-M Futures</span></div>
      <div class="card-body acct-body">
        <div class="acct-item"><span class="ms-k">Mode</span><span class="acct-pill paper" id="ms_mode">PAPER</span></div>
        <div class="acct-item"><span class="ms-k">Exchange</span><span class="ms-v">Bitget USDⓈ-M</span></div>
        <div class="acct-item"><span class="ms-k">Watched</span><span class="ms-v" id="ms_watch">—</span></div>
        <div class="acct-item"><span class="ms-k">Last update</span><span class="ms-v mono" id="ms_upd">—</span></div>
      </div>
    </section>

    <section class="kpi-grid">
      <div class="card kpi"><div class="kpi-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12V7H5a2 2 0 0 0-4 4h14v4"></path><path d="M3 5v14a2 2 0 0 0 2 2h16v-5"></path></svg></div><div class="kpi-body"><div class="kpi-label">Net P&amp;L (after fees)</div><div class="kpi-value num" id="k_pnl">—</div><div class="kpi-sub" id="k_pnl_sub">USDT, fee-aware</div></div></div>
      <div class="card kpi"><div class="kpi-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 7 13.5 15.5 8.5 10.5 2 17"></polyline><polyline points="16 7 22 7 22 13"></polyline></svg></div><div class="kpi-body"><div class="kpi-label">Fees paid</div><div class="kpi-value num" id="k_fees">—</div><div class="kpi-sub">real execFee (BUG A)</div></div></div>
      <div class="card kpi"><div class="kpi-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 2 9 5-9 5-9-5 9-5Z"></path><path d="m3 12 9 5 9-5"></path></svg></div><div class="kpi-body"><div class="kpi-label">Open positions</div><div class="kpi-value num" id="k_pos">0</div><div class="kpi-sub">active positions</div></div></div>
      <div class="card kpi"><div class="kpi-icon amber"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"></circle><circle cx="12" cy="12" r="5"></circle><circle cx="12" cy="12" r="1"></circle></svg></div><div class="kpi-body"><div class="kpi-label">Win rate</div><div class="kpi-value num muted" id="k_wr">—</div><div class="kpi-sub">needs live fills</div></div></div>
      <div class="card kpi"><div class="kpi-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="20" x2="12" y2="10"></line><line x1="18" y1="20" x2="18" y2="4"></line><line x1="6" y1="20" x2="6" y2="16"></line></svg></div><div class="kpi-body"><div class="kpi-label">Profit factor</div><div class="kpi-value num muted" id="k_pf">—</div><div class="kpi-sub">gross win / loss</div></div></div>
      <div class="card kpi"><div class="kpi-icon red"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 17 13.5 8.5 8.5 13.5 2 7"></polyline><polyline points="16 17 22 17 22 11"></polyline></svg></div><div class="kpi-body"><div class="kpi-label">Max drawdown</div><div class="kpi-value num muted" id="k_dd">—</div><div class="kpi-sub">peak to trough</div></div></div>
      <div class="card kpi"><div class="kpi-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"></polyline></svg></div><div class="kpi-body"><div class="kpi-label">Trades</div><div class="kpi-value num" id="k_trades">0</div><div class="kpi-sub">closed trades</div></div></div>
    </section>

    <section class="section card">
      <div class="card-head"><h2>Go-live gate</h2><span class="hint" id="gateHint">paper by default</span></div>
      <div class="card-body"><div id="gate"></div></div>
    </section>

    <section class="section card">
      <div class="card-head"><h2>Wallet history</h2><span class="hint" id="walletHint">equity (cum PnL)</span></div>
      <div class="card-body">
        <div class="chart-tabs">
          <button class="chip active" data-series="equity">Equity</button>
          <button class="chip" data-series="fees">Fees paid</button>
          <button class="chip" data-series="open">Open positions</button>
        </div>
        <div class="chart-wrap"><svg id="walletChart" viewBox="0 0 600 220" preserveAspectRatio="none" class="chart"></svg></div>
        <div class="table-wrap" style="margin-top:12px;max-height:240px">
          <table><thead><tr><th>Time (WIB)</th><th>Equity</th><th>Fees</th><th>Open</th></tr></thead><tbody id="walletBody"></tbody></table>
        </div>
        <div class="empty" id="walletEmpty" style="display:none"><span>No wallet samples yet — first sample in ~60s.</span></div>
      </div>
    </section>

    <div class="grid2x2">
      <section class="card">
        <div class="card-head"><h2>Open positions</h2><span class="hint" id="posCount">0</span></div>
        <div class="card-body">
          <div class="table-wrap" id="positionsTable" style="display:none">
            <table><thead><tr><th>Symbol</th><th>Side</th><th>Size</th><th>Entry</th><th>Mark</th><th>Liq</th><th>PnL</th><th>PnL%</th><th></th></tr></thead><tbody id="positionsBody"></tbody></table>
          </div>
          <div class="empty" id="posEmpty"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"></path><path d="M7 14l3-3 3 3 4-5"></path></svg><span>No open positions yet — signals that fill will appear here.<span class="e-sub"> Paper.</span></span></div>
        </div>
      </section>
      <section class="card">
        <div class="card-head"><h2>Recent trades</h2><span class="hint">last 30</span></div>
        <div class="card-body"><div class="table-wrap"><table><thead><tr><th>Time</th><th>Symbol</th><th>PnL</th></tr></thead><tbody id="tradesBody"><tr><td colspan="3"><div class="empty"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 18 0 9 9 0 0 0-18 0"></path><path d="M12 7v5l3 2"></path></svg><span>No closed trades yet.<span class="e-sub"> Fills &amp; exits show here.</span></span></div></td></tr></tbody></table></div></div>
      </section>
    </div>

    <section class="section card">
      <div class="card-head"><h2>Rejections &amp; skips</h2></div>
      <div class="card-body"><div id="skips" class="empty"><span>None.</span></div></div>
    </section>
  </section>

  <!-- ===== ACTIVITY ===== -->
  <section class="tab-pane" id="tab-activity">
    <div class="grid2col">
      <section class="card">
        <div class="card-head"><h2>Activities</h2><span class="hint">control actions logged server-side</span></div>
        <div class="card-body"><div class="timeline" id="activities"><div class="empty"><span>No activity yet.</span></div></div></div>
      </section>
      <section class="card">
        <div class="card-head"><h2>Statistics</h2></div>
        <div class="card-body"><div class="stats-list">
          <div class="stat-row"><span class="k">Total trades</span><span class="v num" id="s_total">0</span></div>
          <div class="stat-row"><span class="k">Wins / Losses</span><span class="v num" id="s_wl">0 / 0</span></div>
          <div class="stat-row"><span class="k">Win rate</span><span class="v num" id="s_wr">—</span></div>
          <div class="stat-row"><span class="k">Profit factor</span><span class="v num" id="s_pf">—</span></div>
          <div class="stat-row"><span class="k">Expectancy</span><span class="v num" id="s_exp">—</span></div>
          <div class="stat-row"><span class="k">Max drawdown</span><span class="v num" id="s_dd">—</span></div>
          <div class="stat-row"><span class="k">Open positions</span><span class="v num" id="s_open">0</span></div>
          <div class="stat-row"><span class="k">Net PnL</span><span class="v num" id="s_net">—</span></div>
        </div></div>
      </section>
      <section class="card">
        <div class="card-head"><h2>Logs</h2><span class="hint">live event stream</span></div>
        <div class="card-body"><div class="log" id="logs"></div></div>
      </section>
    </div>
  </section>

  <!-- ===== CONTROL ===== -->
  <section class="tab-pane" id="tab-control">
    <section class="section card">
      <div class="card-head"><h2>Control panel</h2><span class="hint">close · mode — password required</span></div>
      <div class="card-body">
        <div class="ctrl-actions">
          <span id="mode2" class="pill paper">PAPER</span>
          <button class="btn ghost sm" id="c_topaper">→ Paper</button>
          <button class="btn sm" id="c_tolive">→ Live (token)</button>
        </div>
        <div class="ctrl-actions">
          <button class="btn warn" id="c_flatten">Flatten all (close everything)</button>
        </div>
        <div class="fld full" style="margin-top:12px"><label>Go-live token</label><input id="c_token" type="text" placeholder="paste --go-live token, then → Live" autocomplete="off"></div>
        <div class="out" id="c_out"></div>
      </div>
    </section>
  </section>

  <footer class="foot">
    <span id="health">poll 5s · listening since — · updated —</span>
    <span class="foot-brand">bitget-listener · real-time dashboard · times in Asia/Jakarta (UTC+7)</span>
  </footer>
</main>

<script>
(function(){
'use strict';
const WSS = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/events';
const $ = id => document.getElementById(id);
function esc(s){s=String(s==null?'':s);return s.replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
function num(v){const n=Number(v);return (v===null||v===undefined||v===''||isNaN(n))?null:n;}
function fmt(n){n=num(n);if(n===null)return '—';const a=Math.abs(n);if(a>=1)return n.toLocaleString(undefined,{maximumFractionDigits:2});return n.toFixed(4);}
function fmtSigned(n){n=num(n);if(n===null)return '—';return (n>=0?'+':'')+fmt(n);}
function logTime(ts){const d=ts?new Date(ts*1000):new Date();return d.toLocaleTimeString('en-GB',{timeZone:'Asia/Jakarta',hour12:false});}
function clsOf(v){const n=num(v);if(n===null)return '';if(n>0)return 'pos';if(n<0)return 'neg';return '';}
function setConn(live){const dot=document.querySelector('#conn .dot');const txt=$('connText');if(live){dot.className='dot live';txt.textContent='live';}else{dot.className='dot';txt.textContent='reconnecting…';}}

// auth (control actions only; monitoring is open)
function setModeUI(paper){const m=$('mode');m.textContent=paper?'PAPER':'LIVE';m.className='pill '+(paper?'paper':'live');const m2=$('mode2');if(m2){m2.textContent=paper?'PAPER':'LIVE';m2.className='pill '+(paper?'paper':'live');}}
function askPw(){return prompt('Enter dashboard password to control the bot:');}
function withPw(body){const pw=askPw();if(pw===null)return null;return Object.assign({},body,{password:pw});}

// tabs
function showTab(name){
  document.querySelectorAll('.tab-pane').forEach(p=>p.classList.toggle('active', p.id==='tab-'+name));
  document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('active', t.dataset.tab===name));
  try{localStorage.setItem('dash_tab',name);}catch(e){}
}
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>showTab(t.dataset.tab));
try{const saved=localStorage.getItem('dash_tab');if(saved)showTab(saved);}catch(e){}

// polling snapshot against bitget's /api/snapshot (tia-compatible shape)
async function refresh(){
  try{const r=await fetch('/api/snapshot');const s=await r.json();renderSnapshot(s);}catch(e){}
  setTimeout(refresh,5000);
}
function renderSnapshot(s){
  if(!s)return;
  window.__lastSnap = s;
  const paper=!!s.paper;setModeUI(paper);
  if($('ms_mode')){const me=$('ms_mode');me.textContent=paper?'PAPER':'LIVE';me.className='acct-pill '+(paper?'paper':'live');}
  if($('ms_watch'))$('ms_watch').textContent=(s.market&&Object.keys(s.market).length)?Object.keys(s.market).length+' pairs':'—';
  if($('ms_upd'))$('ms_upd').textContent=logTime(Math.floor(Date.now()/1000));
  const st=s.stats||{};
  const bal=(s.balance&&s.balance.equity!=null)?s.balance.equity:0;
  $('k_pnl').textContent=fmtSigned(st.net_pnl!=null?st.net_pnl:bal);$('k_pnl').className='kpi-value num '+clsOf(st.net_pnl!=null?st.net_pnl:bal);
  $('k_fees').textContent=fmt(s.fees_usd!=null?s.fees_usd:(s.balance&&s.balance.available!=null?0:0));$('k_fees').className='kpi-value num';
  $('k_pos').textContent=st.open_positions!=null?st.open_positions:(s.positions?s.positions.length:0);
  $('k_wr').textContent=st.win_rate?st.win_rate+'%':'—';$('k_wr').classList.toggle('muted',!st.win_rate);
  $('k_pf').textContent=st.profit_factor?st.profit_factor:'—';$('k_pf').classList.toggle('muted',!st.profit_factor);
  $('k_dd').textContent=st.max_drawdown?fmt(st.max_drawdown):'—';$('k_dd').classList.toggle('muted',!st.max_drawdown);
  $('k_trades').textContent=st.total_trades||0;
  // go-live gate
  const g=$('gate');
  if(s.gate&&s.gate.conditions_met){g.innerHTML='<span class="pos" style="color:var(--pos);font-weight:800">all conditions met — run --go-live for a token</span>';}
  else if(s.gate){g.innerHTML='<div class="k" style="color:var(--mut)">blocked by</div><ul style="margin:6px 0 0;padding-left:18px;color:var(--ink-soft);font-size:.8rem">'+
    (s.gate.unmet||[]).map(u=>'<li>'+esc(u)+'</li>').join('')+'</ul>';$('gateHint').textContent=s.gate.paper?'paper by default':'conditions unmet';}
  const pos=s.positions||[];$('posCount').textContent=pos.length;
  if(pos.length){const tb=$('positionsBody');tb.innerHTML='';pos.forEach(p=>{
      const tr=document.createElement('tr');
      tr.innerHTML='<td class="sym">'+esc(p.symbol)+'</td><td><span class="side '+(p.direction||'').toLowerCase()+'">'+(p.direction||'')+'</span></td><td class="num">'+fmt(p.size)+'</td><td class="num">'+fmt(p.entry)+'</td><td class="num">'+fmt(p.mark)+'</td><td class="num">'+fmt(p.liq_price)+'</td><td class="num '+clsOf(p.pnl)+'">'+fmtSigned(p.pnl)+'</td>'+'<td class="num '+clsOf(p.pnl)+'">'+(p.pnl!=null&&p.entry&&p.size?((p.pnl/(p.entry*p.size)*100).toFixed(2)+'%'):'—')+'</td>'+'<td><button class="btn danger sm" data-close="'+esc(p.symbol)+'">Close</button></td>';
      tb.appendChild(tr);});
    $('positionsTable').style.display='';$('posEmpty').style.display='none';
  } else {$('positionsTable').style.display='none';$('posEmpty').style.display='';}
  const tb2=$('tradesBody');const trs=(s.trades||[]).slice(0,30);
  if(trs.length){tb2.innerHTML='';trs.forEach(t=>{const tr=document.createElement('tr');
    tr.innerHTML='<td class="num">'+logTime(t.close_ts||t.ts)+'</td><td class="sym">'+esc(t.symbol)+'</td><td class="num '+(clsOf(t.pnl))+'">'+fmtSigned(t.pnl)+'</td>';
    tb2.appendChild(tr);});}else{tb2.innerHTML='<tr><td colspan="3"><div class="empty"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 18 0 9 9 0 0 0-18 0"/><path d="M12 7v5l3 2"/></svg><span>No closed trades yet.<span class="e-sub"> Fills &amp; exits show here.</span></span></div></td></tr>';}
  // skips / rejections
  const sk=$('skips');const skipped=(s.skipped&&s.skipped.length)?s.skipped:(s.gate&&s.gate.unmet?[]:[]);
  sk.innerHTML=(skipped&&skipped.length)?'<ul style="margin:0;padding-left:18px;color:var(--ink-soft);font-size:.8rem">'+skipped.map(x=>'<li>'+esc(x.symbol||'?')+': '+esc(x.reason||'')+'</li>').join('')+'</ul>':'<span>None.</span>';
  // wallet history (table + chart)
  renderWallet(s.equity_history||[]);
  // stats
  $('s_total').textContent=st.total_trades||0;$('s_wl').textContent=(st.wins||0)+' / '+(st.losses||0);
  $('s_wr').textContent=st.win_rate?st.win_rate+'%':'—';$('s_pf').textContent=st.profit_factor?st.profit_factor:'—';
  $('s_exp').textContent=st.expectancy?fmt(st.expectancy):'—';$('s_dd').textContent=st.max_drawdown?fmt(st.max_drawdown):'—';
  $('s_open').textContent=st.open_positions!=null?st.open_positions:pos.length;$('s_net').textContent=fmtSigned(st.net_pnl);
  // activity (bitget logs control actions server-side; show gate unmet as muted rows)
  $('health').textContent='poll 5s · listening since '+logTime(Math.floor(Date.now()/1000))+' · updated '+logTime(Math.floor(Date.now()/1000));
}

// ── wallet history chart + table ──
let WALLET_SERIES = 'equity';
function renderWallet(hist){
  const wb=$('walletBody');const empty=$('walletEmpty');const chart=$('walletChart');
  if(!hist||!hist.length){empty.style.display='';wb.innerHTML='';chart.innerHTML='';return;}
  empty.style.display='none';
  // table (newest first, last 50)
  const rows=hist.slice(-50).slice().reverse();
  wb.innerHTML=rows.map(r=>{
    const d=new Date(r.ts_ms/1000);
    const t=d.toLocaleString('en-GB',{timeZone:'Asia/Jakarta',hour12:false}).replace(',',' ');
    return '<tr><td class="num">'+esc(t)+' WIB</td><td class="num">'+fmt(r.equity_usd)+
      '</td><td class="num">'+fmt(r.fees_usd)+'</td><td class="num">'+r.open_pos+'</td></tr>';
  }).join('');
  drawWalletChart(hist, WALLET_SERIES);
}
function drawWalletChart(hist, series){
  const svg=$('walletChart');const W=600,H=220,pad=28;
  const vals=hist.map(r=>series==='fees'?r.fees_usd:(series==='open'?r.open_pos:r.equity_usd));
  const min=Math.min.apply(null,vals), max=Math.max.apply(null,vals);
  const span=(max-min)||1; const n=vals.length;
  const X=i=>pad+(i/(Math.max(n-1,1)))*(W-pad*2);
  const Y=v=>H-pad-((v-min)/span)*(H-pad*2);
  let line='', area='';
  vals.forEach((v,i)=>{const x=X(i).toFixed(1),y=Y(v).toFixed(1);
    line+=(i?'L':'M')+x+' '+y+' '; area+=(i?('L'+x+' '+y+' '):('M'+x+' '+y+' '));});
  area+='L'+X(n-1).toFixed(1)+' '+(H-pad)+' L'+X(0).toFixed(1)+' '+(H-pad)+' Z';
  const col=series==='fees'?'var(--warn)':(series==='open'?'var(--accent-600)':'var(--accent)');
  svg.innerHTML='<defs><linearGradient id="walletFill" x1="0" y1="0" x2="0" y2="1">'+
    '<stop offset="0%" stop-color="'+col+'" stop-opacity="0.35"/>'+
    '<stop offset="100%" stop-color="'+col+'" stop-opacity="0"/></linearGradient></defs>'+
    '<path class="area" d="'+area+'"/>'+
    '<path class="line" d="'+line+'" style="stroke:'+col+'"/>'+
    '<text class="lbl" x="4" y="14">'+fmt(max)+'</text>'+
    '<text class="lbl" x="4" y="'+(H-8)+'">'+fmt(min)+'</text>'+
    '<text class="lbl" x="'+(W-pad-2)+'" y="'+(H-8)+'" text-anchor="end">'+hist.length+' pts</text>';
}
document.querySelectorAll('.chip').forEach(c=>c.onclick=()=>{
  document.querySelectorAll('.chip').forEach(x=>x.classList.toggle('active',x===c));
  WALLET_SERIES=c.dataset.series;
  const el=$('walletBody'); // re-render uses last snapshot via global
  if(window.__lastSnap) drawWalletChart(window.__lastSnap.equity_history||[], WALLET_SERIES);
});

// ── control (bitget gated endpoints) ──
$('c_topaper').onclick=async()=>{const body=withPw({});if(!body)return;
  const r=await fetch('/api/paper',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:body.password})});
  const d=await r.json().catch(()=>({}));$('c_out').className='out '+(r.ok?'ok':'err');$('c_out').textContent=JSON.stringify(d,null,2);refresh();};
$('c_tolive').onclick=async()=>{const tok=$('c_token').value.trim();if(!tok){$('c_out').className='out err';$('c_out').textContent='enter a go-live token first (run python -m src.main --go-live)';return;}
  const body=withPw({token:tok});if(!body)return;
  const r=await fetch('/api/live',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:body.password,token:tok})});
  const d=await r.json().catch(()=>({}));$('c_out').className='out '+(r.ok?'ok':'err');$('c_out').textContent=JSON.stringify(d,null,2);refresh();};
$('positionsBody').addEventListener('click',async e=>{const b=e.target.closest('[data-close]');if(!b)return;const sym=b.getAttribute('data-close');if(!confirm('Close '+sym+'?'))return;
  const body=withPw({symbol:sym});if(!body)return;
  const r=await fetch('/api/close',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:body.password,symbol:sym})});
  const d=await r.json().catch(()=>({}));$('c_out').className='out '+(r.ok?'ok':'err');$('c_out').textContent=JSON.stringify(d,null,2);refresh();});
$('c_flatten').onclick=async()=>{if(!confirm('FLATTEN ALL — close every open position now?'))return;
  const r=await fetch('/api/snapshot');const s=await r.json().catch(()=>({}));const pos=(s.positions||[]);
  if(!pos.length){$('c_out').className='out';$('c_out').textContent='nothing to close (no open positions)';return;}
  const pw=askPw();if(pw===null)return;let ok=0,fail=0,msg=[];
  for(const p of pos){const rr=await fetch('/api/close',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw,symbol:p.symbol})});
    const d=await rr.json().catch(()=>({}));if(d&&(d.ok||rr.ok))ok++;else{fail++;msg.push(p.symbol+': '+(d.error||'failed'));}}
  $('c_out').className='out '+(fail?'err':'ok');$('c_out').textContent='Flatten: closed '+ok+(fail?(' · failed '+fail+' ('+msg.join('; ')+')'):'');refresh();};

// ── start ──
function start(){setConn(true);refresh();}
start();
})();
</script>
</body></html>
"""
