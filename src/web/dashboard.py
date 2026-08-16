"""FastAPI operator dashboard.

Standing UX rules for this operator, applied here:
- monitoring loads OPEN — no login gate on the read-only page or its JSON;
- every STATE-CHANGING action (go-live redeem, close, paper toggle) requires the
  ``DASH_PASSWORD`` header/field and is logged server-side at ERROR;
- all timestamps render in Asia/Jakarta (UTC+7), formatted server-side;
- mobile-first: no horizontal scroll at 390px (verified with a headless browser).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

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

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return HTMLResponse(INDEX_HTML)

    # -------------------------------------------------------- control (gated)
    @app.post("/api/close")
    async def close(payload: dict = Body(...), x_dash_password: str | None = Header(None)):
        require_auth(x_dash_password, f"close {payload.get('symbol')}")
        res = await orchestrator.positions.full_close(
            str(payload.get("symbol", "")).upper(), reason="dashboard")
        return JSONResponse(res)

    @app.post("/api/paper")
    async def to_paper(x_dash_password: str | None = Header(None)):
        require_auth(x_dash_password, "to_paper")
        return {"message": orchestrator.gate.to_paper("dashboard")}

    @app.post("/api/live")
    async def go_live(payload: dict = Body(...), x_dash_password: str | None = Header(None)):
        require_auth(x_dash_password, "go_live")
        ok, msg = orchestrator.gate.redeem(str(payload.get("token", "")))
        if not ok:
            log.error("go-live redeem REFUSED via dashboard: %s", msg)
            raise HTTPException(status_code=400, detail=msg)
        return {"message": msg}

    return app


INDEX_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>bitget-listener</title>
<style>
  :root{--bg:#0b0e13;--card:#141922;--line:#232a36;--fg:#e6edf3;--dim:#8b98a9;
        --green:#2ea043;--red:#f85149;--amber:#d29922}
  *{box-sizing:border-box}
  html,body{margin:0;padding:0;background:var(--bg);color:var(--fg);
    font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    overflow-x:hidden}
  .wrap{max-width:900px;margin:0 auto;padding:12px}
  h1{font-size:16px;margin:0 0 10px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .badge{font-size:11px;padding:2px 8px;border-radius:999px;border:1px solid var(--line)}
  .paper{background:#12281a;color:var(--green);border-color:var(--green)}
  .live{background:#2d1315;color:var(--red);border-color:var(--red)}
  .halt{background:#2b2412;color:var(--amber);border-color:var(--amber)}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px}
  .k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.04em}
  .v{font-size:18px;font-variant-numeric:tabular-nums;margin-top:2px;word-break:break-all}
  .pos{color:var(--green)} .neg{color:var(--red)}
  section{margin-top:14px}
  h2{font-size:12px;color:var(--dim);text-transform:uppercase;letter-spacing:.05em;margin:0 0 6px}
  .tablewrap{border:1px solid var(--line);border-radius:10px;overflow:hidden}
  table{border-collapse:collapse;width:100%;font-size:12px}
  th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--line);white-space:nowrap}
  th{color:var(--dim);font-weight:500;background:#111721}
  tr:last-child td{border-bottom:none}
  td.num{font-variant-numeric:tabular-nums}
  /* Narrow screens: stack each row as a labelled card so NOTHING is ever cut off
     and the page never scrolls sideways. */
  @media (max-width:600px){
    .tablewrap{border:none;border-radius:0;overflow:visible}
    table,thead,tbody,tr,td{display:block;width:100%}
    thead{display:none}
    tr{background:var(--card);border:1px solid var(--line);border-radius:10px;
       padding:4px 2px;margin-bottom:8px}
    td{border:none;display:flex;justify-content:space-between;gap:12px;
       padding:4px 10px;white-space:normal}
    td::before{content:attr(data-label);color:var(--dim);font-size:11px;
       text-transform:uppercase;letter-spacing:.04em;flex:0 0 auto}
    td>span{text-align:right;word-break:break-word}
    tr:last-child td{border-bottom:none}
  }
  ul{margin:6px 0 0;padding-left:18px;color:var(--dim);font-size:12px}
  .muted{color:var(--dim);font-size:12px;padding:8px 2px}
  footer{color:var(--dim);font-size:11px;margin:16px 0 8px;text-align:center}
</style></head>
<body><div class="wrap">
  <h1>bitget-listener <span id="mode" class="badge">…</span>
      <span id="halt" class="badge halt" hidden>FEED STALE</span></h1>

  <div class="grid">
    <div class="card"><div class="k">Net P&amp;L (after fees)</div><div class="v" id="net">–</div></div>
    <div class="card"><div class="k">Fees paid</div><div class="v" id="fees">–</div></div>
    <div class="card"><div class="k">Closed trades</div><div class="v" id="closed">–</div></div>
    <div class="card"><div class="k">Open</div><div class="v" id="nopen">–</div></div>
  </div>

  <section><h2>Open positions</h2>
    <div class="tablewrap"><table>
      <thead><tr><th>Symbol</th><th>Side</th><th>Size</th><th>Entry</th>
        <th>SL</th><th>TP</th><th>Liq</th><th>uPnL</th></tr></thead>
      <tbody id="positions"></tbody></table></div>
    <div class="muted" id="noPositions" hidden>No open positions.</div>
  </section>

  <section><h2>Closed trades</h2>
    <div class="tablewrap"><table>
      <thead><tr><th>Closed</th><th>Symbol</th><th>Side</th><th>Size</th><th>Entry</th>
        <th>Exit</th><th>Fees</th><th>Net</th><th>Reason</th></tr></thead>
      <tbody id="trades"></tbody></table></div>
    <div class="muted" id="noTrades" hidden>No closed trades yet.</div>
  </section>

  <section><h2>Go-live gate</h2>
    <div class="card"><div id="gate"></div></div></section>

  <section><h2>Rejections &amp; skips</h2>
    <div class="card"><div id="skips" class="muted">None.</div></div></section>

  <footer>times in Asia/Jakarta (UTC+7) · monitoring is read-only</footer>
</div>
<script>
const f = (n,d=4) => (n===null||n===undefined) ? '–' : Number(n).toFixed(d);
const sign = n => n>0 ? 'pos' : (n<0 ? 'neg' : '');
const esc = s => String(s).replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
// One cell builder for both tables: carries the label used by the mobile card layout.
const cells = rows => rows.map(([label,value,cls]) =>
  `<td class="num ${cls||''}" data-label="${esc(label)}"><span>${esc(value)}</span></td>`
).join('');
async function tick(){
  let s; try { s = await (await fetch('/api/state')).json(); } catch(e){ return; }
  const mode=document.getElementById('mode');
  mode.textContent=s.mode; mode.className='badge '+(s.mode==='LIVE'?'live':'paper');
  document.getElementById('halt').hidden = !s.deadman_halted;

  const net=document.getElementById('net');
  net.textContent=f(s.net_usd)+' USDT'; net.className='v '+sign(s.net_usd);
  document.getElementById('fees').textContent=f(s.fees_usd)+' USDT';
  document.getElementById('closed').textContent=s.closed_trades;
  document.getElementById('nopen').textContent=s.positions.length;

  const pb=document.getElementById('positions'); pb.innerHTML='';
  s.positions.forEach(p=>{
    const tr=document.createElement('tr');
    tr.innerHTML=cells([
      ['Symbol',p.symbol],['Side',p.side],['Size',p.size],['Entry',f(p.entry,2)],
      ['SL',p.sl===null?'–':f(p.sl,2)],['TP',p.tp===null?'–':f(p.tp,2)],
      ['Liq',p.liq===null?'–':f(p.liq,2)],['uPnL',f(p.upnl),sign(p.upnl)]]);
    pb.appendChild(tr);
  });
  document.getElementById('noPositions').hidden = s.positions.length>0;

  const tb=document.getElementById('trades'); tb.innerHTML='';
  (s.trades||[]).forEach(t=>{
    const fees=(t.open_fee||0)+(t.close_fee||0);
    const tr=document.createElement('tr');
    tr.innerHTML=cells([
      ['Closed',t.closed_at],['Symbol',t.symbol],['Side',t.side],
      ['Size',t.qty],['Entry',f(t.entry_price,2)],
      ['Exit',t.exit_price===null?'–':f(t.exit_price,2)],['Fees',f(fees)],
      ['Net',f(t.net_usd),sign(t.net_usd)],
      ['Reason',(t.exit_reason||'')+(t.estimated_exit?' (est)':'')]]);
    tb.appendChild(tr);
  });
  document.getElementById('noTrades').hidden = (s.trades||[]).length>0;

  const g=document.getElementById('gate');
  if(s.gate.conditions_met){ g.innerHTML='<span class="pos">all conditions met</span>'; }
  else { g.innerHTML='<div class="k">blocked by</div><ul>'+
    s.gate.unmet.map(u=>`<li>${u}</li>`).join('')+'</ul>'; }

  const sk=document.getElementById('skips');
  sk.innerHTML=(s.skipped&&s.skipped.length)
    ? '<ul>'+s.skipped.map(x=>`<li>${x.symbol||'?'}: ${x.reason}</li>`).join('')+'</ul>'
    : 'None.';
}
tick(); setInterval(tick,3000);
</script></body></html>
"""
