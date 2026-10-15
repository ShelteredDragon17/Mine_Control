"""Local status page and controls: http://127.0.0.1:8787 by default (localhost only unless configured).

GET  /             status page (refreshes every 30 s)
GET  /api/status   the same as JSON
POST /api/override {"mode": "sleep" | "normal" | "pause", "minutes": 60}
POST /api/override/clear
"""
from __future__ import annotations

import html

from aiohttp import web

from .agent import Agent

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="30">
<title>Curtail agent</title><style>
:root{{--bg:#fafaf9;--fg:#111;--mut:#555;--line:#ddd;--ok:#0a7a3a;--bad:#b42318;--btn:#256abf}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--fg:#f2f2f0;--mut:#aaa;--line:#333;--ok:#4cc07a;--bad:#ff8a80}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif;max-width:860px;margin:0 auto;padding:16px}}
table{{width:100%;border-collapse:collapse}}td,th{{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left}}
.mut{{color:var(--mut)}}.ok{{color:var(--ok)}}.bad{{color:var(--bad)}}
button{{font:inherit;padding:6px 12px;border:0;border-radius:6px;background:var(--btn);color:#fff;cursor:pointer;margin:2px}}
.box{{border:1px solid var(--line);border-radius:8px;padding:12px;margin:12px 0}}
</style></head><body>
<h1>Curtail agent</h1>
<div class="box"><b>Schedule says: {desired}</b> <span class="mut">({reason})</span><br>
<span class="mut">Local time {now} · next change {next}</span>{override}</div>
<table><tr><th>Miner</th><th>Model</th><th>Mode</th><th>Hashrate</th><th>Last command</th></tr>{rows}</table>
<div class="box"><b>Manual override</b><br>
<button onclick="ov('sleep',60)">Sleep 1 h</button><button onclick="ov('normal',60)">Run 1 h</button>
<button onclick="ov('pause',240)">Pause automation 4 h</button><button onclick="clr()">Back to schedule</button></div>
<div class="box"><b>Estimated savings</b> <span class="mut">(time asleep inside schedule windows)</span><br>{savings}</div>
<script>
async function ov(mode,minutes){{await fetch('/api/override',{{method:'POST',headers:{{'content-type':'application/json'}},body:JSON.stringify({{mode,minutes}})}});location.reload()}}
async function clr(){{await fetch('/api/override/clear',{{method:'POST'}});location.reload()}}
</script></body></html>"""


def _row(m: dict) -> str:
    e = html.escape
    if not m["reachable"]:
        state = f'<span class="bad">unreachable</span> <span class="mut">{e(m["error"] or "")}</span>'
    else:
        state = e(m["mode"])
    hr = f'{m["hashrate"]:.1f} {e(m["unit"] or "")}' if m["hashrate"] is not None else "—"
    lc = m["last_command"]
    last = "—" if not lc else (f'<span class="{"ok" if lc["ok"] else "bad"}">{e(lc["mode"])} '
                               f'{"confirmed" if lc["ok"] else "NOT confirmed"}</span>'
                               + (f' <span class="mut">via {e(lc["method"] or "")}</span>' if lc["ok"] else ""))
    return f'<tr><td>{e(m["name"])}<br><span class="mut">{e(m["ip"])}</span></td><td>{e(m["model"] or "—")}</td><td>{state}</td><td>{hr}</td><td>{last}</td></tr>'


def make_app(agent: Agent) -> web.Application:
    async def page(_):
        s = agent.status()
        ov = s["override"]
        sv = s["savings"]
        savings = f'{sv["kwh_total"]:.1f} kWh' + (f' · ${sv["dollars_total"]:.2f}' if "dollars_total" in sv else "")
        body = PAGE.format(
            desired=html.escape(str(s["desired"] or "nothing (paused)")), reason=html.escape(s["reason"]),
            now=html.escape(s["now"]), next=html.escape(s["next_change"] or "none in the next week"),
            override=(f'<br><b>Override:</b> {html.escape(str(ov["mode"] or "paused"))} until {html.escape(ov["until"])}' if ov else ""),
            rows="".join(_row(m) for m in s["miners"]), savings=savings)
        return web.Response(text=body, content_type="text/html")

    async def status(_):
        return web.json_response(agent.status())

    async def override(request: web.Request):
        body = await request.json()
        mode = body.get("mode")
        if mode not in ("sleep", "normal", "pause"):
            raise web.HTTPBadRequest(text="mode must be sleep, normal or pause")
        minutes = int(body.get("minutes", 60))
        if not 1 <= minutes <= 7 * 24 * 60:
            raise web.HTTPBadRequest(text="minutes must be 1..10080")
        agent.set_override(None if mode == "pause" else mode, minutes)
        return web.json_response({"ok": True})

    async def clear(_):
        agent.clear_override()
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_get("/", page)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/override", override)
    app.router.add_post("/api/override/clear", clear)
    return app
