"""GET /dashboard — a self-contained, read-only HTML view of the proxy's own state.

ZERO NEW DEPENDENCIES. The repo has no jinja2 and no StaticFiles, and keeps it that way:
one HTML document from one route, inline CSS and JS, no build step and no CDN fetch. A
dashboard that adds a Node toolchain to a Python proxy is a worse trade than no
dashboard, and a CDN fetch makes an air-gapped deployment render blank.

READ-ONLY. Nothing here calls /queue/pause, /queue/resume, /queue/drain or /queue/flush.
Those need `management`, and this page is reachable with `read` — so a control would
either be dead UI for most callers or a privilege escalation for the rest. A control
surface is a separate feature with its own audit.

NOT AT "/". That path is in the metadata fast-path list and is proxied to Ollama, where
it answers "Ollama is running"; taking it would break every Ollama-compat client that
probes the root.

AUTHENTICATION, AND WHY THE PAGE FETCHES RELATIVE URLS. A browser cannot set an
Authorization header on a top-level navigation. With `auth.enabled: true` the page is
therefore reachable only through something that supplies the credential — a reverse
proxy injecting the header, or a session cookie from a forward-auth layer. The in-page
polls are same-origin relative URLs sent with `credentials: same-origin`, so whatever
authenticated the document authenticates the polls, with no key embedded in the HTML.
With auth off (the default) it simply works in a browser. Either way, no credential is
ever written into this page.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..auth import require_scope

if TYPE_CHECKING:
    from ..main import AppState

router = APIRouter()


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    state: AppState = request.app.state.oqp

    # 404 when disabled, NOT 401/403, and checked before authentication. A disabled
    # feature should not confirm it exists to a caller who cannot use it, and answering
    # 401 first would tell an unauthenticated prober that a dashboard is there to be
    # credentialed into.
    if not state.config.dashboard.enabled:
        return JSONResponse(status_code=404, content={"detail": "Not Found"})

    err = await require_scope(request, "read")
    if err:
        return err

    return HTMLResponse(_PAGE.replace("__REFRESH_MS__", json.dumps(
        state.config.dashboard.refresh_seconds * 1000
    )))


# The single interpolation into this document is the refresh interval, substituted as a
# JSON number from a pydantic-validated int. Everything else is static. Every value that
# comes from an API response is written with textContent — never innerHTML — because
# client_id, host name and model names are operator-supplied config strings that reach
# this page verbatim.
_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>ollama-queue-proxy</title>
<style>
  :root {
    --bg: #fbfbfa; --fg: #1a1a19; --muted: #6b6b68; --line: #e0e0dd;
    --card: #ffffff; --ok: #2e7d32; --bad: #c62828; --warn: #ef6c00;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #16171a; --fg: #e6e6e3; --muted: #9a9a96; --line: #2c2e33;
      --card: #1d1f23; --ok: #66bb6a; --bad: #ef5350; --warn: #ffa726;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 1.5rem; background: var(--bg); color: var(--fg);
    font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  header { display: flex; align-items: baseline; gap: 1rem; flex-wrap: wrap;
           margin-bottom: 1.25rem; }
  h1 { font-size: 1.1rem; font-weight: 600; margin: 0; letter-spacing: -0.01em; }
  h2 { font-size: 0.8rem; font-weight: 600; text-transform: uppercase;
       letter-spacing: 0.06em; color: var(--muted); margin: 1.75rem 0 0.6rem; }
  #meta { font-size: 0.8rem; color: var(--muted); margin-left: auto; }
  #err { display: none; border: 1px solid var(--bad); color: var(--bad);
         border-radius: 6px; padding: 0.6rem 0.8rem; margin-bottom: 1rem;
         font-size: 0.85rem; }
  .tiles { display: grid; gap: 0.75rem;
           grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); }
  .tile { background: var(--card); border: 1px solid var(--line); border-radius: 8px;
          padding: 0.8rem 0.9rem; }
  .tile .k { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em;
             color: var(--muted); }
  .tile .v { font-size: 1.7rem; font-weight: 600; font-variant-numeric: tabular-nums;
             letter-spacing: -0.02em; margin-top: 0.15rem; }
  table { width: 100%; border-collapse: collapse; background: var(--card);
          border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
  th, td { text-align: left; padding: 0.5rem 0.75rem; border-bottom: 1px solid var(--line);
           font-size: 0.88rem; }
  th { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.05em;
       color: var(--muted); font-weight: 600; }
  tr:last-child td { border-bottom: none; }
  td.n { text-align: right; font-variant-numeric: tabular-nums; }
  .ok { color: var(--ok); } .bad { color: var(--bad); }
  .empty { color: var(--muted); font-style: italic; }
  footer { margin-top: 2rem; font-size: 0.75rem; color: var(--muted); }
</style>
</head>
<body>
<header>
  <h1>ollama-queue-proxy</h1>
  <span id="meta">connecting</span>
</header>

<div id="err"></div>

<div class="tiles" id="tiles"></div>

<h2>Queue tiers</h2>
<table><thead><tr>
  <th>Tier</th><th class="n">Depth</th><th class="n">Max</th>
  <th class="n">Processed</th><th class="n">Rejected</th><th class="n">Expired</th>
</tr></thead><tbody id="tiers"></tbody></table>

<h2>Hosts</h2>
<table><thead><tr>
  <th>Host</th><th>State</th><th class="n">Models</th>
  <th class="n">Requests</th><th class="n">Failures</th><th>Last checked</th>
</tr></thead><tbody id="hosts"></tbody></table>

<h2>Clients</h2>
<table><thead><tr>
  <th>Client</th><th>Description</th><th class="n">Processed</th><th class="n">Rejected</th>
</tr></thead><tbody id="clients"></tbody></table>

<footer>Read-only view. Pause, resume, drain and flush require a management key and are
not exposed here.</footer>

<script>
"use strict";
var REFRESH_MS = __REFRESH_MS__;
var timer = null;

// Every cell goes through here. textContent, never innerHTML: client_id, host name and
// model names are operator-supplied strings that arrive verbatim from the API, and this
// is the only thing standing between a config file and script execution in this page.
function cell(row, value, numeric, className) {
  var td = document.createElement("td");
  td.textContent = value === null || value === undefined ? "\\u2014" : String(value);
  if (numeric) { td.className = "n"; }
  if (className) { td.className = (td.className ? td.className + " " : "") + className; }
  row.appendChild(td);
  return td;
}

function emptyRow(tbody, colspan, text) {
  var tr = document.createElement("tr");
  var td = document.createElement("td");
  td.colSpan = colspan;
  td.className = "empty";
  td.textContent = text;
  tr.appendChild(td);
  tbody.appendChild(tr);
}

function tile(key, value) {
  var d = document.createElement("div");
  d.className = "tile";
  var k = document.createElement("div");
  k.className = "k";
  k.textContent = key;
  var v = document.createElement("div");
  v.className = "v";
  v.textContent = value;
  d.appendChild(k);
  d.appendChild(v);
  return d;
}

function duration(total) {
  if (typeof total !== "number" || !isFinite(total) || total < 0) { return "\\u2014"; }
  var s = Math.floor(total);
  var d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
  var m = Math.floor((s % 3600) / 60);
  if (d > 0) { return d + "d " + h + "h"; }
  if (h > 0) { return h + "h " + m + "m"; }
  if (m > 0) { return m + "m " + (s % 60) + "s"; }
  return s + "s";
}

function renderSummary(s) {
  var t = document.getElementById("tiles");
  t.textContent = "";
  t.appendChild(tile("Queued", s.queued));
  t.appendChild(tile("Active", s.active + " / " + s.max_concurrent));
  t.appendChild(tile("Hosts up", s.hosts_healthy + " / " + s.hosts_total));
  t.appendChild(tile("Processed", s.processed));
  t.appendChild(tile("Rejected", s.rejected));
  t.appendChild(tile("Expired", s.expired));
  t.appendChild(tile("Uptime", duration(s.uptime_seconds)));
}

function renderStatus(st) {
  var tiers = document.getElementById("tiers");
  tiers.textContent = "";
  ["high", "normal", "low"].forEach(function (name) {
    var q = (st.queue || {})[name];
    if (!q) { return; }
    var tr = document.createElement("tr");
    cell(tr, name);
    cell(tr, q.depth, true);
    cell(tr, q.max_depth, true);
    cell(tr, q.processed, true);
    cell(tr, q.rejected, true);
    cell(tr, q.expired, true);
    tiers.appendChild(tr);
  });

  var hosts = document.getElementById("hosts");
  hosts.textContent = "";
  var hl = st.hosts || [];
  if (hl.length === 0) {
    emptyRow(hosts, 6, "no hosts configured");
  } else {
    hl.forEach(function (h) {
      var tr = document.createElement("tr");
      cell(tr, h.name);
      cell(tr, h.healthy ? "healthy" : "unreachable", false, h.healthy ? "ok" : "bad");
      cell(tr, (h.models || []).length, true);
      cell(tr, h.requests_handled, true);
      cell(tr, h.failures, true);
      cell(tr, h.last_checked);
      hosts.appendChild(tr);
    });
  }

  var clients = document.getElementById("clients");
  clients.textContent = "";
  var ids = Object.keys(st.clients || {});
  if (ids.length === 0) {
    emptyRow(clients, 4, "no client activity recorded");
  } else {
    ids.sort().forEach(function (id) {
      var c = st.clients[id];
      var tr = document.createElement("tr");
      cell(tr, id);
      cell(tr, c.description);
      cell(tr, c.processed, true);
      cell(tr, c.rejected, true);
      clients.appendChild(tr);
    });
  }
}

function fail(message) {
  var e = document.getElementById("err");
  e.textContent = message;
  e.style.display = "block";
  document.getElementById("meta").textContent = "disconnected";
}

function refresh() {
  // Same-origin and relative, sent with credentials: whatever authenticated the page
  // authenticates these, so no key is ever embedded in the document.
  var opts = { credentials: "same-origin", headers: { "Accept": "application/json" } };
  Promise.all([
    fetch("queue/summary", opts).then(function (r) {
      if (!r.ok) { throw new Error("summary: HTTP " + r.status); }
      return r.json();
    }),
    fetch("queue/status", opts).then(function (r) {
      if (!r.ok) { throw new Error("status: HTTP " + r.status); }
      return r.json();
    })
  ]).then(function (both) {
    renderSummary(both[0]);
    renderStatus(both[1]);
    document.getElementById("err").style.display = "none";
    document.getElementById("meta").textContent =
      "updated " + new Date().toLocaleTimeString();
  }).catch(function (e) {
    fail(String(e && e.message ? e.message : e));
  });
}

// Stop polling while the tab is hidden. A dashboard left open on a second monitor
// otherwise keeps a request every few seconds against a proxy whose whole purpose is
// rationing a scarce resource.
function schedule() {
  if (timer !== null) { clearInterval(timer); timer = null; }
  if (!document.hidden) {
    refresh();
    timer = setInterval(refresh, REFRESH_MS);
  }
}

document.addEventListener("visibilitychange", schedule);
schedule();
</script>
</body>
</html>
"""
