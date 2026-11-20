"""Self-contained HTML dashboard for GigaForge V2.

Design constraints, each of which is a real failure avoided:

* **Self-contained means self-contained.**  No CDN, no external font, no
  network request — the page renders from a cold checkout with no internet.
* **The snapshot is data, not code.**  It is embedded in a
  ``<script type="application/json">`` block, never as an executed assignment,
  and every ``</`` is escaped to ``<\\/`` so a goal string containing
  ``</script>`` cannot close the tag early and blank the page.
* **The page is a pure function of the snapshot.**  All formatting happens in
  JS from real recorded numbers; nothing is hard-coded, so a page generated from
  an empty data directory honestly shows an empty state.

Two serving modes share one renderer: ``serve`` exposes ``/api/snapshot`` plus a
SSE stream so the panel is live, and ``--out`` writes a single portable file.
"""

from __future__ import annotations

import ipaddress
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit

from .snapshot import NODE_ORDER, build_snapshot
from .store import UiStore

PAGE_TITLE = "GigaForge V6.fresh · Control Room"

#: Escaping ``</`` is what keeps an embedded value from closing the script tag.
def _embed_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str).replace("</", "<\\/")


def is_loopback_host(host: str) -> bool:
    """Return whether *host* resolves syntactically to a local loopback address."""
    value = str(host or "").strip().strip("[]").rstrip(".").lower()
    if value == "localhost":
        return True
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped.is_loopback if mapped is not None else address.is_loopback)


_CSS = """
:root{
  --bg:#f3f8fd; --bg2:#ffffff; --panel:#ffffffd9; --panel-solid:#ffffff;
  --line:#dce9f5; --line2:#bfd7eb; --fg:#183b66; --fg-dim:#718bad;
  --accent:#328df2; --accent2:#21b6d2; --ok:#1da96a; --warn:#bd7b11; --bad:#d64d63;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,Inter,Arial,sans-serif;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  background:
    radial-gradient(1100px 620px at 12% -10%,#e5f5ff 0%,transparent 55%),
    radial-gradient(900px 520px at 92% 0%,#e8fbf8 0%,transparent 52%),
    var(--bg);
  color:var(--fg); font-family:var(--sans); font-size:14px; line-height:1.5;
  min-height:100vh; -webkit-font-smoothing:antialiased;
}
a{color:var(--accent)}
.wrap{max-width:1360px;margin:0 auto;padding:22px 20px 64px}
header.top{display:flex;flex-wrap:wrap;gap:14px;align-items:center;justify-content:space-between;margin-bottom:18px}
.brand{display:flex;align-items:center;gap:12px}
.logo{
  width:42px;height:42px;border-radius:12px;flex:0 0 auto;
  background:linear-gradient(135deg,var(--accent),var(--accent2));
  display:grid;place-items:center;font-weight:800;color:#ffffff;font-size:17px;
  box-shadow:0 8px 26px #328df233;
}
h1{font-size:19px;margin:0;letter-spacing:.2px}
.sub{color:var(--fg-dim);font-size:12.5px;font-family:var(--mono)}
.pill{
  display:inline-flex;align-items:center;gap:7px;padding:5px 11px;border-radius:999px;
  border:1px solid var(--line2);background:#ffffff;font-family:var(--mono);font-size:12px;
}
.pill .dot{width:8px;height:8px;border-radius:50%;background:var(--fg-dim)}
.pill.ok .dot{background:var(--ok);box-shadow:0 0 10px var(--ok)}
.pill.bad .dot{background:var(--bad);box-shadow:0 0 10px var(--bad)}
.pill.run .dot{background:var(--warn);box-shadow:0 0 10px var(--warn);animation:pulse 1.2s infinite}
@keyframes pulse{50%{opacity:.32}}
.controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
button,select,input{
  font-family:var(--mono);font-size:12px;color:var(--fg);background:#ffffff;
  border:1px solid var(--line2);border-radius:9px;padding:7px 11px;cursor:pointer;
}
button:hover{border-color:var(--accent);color:var(--accent)}
input{cursor:text;min-width:190px}
.grid{display:grid;gap:14px}
.kpis{grid-template-columns:repeat(auto-fit,minmax(196px,1fr));margin-bottom:14px}
.card{
  background:var(--panel);border:1px solid var(--line);border-radius:15px;padding:15px 16px;
  backdrop-filter:blur(9px);position:relative;overflow:hidden;
}
.card::after{content:"";position:absolute;inset:0 0 auto 0;height:2px;background:linear-gradient(90deg,var(--accent),var(--accent2));opacity:.75}
.card .label{color:var(--fg-dim);font-size:11px;text-transform:uppercase;letter-spacing:.7px;font-family:var(--mono)}
.card .value{font-size:25px;font-weight:700;margin-top:7px;font-variant-numeric:tabular-nums}
.card .hint{color:var(--fg-dim);font-size:11.5px;margin-top:5px;font-family:var(--mono)}
.card.ok .value{color:var(--ok)} .card.warn .value{color:var(--warn)} .card.bad .value{color:var(--bad)}
.card.acc .value{color:var(--accent)} .card.mag .value{color:#c084fc}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:15px;padding:0;overflow:hidden;backdrop-filter:blur(9px)}
.panel > h2{
  margin:0;padding:13px 16px;font-size:12.5px;letter-spacing:.8px;text-transform:uppercase;
  color:var(--fg-dim);font-family:var(--mono);border-bottom:1px solid var(--line);
  display:flex;justify-content:space-between;align-items:center;gap:10px;
}
.panel .body{padding:15px 16px}
.cols{display:grid;grid-template-columns:1.35fr 1fr;gap:14px}
@media(max-width:960px){.cols{grid-template-columns:1fr}}
.bar{height:12px;border-radius:999px;background:#edf4fa;border:1px solid var(--line2);overflow:hidden}
.bar > i{display:block;height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2));transition:width .5s ease}
.bar.ok > i{background:linear-gradient(90deg,#22c55e,var(--ok))}
.bar.warn > i{background:linear-gradient(90deg,#f59e0b,var(--warn))}
.bar.bad > i{background:linear-gradient(90deg,#ef4444,var(--bad))}
.meter{margin:9px 0}
.meter .top{display:flex;justify-content:space-between;font-family:var(--mono);font-size:12px;color:var(--fg-dim);margin-bottom:5px}
.meter .top b{color:var(--fg)}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--fg-dim);font-family:var(--mono);font-size:10.5px;text-transform:uppercase;letter-spacing:.6px;position:sticky;top:0;background:var(--panel-solid)}
tbody tr:hover{background:#f0f7fd}
td.num{text-align:right;font-variant-numeric:tabular-nums;font-family:var(--mono)}
.scroll{max-height:380px;overflow:auto}
.steps{display:flex;flex-direction:column;gap:7px}
.step{display:grid;grid-template-columns:26px 1fr auto auto;gap:10px;align-items:center;padding:9px 11px;border:1px solid var(--line);border-radius:11px;background:#f9fcff}
.step .name{font-family:var(--mono);font-weight:600}
.step .meta{font-family:var(--mono);color:var(--fg-dim);font-size:11.5px}
.badge{font-family:var(--mono);font-size:10.5px;padding:3px 8px;border-radius:999px;border:1px solid var(--line2);text-transform:uppercase;letter-spacing:.4px}
.badge.succeeded{color:#157b4d;background:#e8f8ef;border-color:#bfead1}
.badge.failed,.badge.timeout{color:#b83d51;background:#fff0f1;border-color:#ffd9df}
.badge.running{color:#94620f;background:#fff4df;border-color:#f3dfb8}
.badge.skipped,.badge.unknown,.badge.pending{color:var(--fg-dim)}
.glyph{font-size:14px;text-align:center}
.k-ok{color:var(--ok)} .k-bad{color:var(--bad)} .k-warn{color:var(--warn)} .k-dim{color:var(--fg-dim)}
pre.diff{
  margin:0;padding:13px 15px;max-height:460px;overflow:auto;font-family:var(--mono);
  font-size:12px;line-height:1.5;background:#f7fbff;border-top:1px solid var(--line);
}
pre.diff .a{color:#157b4d;background:#e8f8ef}
pre.diff .d{color:#b83d51;background:#fff0f1}
pre.diff .h{color:var(--accent)}
pre.diff .m{color:#665ce7;font-weight:600}
pre.diff .c{color:#718bad}
.trace{font-family:var(--mono);font-size:12px}
.trace .row{display:grid;grid-template-columns:96px 158px 1fr;gap:10px;padding:6px 4px;border-bottom:1px solid var(--line)}
.trace .ts{color:var(--fg-dim)}
.trace .ev{color:var(--accent)}
.trace .det{color:#4d6b91;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tabs{display:flex;gap:6px;padding:11px 14px 0}
.tab{padding:7px 13px;border-radius:9px 9px 0 0;border:1px solid var(--line);border-bottom:none;background:#f3f8fd;font-family:var(--mono);font-size:12px;color:var(--fg-dim);cursor:pointer}
.tab.active{background:var(--panel-solid);color:var(--fg);border-color:var(--line2)}
.hide{display:none!important}
.empty{padding:34px 16px;text-align:center;color:var(--fg-dim);font-family:var(--mono);font-size:12.5px}
.chips{display:flex;gap:7px;flex-wrap:wrap;margin-top:9px}
.chip{font-family:var(--mono);font-size:11.5px;padding:4px 9px;border-radius:999px;border:1px solid var(--line2);color:var(--fg-dim)}
.chip b{color:var(--fg);font-weight:600}
.spark{width:100%;height:62px;display:block}
footer{margin-top:20px;color:var(--fg-dim);font-family:var(--mono);font-size:11.5px;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
"""

_JS = r"""
(function(){
  "use strict";
  const el = (id) => document.getElementById(id);
  let SNAP = window.__GF_SNAPSHOT__ || null;
  let auto = true;
  let timer = null;
  const canPoll = location.protocol === "http:" || location.protocol === "https:";

  const fmtInt = (n) => (Number(n)||0).toLocaleString("en-US");
  const usd = (micros) => {
    const v = (Number(micros)||0)/1e6;
    if (v === 0) return "$0.000000";
    return v < 0.01 ? "$" + v.toFixed(6) : "$" + v.toFixed(4);
  };
  const dur = (ms) => {
    const v = Number(ms)||0;
    if (!v) return "-";
    if (v < 1000) return Math.round(v) + " ms";
    if (v < 60000) return (v/1000).toFixed(2) + " s";
    const m = Math.floor(v/60000), s = ((v%60000)/1000).toFixed(1);
    return m + "m " + s + "s";
  };
  const pct = (r) => ((Number(r)||0)*100).toFixed(1) + "%";
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

  function statusPill(status){
    const s = String(status||"unknown").toLowerCase();
    const cls = ["succeeded","failed","running","skipped","pending","timeout"].includes(s) ? s : "";
    return '<span class="pill ' + (s==="succeeded"?"ok":(s==="failed"||s==="timeout")?"bad":s==="running"?"run":"") + '"><span class="dot"></span>' + esc(s) + "</span>";
  }

  function render(data){
    if (!data) return;
    SNAP = data;
    const k = data.kpis || {}, tok = k.tokens || {}, cost = k.cost || {}, cache = k.cache || {},
          rep = k.repair || {}, lat = k.latency || {}, health = k.health || {}, run = data.run || {},
          prog = data.progress || {};

    el("runid").textContent = (run.run_id || "no run recorded");
    el("status").innerHTML = statusPill(run.status) + '<span class="pill ' + (run.accepted ? "ok" : "") + '">accepted: ' + (run.accepted ? "yes" : "no") + "</span>";
    el("stamp").textContent = data.generated_at || "";
    el("srcnote").textContent = (data.source && data.source.db_present ? "db ok" : "db missing") + " · " + (data.source && data.source.trace_present ? "trace ok" : "trace missing");

    el("kpiTokens").textContent = fmtInt(tok.total);
    el("kpiTokensHint").textContent = "prompt " + fmtInt(tok.prompt) + " / completion " + fmtInt(tok.completion) + " · " + fmtInt(tok.calls) + " calls";
    el("kpiCost").textContent = cost.display || usd(cost.micros);
    el("kpiCostHint").textContent = "router p95 " + dur(lat.router_p95_ms);
    el("kpiCache").textContent = pct(cache.hit_rate);
    el("kpiCacheHint").textContent = fmtInt(cache.hits) + " hits / " + fmtInt(cache.misses) + " misses · " + fmtInt(cache.entries) + " entries";
    const rr = Number(rep.reject_rate)||0;
    el("kpiReject").textContent = pct(rr);
    el("kpiRejectHint").textContent = fmtInt(rep.rejections) + " of " + fmtInt(rep.proposals) + " proposals";
    el("kpiReject").parentNode.className = "card " + (rr > 0.25 ? "bad" : rr > 0 ? "warn" : "ok");
    el("kpiDuration").textContent = dur(run.duration_ms);
    el("kpiDurationHint").textContent = "p50 " + dur(lat.router_p50_ms) + " · p95 " + dur(lat.router_p95_ms);
    el("kpiProgress").textContent = (prog.done||0) + "/" + (prog.total||0);
    el("kpiProgressHint").textContent = (prog.percent||0) + "% · running " + (prog.running||0) + " · failed " + (prog.failed||0);
    el("kpiErrors").textContent = fmtInt((health.router_errors||0) + (health.breaker_rejected||0));
    el("kpiErrorsHint").textContent = "429s " + fmtInt(health.ratelimit_429) + " · subagents " + fmtInt(health.subagent_runs);
    el("kpiErrors").parentNode.className = "card " + ((health.router_errors||0) ? "warn" : "ok");

    el("progBar").style.width = ((prog.fraction||0)*100).toFixed(1) + "%";
    el("progText").textContent = (prog.done||0) + " of " + (prog.total||0) + " nodes complete" + (prog.failed ? " · " + prog.failed + " failed" : "");

    const hr = Number(cache.hit_rate)||0;
    el("cacheBar").className = "bar " + (hr >= 0.6 ? "ok" : hr >= 0.25 ? "warn" : "bad");
    el("cacheBar").firstElementChild.style.width = (hr*100).toFixed(1) + "%";
    el("cacheText").textContent = pct(cache.hit_rate);
    el("cacheMeta").textContent = "hits " + fmtInt(cache.hits) + " · misses " + fmtInt(cache.misses) + " · writes " + fmtInt(cache.writes) + " · " + fmtInt(cache.bytes||0) + " B";
    el("rejBar").className = "bar " + (rr === 0 ? "ok" : rr <= 0.25 ? "warn" : "bad");
    el("rejBar").firstElementChild.style.width = Math.min(100, rr*100).toFixed(1) + "%";
    el("rejText").textContent = pct(rep.reject_rate);
    el("rejMeta").textContent = "rejections " + fmtInt(rep.rejections) + " / " + fmtInt(rep.proposals) + " · fix-loop runs " + fmtInt(rep.fixloop_runs) + " · avg iterations " + (rep.avg_iterations||0);

    const ns = cache.namespaces || {};
    el("nsChips").innerHTML = Object.keys(ns).length
      ? Object.keys(ns).map((n) => '<span class="chip">' + esc(n) + " <b>" + fmtInt(ns[n]) + "</b></span>").join("")
      : '<span class="chip">no cache entries</span>';

    const steps = data.steps || [];
    el("steps").innerHTML = steps.length ? steps.map((s) =>
      '<div class="step">' +
        '<div class="glyph ' + (s.status==="succeeded"?"k-ok":(s.status==="failed"?"k-bad":(s.status==="running"?"k-warn":"k-dim"))) + '">' +
          (s.status==="succeeded"?"✓":s.status==="failed"?"✗":s.status==="running"?"▶":"·") + "</div>" +
        '<div><div class="name">' + esc(s.node) + '</div><div class="meta">' + esc(s.started_at||"") + (s.error_msg ? " · " + esc(s.error_msg) : "") + "</div></div>" +
        '<div class="meta">' + dur(s.duration_ms) + (s.attempts > 1 ? " · " + s.attempts + "×" : "") + "</div>" +
        '<div><span class="badge ' + esc(s.status) + '">' + esc(s.status) + "</span></div>" +
      "</div>").join("") : '<div class="empty">no node journal yet — run the pipeline once</div>';

    const phases = data.phases || [];
    el("phases").innerHTML = '<table><thead><tr><th>phase</th><th>model</th><th class="num">calls</th><th class="num">tokens</th><th class="num">cost</th><th class="num">p95</th><th class="num">errors</th></tr></thead><tbody>' +
      (phases.length ? phases.map((p) =>
        "<tr><td>" + esc(p.phase) + "</td><td>" + esc(p.model||"-") + '</td><td class="num">' + fmtInt(p.calls) +
        '</td><td class="num">' + fmtInt(p.tokens) + '</td><td class="num">' + usd(p.cost_micros) +
        '</td><td class="num">' + dur(p.latency_p95_ms) + '</td><td class="num">' + fmtInt(p.errors) + "</td></tr>").join("")
        : '<tr><td colspan="7" class="empty">no router metrics recorded</td></tr>') + "</tbody></table>";

    const models = data.models || [];
    el("models").innerHTML = '<table><thead><tr><th>model</th><th class="num">calls</th></tr></thead><tbody>' +
      (models.length ? models.map((m) => "<tr><td>" + esc(m.model) + '</td><td class="num">' + fmtInt(m.calls) + "</td></tr>").join("")
        : '<tr><td colspan="2" class="empty">no model calls recorded</td></tr>') + "</tbody></table>";

    const counts = data.trace_counts || {};
    el("traceCounts").innerHTML = Object.keys(counts).length
      ? Object.keys(counts).map((n) => '<span class="chip">' + esc(n) + " <b>" + fmtInt(counts[n]) + "</b></span>").join("")
      : '<span class="chip">trace empty</span>';
    renderTrace();

    renderDiff(data.diff || {});
    renderSpark(data.series || null);
    const idx = data.index || {};
    el("indexInfo").innerHTML = idx.present
      ? '<span class="chip">files <b>' + fmtInt(idx.files) + '</b></span><span class="chip">symbols <b>' + fmtInt(idx.symbols) + "</b></span>" +
        Object.keys(idx.languages||{}).map((l) => '<span class="chip">' + esc(l) + " <b>" + fmtInt(idx.languages[l]) + "</b></span>").join("")
      : '<span class="chip">no L0 index database</span>';
    const notes = (data.source && data.source.missing) || [];
    el("notes").innerHTML = notes.length ? notes.map((n) => "! " + esc(n)).join("<br>") : "data sources ok";
    const totals = data.totals || {};
    el("totals").textContent = fmtInt(totals.runs_recorded) + " runs · " + fmtInt(totals.metrics_points) + " metric points · " + fmtInt(totals.trace_events) + " trace events";
  }

  function renderTrace(){
    const q = (el("traceFilter").value || "").toLowerCase();
    const rows = (SNAP.trace || []).filter((e) => {
      if (!q) return true;
      return JSON.stringify(e).toLowerCase().indexOf(q) >= 0;
    });
    el("trace").innerHTML = rows.length ? rows.map((e) =>
      '<div class="row"><div class="ts">' + esc(e.ts||"") + '</div><div class="ev">' + esc(e.event||"") + '</div><div class="det">' + esc(
        [e.node,e.phase,e.model,e.status,e.code,e.error,(e.tokens!=null?"tok="+e.tokens:""),(e.duration_ms!=null?"dur="+dur(e.duration_ms):"")]
          .filter(Boolean).join(" · ")) + "</div></div>").join("")
      : '<div class="empty">no trace events' + (q ? " matching “" + esc(q) + "”" : "") + "</div>";
    el("traceShown").textContent = rows.length + " / " + (SNAP.trace || []).length + " events";
  }

  function renderDiff(d){
    const files = d.files || [];
    el("diffMeta").textContent = (d.hunks||0) + " hunks · +" + (d.added||0) + " / −" + (d.removed||0) + " · " + files.length + " file(s)";
    el("diffFiles").innerHTML = files.length ? files.map((f) => '<span class="chip">' + esc(f) + "</span>").join("") : "";
    const text = d.text || "";
    if (!text){
      el("diff").innerHTML = '<div class="empty">no diff recorded for this run</div>';
      return;
    }
    const out = text.split("\n").map((line) => {
      let cls = "c";
      if (line.indexOf("diff --git") === 0 || line.indexOf("+++") === 0 || line.indexOf("---") === 0) cls = "m";
      else if (line.indexOf("@@") === 0) cls = "h";
      else if (line.charAt(0) === "+") cls = "a";
      else if (line.charAt(0) === "-") cls = "d";
      return '<span class="' + cls + '">' + esc(line) + "</span>";
    }).join("\n");
    el("diff").innerHTML = out + (d.truncated ? '\n<span class="k-dim">… truncated</span>' : "");
  }

  function renderSpark(series){
    const host = el("spark");
    if (!series){ host.innerHTML = '<span class="k-dim">no series</span>'; return; }
    const values = (series || []).map((p) => Number(p.value)||0);
    if (values.length < 2){ host.innerHTML = '<span class="k-dim">not enough points for a trend</span>'; return; }
    const w = 620, h = 56, max = Math.max.apply(null, values), min = Math.min.apply(null, values);
    const span = (max - min) || 1;
    const pts = values.map((v, i) => {
      const x = (i/(values.length-1))*w;
      const y = h - ((v-min)/span)*(h-10) - 5;
      return x.toFixed(1) + "," + y.toFixed(1);
    }).join(" ");
    host.innerHTML = '<svg class="spark" viewBox="0 0 ' + w + " " + h + '" preserveAspectRatio="none">' +
      '<polyline fill="none" stroke="url(#g)" stroke-width="2" points="' + pts + '"/>' +
      '<defs><linearGradient id="g" x1="0" x2="1"><stop offset="0" stop-color="#4cc9f0"/><stop offset="1" stop-color="#7b61ff"/></linearGradient></defs>' +
      "</svg>";
  }

  function tab(name){
    ["overview","steps","trace","diff"].forEach((t) => {
      el("tab-" + t).className = "tab" + (t === name ? " active" : "");
      el("view-" + t).className = (t === name ? "" : "hide");
    });
  }

  function tick(){
    // Only a served page has the snapshot endpoint; on a file:// page the fetch
    // would fail with a console error, so polling is disabled there and the
    // embedded snapshot stays authoritative.
    if (!auto || !canPoll) return;
    fetch("api/snapshot", {cache:"no-store"}).then((r) => (r.ok ? r.json() : null)).then((d) => { if (d) render(d); }).catch(() => {});
  }

  function boot(){
    render(SNAP);
    el("tab-overview").onclick = () => tab("overview");
    el("tab-steps").onclick = () => tab("steps");
    el("tab-trace").onclick = () => tab("trace");
    el("tab-diff").onclick = () => tab("diff");
    el("traceFilter").oninput = renderTrace;
    el("refresh").onclick = tick;
    el("auto").onclick = () => {
      auto = !auto;
      el("auto").textContent = auto ? "auto-refresh: on" : "auto-refresh: off";
    };
    timer = setInterval(tick, 2000);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
"""


def render_html(
    snapshot: Mapping[str, Any],
    *,
    title: str = PAGE_TITLE,
    live: bool = False,
) -> str:
    """The whole dashboard as one self-contained HTML document.

    ``live=True`` keeps the polling loop pointed at ``api/snapshot``; when the
    page is written to a file that endpoint does not exist, so the fetch fails
    silently and the embedded snapshot stays authoritative.
    """
    run = snapshot.get("run") or {}
    payload = _embed_json(snapshot)
    node_hint = ", ".join(NODE_ORDER)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="GigaForge V6.fresh control room — pipeline progress, token and cost metrics, cache hit rate, reject rate, step trace and diff viewer.">
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div class="brand">
      <div class="logo">GF</div>
      <div>
        <h1>GigaForge V6.fresh · Control Room</h1>
        <div class="sub" id="runid">{run.get('run_id') or 'no run recorded'}</div>
      </div>
    </div>
    <div class="controls">
      <span id="status"></span>
      <span class="pill" id="stamp"></span>
      <span class="pill"><span class="dot"></span><span id="srcnote"></span></span>
      <input id="traceFilter" type="search" placeholder="filter trace…" aria-label="filter trace events">
      <button id="auto" title="toggle polling">auto-refresh: on</button>
      <button id="refresh" title="refresh now">refresh</button>
    </div>
  </header>

  <section class="grid kpis" aria-label="key metrics">
    <div class="card acc"><div class="label">tokens</div><div class="value" id="kpiTokens">0</div><div class="hint" id="kpiTokensHint">-</div></div>
    <div class="card mag"><div class="label">cost</div><div class="value" id="kpiCost">$0.000000</div><div class="hint" id="kpiCostHint">-</div></div>
    <div class="card ok"><div class="label">cache hit rate</div><div class="value" id="kpiCache">0.0%</div><div class="hint" id="kpiCacheHint">-</div></div>
    <div class="card"><div class="label">reject rate</div><div class="value" id="kpiReject">0.0%</div><div class="hint" id="kpiRejectHint">-</div></div>
    <div class="card"><div class="label">duration</div><div class="value" id="kpiDuration">-</div><div class="hint" id="kpiDurationHint">-</div></div>
    <div class="card"><div class="label">nodes done</div><div class="value" id="kpiProgress">0/0</div><div class="hint" id="kpiProgressHint">-</div></div>
    <div class="card"><div class="label">error signals</div><div class="value" id="kpiErrors">0</div><div class="hint" id="kpiErrorsHint">-</div></div>
  </section>

  <div class="panel" style="margin-bottom:14px">
    <h2>pipeline progress<span class="k-dim" style="font-size:11px">node order: {node_hint}</span></h2>
    <div class="body">
      <div class="bar"><i id="progBar" style="width:0%"></i></div>
      <div class="hint k-dim" id="progText" style="margin-top:8px;font-family:var(--mono);font-size:12px"></div>
    </div>
  </div>

  <div class="tabs">
    <div class="tab active" id="tab-overview">Overview</div>
    <div class="tab" id="tab-steps">Steps</div>
    <div class="tab" id="tab-trace">Trace</div>
    <div class="tab" id="tab-diff">Diff</div>
  </div>

  <div id="view-overview" class="cols">
    <div class="panel">
      <h2>cost &amp; usage</h2>
      <div class="body">
        <div class="meter">
          <div class="top"><span>cache hit rate</span><b id="cacheText">0.0%</b></div>
          <div class="bar" id="cacheBar"><i style="width:0%"></i></div>
          <div class="hint k-dim" id="cacheMeta" style="margin-top:6px;font-family:var(--mono);font-size:11.5px"></div>
        </div>
        <div class="meter">
          <div class="top"><span>reject rate</span><b id="rejText">0.0%</b></div>
          <div class="bar" id="rejBar"><i style="width:0%"></i></div>
          <div class="hint k-dim" id="rejMeta" style="margin-top:6px;font-family:var(--mono);font-size:11.5px"></div>
        </div>
        <div class="chips" id="nsChips"></div>
      </div>
    </div>
    <div class="panel">
      <h2>token trend</h2>
      <div class="body" id="spark"><span class="k-dim">no series</span></div>
    </div>
    <div class="panel" style="grid-column:1/-1">
      <h2>per-phase breakdown</h2>
      <div class="body" style="padding:0"><div class="scroll" id="phases"></div></div>
    </div>
    <div class="panel">
      <h2>models</h2>
      <div class="body" style="padding:0"><div class="scroll" id="models"></div></div>
    </div>
    <div class="panel">
      <h2>L0 index</h2>
      <div class="body"><div class="chips" id="indexInfo"></div><div class="hint k-dim" id="notes" style="margin-top:10px;font-family:var(--mono);font-size:11.5px"></div></div>
    </div>
  </div>

  <div id="view-steps" class="hide">
    <div class="panel"><h2>step timeline</h2><div class="body"><div class="steps" id="steps"></div></div></div>
  </div>

  <div id="view-trace" class="hide">
    <div class="panel">
      <h2>trace viewer<span class="k-dim" id="traceShown" style="font-size:11px"></span></h2>
      <div class="body"><div class="chips" id="traceCounts"></div></div>
      <div class="body trace" style="padding-top:0"><div id="trace"></div></div>
    </div>
  </div>

  <div id="view-diff" class="hide">
    <div class="panel">
      <h2>diff viewer<span class="k-dim" id="diffMeta" style="font-size:11px"></span></h2>
      <div class="body"><div class="chips" id="diffFiles"></div></div>
      <pre class="diff" id="diff"></pre>
    </div>
  </div>

  <footer>
    <span id="totals"></span>
    <span>self-contained panel · no external requests · snapshot embedded as application/json</span>
  </footer>
</div>
<script type="application/json" id="gf-snapshot">{payload}</script>
<script>window.__GF_SNAPSHOT__ = JSON.parse(document.getElementById("gf-snapshot").textContent);</script>
<script>{_JS}</script>
</body>
</html>
"""


def write_html(
    store: UiStore,
    out_path: str | Path,
    *,
    run_id: Optional[str] = None,
    prices: Any = None,
    default_model: str = "GigaChat",
    include_series: bool = True,
) -> Path:
    """Build the snapshot and write the panel to ``out_path``."""
    from .snapshot import build_series

    snapshot = build_snapshot(store, run_id=run_id, prices=prices, default_model=default_model)
    if include_series:
        snapshot["series"] = build_series(store, limit=240).get("tokens", [])
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(snapshot), encoding="utf-8")
    return path


# ── live server ────────────────────────────────────────────────────────────
def make_handler(
    store: UiStore,
    *,
    run_id: Optional[str] = None,
    prices: Any = None,
    default_model: str = "GigaChat",
    chat_handler: Optional[Callable[[str], str]] = None,
) -> type[BaseHTTPRequestHandler]:
    """A small stdlib handler: the panel, the snapshot API and (optionally) chat."""

    def snapshot_dict() -> dict[str, Any]:
        from .snapshot import build_series

        snap = build_snapshot(store, run_id=run_id, prices=prices, default_model=default_model)
        snap["series"] = build_series(store, limit=240).get("tokens", [])
        return snap

    class Handler(BaseHTTPRequestHandler):
        server_version = "GigaForgeUI/2.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # keep the console clean
            return

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
                pass

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path in {"/", "/index.html", "/dashboard"}:
                html = render_html(snapshot_dict(), live=True)
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/snapshot":
                self._send(200, _embed_json(snapshot_dict()).encode("utf-8"), "application/json")
            elif path == "/api/events":
                self._send(200, _sse_body(snapshot_dict).encode("utf-8"), "text/event-stream")
            elif path == "/api/health":
                self._send(200, b'{"ok":true}', "application/json")
            else:
                self._send(404, b'{"error":"not found"}', "application/json")

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/")
            if path != "/api/chat" or chat_handler is None:
                self._send(404, b'{"error":"not found"}', "application/json")
                return
            local_address = str(self.connection.getsockname()[0])
            if not is_loopback_host(local_address):
                self._send(403, b'{"error":"chat is available on loopback only"}',
                           "application/json")
                return
            origin = self.headers.get("Origin")
            if origin:
                parsed_origin = urlsplit(origin)
                request_host = str(self.headers.get("Host") or "").lower()
                if (
                    parsed_origin.scheme != "http"
                    or not is_loopback_host(parsed_origin.hostname or "")
                    or parsed_origin.netloc.lower() != request_host
                ):
                    self._send(403, b'{"error":"cross-origin chat requests are not allowed"}',
                               "application/json")
                    return
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                self._send(415, b'{"error":"application/json is required"}',
                           "application/json")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0:
                self._send(400, b'{"error":"a JSON message is required"}', "application/json")
                return
            if length > 64_000:
                self._send(413, b'{"error":"request body is too large"}', "application/json")
                return
            body = self.rfile.read(length).decode("utf-8", "replace")
            try:
                payload = json.loads(body)
            except ValueError:
                payload = None
            if not isinstance(payload, dict):
                self._send(400, b'{"error":"invalid JSON object"}', "application/json")
                return
            message = payload.get("message")
            if not isinstance(message, str) or not message.strip():
                self._send(400, b'{"error":"message must be a non-empty string"}',
                           "application/json")
                return
            try:
                reply = chat_handler(message.strip())
            except Exception as exc:  # pragma: no cover - defensive
                reply = f"error: {exc}"
            self._send(200, json.dumps({"reply": reply}).encode("utf-8"), "application/json")

    return Handler


def _sse_body(snapshot_dict: Callable[[], Mapping[str, Any]]) -> str:
    """Build an SSE frame with the current snapshot as its JSON data line."""
    data = _embed_json(snapshot_dict()).replace("\n", "")
    return f"event: snapshot\ndata: {data}\n\n"


def serve(
    store: UiStore,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    run_id: Optional[str] = None,
    prices: Any = None,
    default_model: str = "GigaChat",
    chat_handler: Optional[Callable[[str], str]] = None,
    open_browser: bool = False,
    block: bool = True,
) -> ThreadingHTTPServer:
    """Start the control-room server (returns the instance so tests can stop it)."""
    # A network-visible control room is deliberately read-only. Chat can run
    # arbitrary repository tasks and remains a loopback-only capability.
    if not is_loopback_host(host):
        chat_handler = None
    handler = make_handler(
        store,
        run_id=run_id,
        prices=prices,
        default_model=default_model,
        chat_handler=chat_handler,
    )
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}/"
    if open_browser:  # pragma: no cover - interactive
        import webbrowser

        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    if block:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:  # pragma: no cover
            pass
    return httpd


__all__ = [
    "PAGE_TITLE",
    "make_handler",
    "render_html",
    "serve",
    "write_html",
]
