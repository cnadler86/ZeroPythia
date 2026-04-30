"""FastAPI dashboard server.

HTTP + WebSocket endpoints:
  GET  /               – HTML dashboard GUI
  GET  /api/state      – current DashboardState as JSON
  GET  /api/regulators – list all registered regulators
  POST /api/mode       – set operating mode
  POST /api/regulators/select        – select active regulator
  POST /api/regulators/{name}/settings – update regulator settings
  WS   /ws             – live state stream (DashboardState JSON, ~1 s)

The server receives a ``ControlRuntime`` instance from the entry point and
registers/removes WebSocket callbacks on connect/disconnect.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from .models import (
    AutoConnectCommand,
    DashboardState,
    SelectRegulatorCommand,
    SetModeCommand,
)
from .runtime import ControlRuntime

logger = logging.getLogger(__name__)


# ── HTML GUI (single-file, no build step) ────────────────────────────────────

_HTML = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Zendure Dashboard</title>
<style>
  :root {
    --bg: #0f1117; --card: #1c1f2e; --border: #2a2d3e;
    --accent: #4f8ef7; --green: #22c55e; --red: #ef4444;
    --yellow: #eab308; --text: #e2e8f0; --muted: #64748b;
    --font: system-ui, -apple-system, sans-serif;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: var(--font); font-size: 14px; }
  h1 { font-size: 1.15rem; font-weight: 600; }
  h2 { font-size: 0.78rem; font-weight: 600; color: var(--muted); text-transform: uppercase;
       letter-spacing: .05em; margin-bottom: .5rem; }
  /* ── Header ─────────────────────────────────── */
  .header { display: flex; align-items: center; gap: 10px; padding: 10px 16px;
            border-bottom: 1px solid var(--border); flex-wrap: wrap; }
  .header-metrics { display: flex; gap: 16px; flex-wrap: wrap; margin-left: auto; }
  .metric { display: flex; align-items: center; gap: 4px; font-size: 12px; }
  .metric-icon { font-size: 13px; }
  .metric-val { font-weight: 700; font-variant-numeric: tabular-nums; min-width: 46px; font-size: 12px; }
  .metric-label { color: var(--muted); font-size: 10px; }
  /* ── Layout grid ────────────────────────────── */
  .layout-live { display: grid; grid-template-columns: repeat(auto-fill, minmax(270px, 1fr));
            gap: 10px; padding: 12px 12px 0; max-width: 1500px; margin: 0 auto; }
  .layout-settings { display: grid; grid-template-columns: repeat(auto-fill, minmax(270px, 1fr));
            gap: 10px; padding: 8px 12px 12px; max-width: 1500px; margin: 0 auto; }
  .card { background: var(--card); border: 1px solid var(--border);
          border-radius: 10px; padding: 12px; }
  /* ── Misc helpers ───────────────────────────── */
  .dot { width:9px; height:9px; border-radius:50%; background:var(--muted); flex-shrink:0; }
  .dot.live { background:var(--green); box-shadow: 0 0 5px var(--green); }
  .dot.error { background:var(--red); }
  .row { display: flex; justify-content: space-between; align-items: center;
         padding: 3px 0; border-bottom: 1px solid var(--border); }
  .row:last-child { border-bottom: none; }
  .label { color: var(--muted); font-size: 12px; }
  .value { font-weight: 600; font-variant-numeric: tabular-nums; font-size: 13px; }
  .badge { display: inline-block; padding: 2px 7px; border-radius: 4px;
           font-size: 11px; font-weight: 700; }
  .badge-green { background: #14532d; color: var(--green); }
  .badge-red   { background: #450a0a; color: var(--red); }
  .badge-yellow{ background: #422006; color: var(--yellow); }
  .badge-blue  { background: #1e3a5f; color: var(--accent); }
  .badge-gray  { background: #1e293b; color: var(--muted); }
  .mode-btn { padding: 7px 12px; border: 1px solid var(--border); border-radius: 6px;
              background: var(--card); color: var(--text); cursor: pointer;
              font-size: 12px; transition: all .15s; }
  .mode-btn:hover { border-color: var(--accent); color: var(--accent); }
  .mode-btn.active { background: var(--accent); border-color: var(--accent); color:#fff; }
  .btn-row { display: flex; gap: 7px; flex-wrap: wrap; margin-top: 8px; }
  input[type=number], input[type=text], select {
    background: var(--bg); border: 1px solid var(--border); color: var(--text);
    border-radius: 6px; padding: 5px 8px; width: 100%; font-size: 12px;
  }
  input:focus, select:focus { outline: none; border-color: var(--accent); }
  label { display: block; color: var(--muted); font-size: 11px; margin: 6px 0 2px; }
  .apply-btn { margin-top: 10px; padding: 7px 14px; background: var(--accent);
               border: none; border-radius: 6px; color: #fff;
               cursor: pointer; font-size: 12px; width: 100%; }
  .apply-btn:hover { opacity: .85; }
  .apply-btn.secondary { background: #374151; }
  .apply-btn.secondary:hover { background: #4b5563; }
  /* ── Mini chart ─────────────────────────────── */
  .chart-wrap { width: 100%; height: 80px; overflow: hidden; margin-top: 8px; }
  canvas { width: 100%; height: 100%; display: block; }
  /* ── Per-phase card ─────────────────────────── */
  .phase-header { display: flex; align-items: center; justify-content: space-between;
                  margin-bottom: 6px; }
  .osc-inline { display: flex; align-items: center; gap: 5px; font-size: 12px; }
  /* ── Plan list ──────────────────────────────── */
  .plan-entry { display: flex; align-items: center; gap: 8px; padding: 4px 0;
                border-bottom: 1px solid var(--border); }
  .plan-entry:last-child { border-bottom: none; }
  .plan-time { font-variant-numeric: tabular-nums; font-size: 11px; color: var(--muted); min-width: 90px; }
  .plan-mode { font-weight: 600; flex: 1; font-size: 12px; }
  .plan-pwr  { font-size: 11px; color: var(--muted); }
  /* ── Toast ──────────────────────────────────── */
  #toast { position: fixed; bottom: 18px; right: 18px; background: #22c55e; color: #fff;
           padding: 9px 16px; border-radius: 8px; font-size: 13px; display: none; z-index: 99; }
</style>
</head>
<body>

<!-- ── Header ─────────────────────────────────────────────────────────────── -->
<div class="header">
  <div class="dot" id="ws-dot"></div>
  <h1>Zendure Dashboard</h1>
  <span id="mode-badge" class="badge badge-gray" style="font-size:12px">–</span>
  <span id="zfi-pause-badge" class="badge badge-yellow" style="display:none">⏸ Pausiert</span>
  <div class="header-metrics">
    <div class="metric">
      <span class="metric-icon">⚡</span>
      <div>
        <div class="metric-label">Netz gesamt</div>
        <div class="metric-val" id="hm-total">–</div>
      </div>
    </div>
    <div class="metric">
      <span class="metric-icon">🏠</span>
      <div>
        <div class="metric-label">Verbrauch</div>
        <div class="metric-val" id="hm-cons">–</div>
      </div>
    </div>
    <div class="metric">
      <span class="metric-icon">🔋</span>
      <div>
        <div class="metric-label">Batterie</div>
        <div class="metric-val" id="hm-batt">–</div>
      </div>
    </div>
    <div class="metric">
      <span class="metric-icon">📊</span>
      <div>
        <div class="metric-label">SoC</div>
        <div class="metric-val" id="hm-soc">–</div>
      </div>
    </div>
  </div>
  <span id="ts" style="color:var(--muted);font-size:11px;white-space:nowrap"></span>
</div>

<div class="layout-live">

  <!-- ── Betriebsmodus ─────────────────────────────────────────────────────── -->
  <div class="card" id="card-mode">
    <h2>Betriebsmodus</h2>
    <div class="btn-row">
      <button class="mode-btn" id="btn-idle" onclick="setMode('idle')">Idle</button>
      <button class="mode-btn" id="btn-zf"   onclick="setMode('discharge_zero_feed')">Zero-Feed</button>
      <button class="mode-btn" id="btn-charge" onclick="openChargeDialog()">AC Laden ▸</button>
      <button class="mode-btn" id="btn-auto"   onclick="openAutoDialog()">Auto ▸</button>
    </div>
    <div id="charge-panel" style="display:none;margin-top:10px">
      <label>Ladeleistung [W]</label>
      <input type="number" id="charge-w" value="400" min="1" max="3000"/>
      <button class="apply-btn" onclick="setMode('ac_charge')">Laden starten</button>
    </div>
    <div id="auto-panel" style="display:none;margin-top:10px">
      <label>MQTT Broker URL</label>
      <input type="text" id="auto-broker" value="mqtt://localhost:1883"/>
      <label>Device ID</label>
      <input type="text" id="auto-device-id" value="SF800Pro"/>
      <button class="apply-btn" onclick="activateAuto()">Auto aktivieren</button>
      <button class="apply-btn secondary" style="margin-top:4px" onclick="deactivateAuto()">Deaktivieren</button>
    </div>
  </div>

  <!-- ── Phase A ────────────────────────────────────────────────────────────── -->
  <div class="card" id="card-live-A">
    <div class="phase-header">
      <h2>Phase A &nbsp;<span id="ph-role-A" class="badge badge-blue" style="font-size:10px">FF</span></h2>
      <div class="osc-inline">
        <span id="osc-badge-A" class="badge badge-gray" style="font-size:11px">–</span>
        <span id="osc-det-A" style="color:var(--accent);font-size:11px"></span>
      </div>
    </div>
    <div class="row"><span class="label">Netz</span><span class="value" id="ph-grid-A">–</span></div>
    <div class="row"><span class="label">Anforderung</span><span class="value" id="ph-sp-A">–</span></div>
    <div class="row" id="osc-lim-row-A" style="display:none"><span class="label">OSZ-Limit</span><span class="value" id="osc-lim-A" style="color:var(--yellow)">–</span></div>
    <div class="chart-wrap"><canvas id="chart-A"></canvas></div>
  </div>

  <!-- ── Phase B ────────────────────────────────────────────────────────────── -->
  <div class="card" id="card-live-B">
    <div class="phase-header">
      <h2>Phase B &nbsp;<span id="ph-role-B" class="badge badge-green" style="font-size:10px">Bat</span></h2>
      <div class="osc-inline">
        <span id="osc-badge-B" class="badge badge-gray" style="font-size:11px">–</span>
        <span id="osc-det-B" style="color:var(--accent);font-size:11px"></span>
      </div>
    </div>
    <div class="row"><span class="label">Netz</span><span class="value" id="ph-grid-B">–</span></div>
    <div class="row"><span class="label">Verbrauch (est.)</span><span class="value" id="ph-cons-B">–</span></div>
    <div class="row"><span class="label">Setpoint (FB)</span><span class="value" id="ph-sp-B">–</span></div>
    <div class="row" id="osc-lim-row-B" style="display:none"><span class="label">OSZ-Limit</span><span class="value" id="osc-lim-B" style="color:var(--yellow)">–</span></div>
    <div class="chart-wrap"><canvas id="chart-B"></canvas></div>
  </div>

  <!-- ── Phase C ────────────────────────────────────────────────────────────── -->
  <div class="card" id="card-live-C">
    <div class="phase-header">
      <h2>Phase C &nbsp;<span id="ph-role-C" class="badge badge-blue" style="font-size:10px">FF</span></h2>
      <div class="osc-inline">
        <span id="osc-badge-C" class="badge badge-gray" style="font-size:11px">–</span>
        <span id="osc-det-C" style="color:var(--accent);font-size:11px"></span>
      </div>
    </div>
    <div class="row"><span class="label">Netz</span><span class="value" id="ph-grid-C">–</span></div>
    <div class="row"><span class="label">Anforderung</span><span class="value" id="ph-sp-C">–</span></div>
    <div class="row" id="osc-lim-row-C" style="display:none"><span class="label">OSZ-Limit</span><span class="value" id="osc-lim-C" style="color:var(--yellow)">–</span></div>
    <div class="chart-wrap"><canvas id="chart-C"></canvas></div>
  </div>

  <!-- ── Globale Statistik + Gesamtplot ─────────────────────────────────────── -->
  <div class="card">
    <div class="phase-header">
      <h2>Gesamt &amp; Regler</h2>
      <span id="ctrl-status-badge" class="badge badge-gray" style="font-size:10px">–</span>
    </div>
    <div class="row"><span class="label">Regler</span><span class="value" id="ctrl-name">–</span></div>
    <div class="row"><span class="label">Setpoint Batterie</span><span class="value" id="ctrl-sp">–</span></div>
    <div class="row"><span class="label">Ziel (roh)</span><span class="value" id="ctrl-raw">–</span></div>
    <div class="row"><span class="label">FF gesamt</span><span class="value" id="ctrl-ff">–</span></div>
    <div class="row"><span class="label">Feedback</span><span class="value" id="ctrl-fb">–</span></div>
    <div class="row">
      <span class="label">Watchdog Resets</span>
      <span class="value" id="ctrl-wdog" style="color:var(--muted)">0</span>
    </div>
    <div class="row" id="zfi-status-row" style="display:none">
      <span class="label">ZFI Regelung</span>
      <span id="auto-zfi-state" class="badge badge-gray">–</span>
    </div>
    <div class="row">
      <span class="label">Abweichung (Netz − Ziel)</span>
      <span class="value" id="ctrl-delta">–</span>
    </div>
    <div class="chart-wrap"><canvas id="chart-global"></canvas></div>
  </div></div>

<div class="layout-settings">
  <!-- ── Regler-Einstellungen (allgemein) ───────────────────────────────────── -->
  <div class="card" id="card-general">
    <h2>Regler-Einstellungen</h2>
    <select id="reg-select" onchange="onRegSelectChange()"></select>
    <div id="reg-desc" style="color:var(--muted);font-size:11px;margin-top:5px;min-height:20px"></div>
    <div id="general-settings" style="margin-top:8px"></div>
    <button class="apply-btn" style="margin-top:8px"
            onclick="applyGroupSettingsAndActivate('General')">Anwenden &amp; aktivieren</button>
  </div>

  <!-- ── Phase A Einstellungen ─────────────────────────────────────────────── -->
  <div class="card" id="card-phase-A" style="display:none">
    <h2 id="ph-head-A">Phase A</h2>
    <div id="phase-settings-A"></div>
    <button class="apply-btn" style="margin-top:8px"
            onclick="applyGroupSettings('Phase A')">Anwenden</button>
  </div>

  <!-- ── Phase B Einstellungen ─────────────────────────────────────────────── -->
  <div class="card" id="card-phase-B" style="display:none">
    <h2 id="ph-head-B">Phase B</h2>
    <div id="phase-settings-B"></div>
    <button class="apply-btn" style="margin-top:8px"
            onclick="applyGroupSettings('Phase B')">Anwenden</button>
  </div>

  <!-- ── Phase C Einstellungen ─────────────────────────────────────────────── -->
  <div class="card" id="card-phase-C" style="display:none">
    <h2 id="ph-head-C">Phase C</h2>
    <div id="phase-settings-C"></div>
    <button class="apply-btn" style="margin-top:8px"
            onclick="applyGroupSettings('Phase C')">Anwenden</button>
  </div>

  <!-- ── GridPythia Auto Plan ───────────────────────────────────────────────── -->
  <div class="card" id="card-auto" style="display:none">
    <h2>GridPythia Plan</h2>
    <div class="row">
      <span class="label">Verbindung</span>
      <span id="auto-conn-badge" class="badge badge-gray">–</span>
    </div>
    <div class="row">
      <span class="label">Plan erstellt</span>
      <span id="auto-plan-ts" style="color:var(--muted);font-size:11px">–</span>
    </div>
    <div class="row">
      <span class="label">Aktuell effektiv</span>
      <span id="auto-effective" class="badge badge-gray">–</span>
    </div>
    <div id="plan-list" style="margin-top:8px"></div>
  </div>

</div>
<div id="toast">✓ Gespeichert</div>

<script>
// ── State & history ────────────────────────────────────────────────────────
let ws, state = null, regulators = [];
const HISTORY = 60;
// Per-phase histories: {grid: [], sp: [], cons: [] (for batt phase)}
const phHist = { A:{grid:[],sp:[],cons:[],err:[]}, B:{grid:[],sp:[],cons:[],err:[]}, C:{grid:[],sp:[],cons:[],err:[]} };
// Global history: tracking error (total_grid - target_w)
const globalHist = { err:[] };
// Last known target_w from regulator settings (updated on each render)
let lastTargetW = 0;

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => { dot('live'); fetchRegulators(); };
  ws.onclose = () => { dot('error'); setTimeout(connect, 3000); };
  ws.onerror = () => { dot('error'); };
  ws.onmessage = (ev) => { state = JSON.parse(ev.data); render(state); };
}
function dot(cls) { document.getElementById('ws-dot').className = 'dot ' + cls; }

// ── Helpers ────────────────────────────────────────────────────────────────
function w(v, dec=0, unit='W') {
  if (v == null) return '–';
  const n = Number(v);
  const s = n.toFixed(dec) + ' ' + unit;
  if (n > 0) return `<span style="color:var(--red)">${s}</span>`;
  if (n < 0) return `<span style="color:var(--green)">${s}</span>`;
  return s;
}
function push(arr, val) { arr.push(val ?? 0); if (arr.length > HISTORY) arr.shift(); }

// ── Chart drawing ──────────────────────────────────────────────────────────
function drawChartWithZero(id, data, color) {
  const canvas = document.getElementById(id);
  if (!canvas) return;
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const W = rect.width * dpr, H = rect.height * dpr;
  if (canvas.width !== W || canvas.height !== H) { canvas.width = W; canvas.height = H; }
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, W, H);
  if (!data.length) return;
  const mn = Math.min(...data, -5), mx = Math.max(...data, 5);
  const range = mx - mn || 1;
  const zeroY = H - ((0 - mn) / range) * H;
  // Zero-line
  ctx.strokeStyle = '#3d4460'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(0, zeroY); ctx.lineTo(W, zeroY); ctx.stroke();
  // Fill area: above zero = red (feed-in, bad), below zero = green (draw, ok)
  ctx.beginPath();
  data.forEach((v, i) => {
    const x = (i / (HISTORY - 1)) * W;
    const y = H - ((v - mn) / range) * H;
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  // close to zero line
  ctx.lineTo(((data.length-1)/(HISTORY-1))*W, zeroY);
  ctx.lineTo(0, zeroY);
  ctx.closePath();
  // Split fill: positive = red-tinted, negative = green-tinted
  const grad = ctx.createLinearGradient(0, 0, 0, H);
  grad.addColorStop(0, 'rgba(239,68,68,0.25)');
  grad.addColorStop((zeroY/H), 'rgba(239,68,68,0.05)');
  grad.addColorStop((zeroY/H), 'rgba(34,197,94,0.05)');
  grad.addColorStop(1, 'rgba(34,197,94,0.25)');
  ctx.fillStyle = grad; ctx.fill();
  // Line
  ctx.strokeStyle = color; ctx.lineWidth = 1.5 * dpr;
  ctx.setLineDash([]);
  ctx.beginPath();
  data.forEach((v, i) => {
    const x = (i / (HISTORY - 1)) * W;
    const y = H - ((v - mn) / range) * H;
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function drawChart(id, series) {
  const canvas = document.getElementById(id);
  if (!canvas) return;
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const W = rect.width * dpr, H = rect.height * dpr;
  if (canvas.width !== W || canvas.height !== H) { canvas.width = W; canvas.height = H; }
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, W, H);
  // Zero line
  ctx.strokeStyle = '#2a2d3e'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(0, H/2); ctx.lineTo(W, H/2); ctx.stroke();
  const allVals = series.flatMap(s => s.data);
  if (!allVals.length) return;
  const mn = Math.min(...allVals, -1), mx = Math.max(...allVals, 1);
  const range = mx - mn || 1;
  for (const {data, color, dash} of series) {
    if (!data.length) continue;
    ctx.strokeStyle = color; ctx.lineWidth = 1.5 * dpr;
    ctx.setLineDash(dash || []);
    ctx.beginPath();
    data.forEach((v, i) => {
      const x = (i / (HISTORY - 1)) * W;
      const y = H - ((v - mn) / range) * H;
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.stroke();
  }
  ctx.setLineDash([]);
}

// ── Main render ────────────────────────────────────────────────────────────
function render(s) {
  document.getElementById('ts').textContent = new Date(s.timestamp*1000).toLocaleTimeString();

  // ── Mode badge & buttons
  const modeMap = { idle:['IDLE','gray'], ac_charge:['AC Laden','yellow'],
                    discharge_zero_feed:['Zero-Feed','green'], auto:['Auto','blue'] };
  const [ml, mc] = modeMap[s.mode] || [s.mode, 'gray'];
  const mb = document.getElementById('mode-badge');
  mb.textContent = ml; mb.className = `badge badge-${mc}`;
  ['btn-idle','btn-zf','btn-charge','btn-auto'].forEach(id =>
    document.getElementById(id).classList.remove('active'));
  if (s.mode==='idle')                   document.getElementById('btn-idle').classList.add('active');
  else if (s.mode==='discharge_zero_feed') document.getElementById('btn-zf').classList.add('active');
  else if (s.mode==='ac_charge')          document.getElementById('btn-charge').classList.add('active');
  else if (s.mode==='auto')               document.getElementById('btn-auto').classList.add('active');

  // ── Pause badge
  const paused = s.zfi_paused_low_soc || s.zfi_paused_full_battery;
  const pauseBadge = document.getElementById('zfi-pause-badge');
  pauseBadge.style.display = paused ? '' : 'none';
  if (s.zfi_paused_low_soc)      pauseBadge.textContent = '⏸ SoC-Min';
  else if (s.zfi_paused_full_battery) pauseBadge.textContent = '⏸ Batt. voll';

  // ── Auto plan card
  const cardAuto = document.getElementById('card-auto');
  cardAuto.style.display = s.mode === 'auto' ? '' : 'none';
  if (s.auto_status) renderAutoStatus(s.auto_status, s);

  // ── Sample data
  const sm = s.sample;
  const total = sm ? (sm.phase_a_w||0)+(sm.phase_b_w||0)+(sm.phase_c_w||0) : null;
  const cons  = sm ? (total + (sm.battery_output_w||0)) : null;
  const battOut = sm?.battery_output_w;
  const battIn  = sm?.charge_input_w;

  // Header metrics
  document.getElementById('hm-total').innerHTML = w(total);
  document.getElementById('hm-cons').innerHTML  = w(cons);
  // Battery: show output or charge input
  if (battOut > 0)      document.getElementById('hm-batt').innerHTML = `<span style="color:var(--green)">↓${battOut.toFixed(0)} W</span>`;
  else if (battIn > 0)  document.getElementById('hm-batt').innerHTML = `<span style="color:var(--yellow)">↑${battIn.toFixed(0)} W</span>`;
  else                  document.getElementById('hm-batt').textContent = '—';
  document.getElementById('hm-soc').textContent = sm?.soc_percent != null ? sm.soc_percent + ' %' : '–';

  // ── Per-phase live cards
  const c = s.control;
  const ffp = c?.ff_per_phase || {};
  const configTarget = c?.target_power_w ?? null;
  (['A','B','C']).forEach(ph => {
    const isFF = (ffp[ph] !== undefined);
    // Role badge
    const roleBadge = document.getElementById('ph-role-' + ph);
    if (roleBadge) {
      roleBadge.textContent = isFF ? 'FF' : 'Bat';
      roleBadge.className = 'badge ' + (isFF ? 'badge-blue' : 'badge-green');
    }
    // Grid value (raw from Shelly)
    const gridVal = sm ? sm['phase_'+ph.toLowerCase()+'_w'] : null;
    document.getElementById('ph-grid-' + ph).innerHTML = w(gridVal);
    // For battery phase: show estimated real consumption = grid + estimated_battery
    const consEl = document.getElementById('ph-cons-' + ph);
    let consVal = null;
    if (consEl) {
      consVal = (gridVal != null && sm?.battery_output_w != null)
        ? gridVal + sm.battery_output_w : null;
      consEl.innerHTML = consVal != null ? w(consVal) : '–';
      push(phHist[ph].cons, consVal);
    }
    // Setpoint demand for this phase:
    //   FF phase  → individual FF demand (target = 0 W)
    //   Bat phase → target for phase B on grid = target_power_w − ff_sum
    //              (= the portion of the total target that falls on the battery phase)
    let spVal = null;
    if (c) spVal = isFF ? (ffp[ph] ?? null)
                       : ((c.target_power_w ?? 0) - (c.ff_output_w ?? 0));
    document.getElementById('ph-sp-' + ph).innerHTML = spVal != null ? w(spVal) : '–';
    // Oscillation
    const oscData = c ? c['osc_'+ph.toLowerCase()] : null;
    renderOscInline('osc-badge-'+ph, 'osc-det-'+ph, 'osc-lim-'+ph, oscData);
    // Push to history
    push(phHist[ph].grid, gridVal);
    push(phHist[ph].sp,   spVal);
    // Phasen-Chart: Delta = Netz − Anforderung (Nulllinie = perfekte Regelung)
    const errVal = (gridVal != null && spVal != null) ? gridVal - spVal : null;
    push(phHist[ph].err, errVal);
    const phColor = ph==='A' ? '#4f8ef7' : ph==='B' ? '#fb923c' : '#eab308';
    drawChartWithZero('chart-'+ph, phHist[ph].err, phColor);
  });

  // ── Global stats card
  const sp = c?.setpoint_w ?? null;
  document.getElementById('ctrl-name').textContent = c?.regulator_name ?? '–';
  document.getElementById('ctrl-sp').textContent   = sp != null ? sp + ' W' : '–';
  document.getElementById('ctrl-raw').innerHTML    = c?.raw_target_w != null ? Number(c.raw_target_w).toFixed(0)+' W' : '–';
  document.getElementById('ctrl-ff').innerHTML     = c?.ff_output_w  != null ? Number(c.ff_output_w).toFixed(0)+' W' : '–';
  document.getElementById('ctrl-fb').innerHTML     = c?.feedback_output_w != null ? Number(c.feedback_output_w).toFixed(0)+' W' : '–';
  const wd = c?.watchdog_resets ?? 0;
  const wdEl = document.getElementById('ctrl-wdog');
  wdEl.textContent = wd; wdEl.style.color = wd > 0 ? 'var(--red)' : 'var(--muted)';
  // Tracking error = measured total grid draw minus the configured target (e.g. 3 W)
  const trackErr = (total != null && configTarget != null) ? total - configTarget : null;
  const deltaEl = document.getElementById('ctrl-delta');
  deltaEl.innerHTML = trackErr != null ? w(trackErr, 1) : '–';
  push(globalHist.err, trackErr);
  // Draw tracking-error chart with zero-line emphasis
  drawChartWithZero('chart-global', globalHist.err, '#4f8ef7');

  // ZFI state in global card (always visible)
  const zfiRow = document.getElementById('zfi-status-row');
  zfiRow.style.display = '';
  const zfiEl = document.getElementById('auto-zfi-state');
  const statusBadge = document.getElementById('ctrl-status-badge');
  if (s.zfi_paused_low_soc) {
    zfiEl.textContent = 'Pausiert (SoC-Min)'; zfiEl.className = 'badge badge-yellow';
    statusBadge.textContent = 'Pausiert'; statusBadge.className = 'badge badge-yellow';
  } else if (s.zfi_paused_full_battery) {
    zfiEl.textContent = 'Pausiert (Batt. voll)'; zfiEl.className = 'badge badge-yellow';
    statusBadge.textContent = 'Pausiert'; statusBadge.className = 'badge badge-yellow';
  } else if (s.mode === 'discharge_zero_feed' || (s.mode==='auto' && c)) {
    zfiEl.textContent = 'Aktiv'; zfiEl.className = 'badge badge-green';
    statusBadge.textContent = 'Aktiv'; statusBadge.className = 'badge badge-green';
  } else {
    zfiEl.textContent = 'Inaktiv'; zfiEl.className = 'badge badge-gray';
    statusBadge.textContent = s.mode ?? '–'; statusBadge.className = 'badge badge-gray';
  }
}

function renderOscInline(badgeId, detId, limId, osc) {
  const b = document.getElementById(badgeId);
  const d = document.getElementById(detId);
  const l = document.getElementById(limId);
  const limRow = document.getElementById(limId.replace('osc-lim-', 'osc-lim-row-'));
  if (!b) return;
  if (!osc) {
    b.textContent='–'; b.className='badge badge-gray';
    if(d) d.textContent='';
    if(l) l.textContent='';
    if(limRow) limRow.style.display='none';
    return;
  }
  b.textContent = osc.oscillating ? 'OSZ!' : 'OK';
  b.className = 'badge ' + (osc.oscillating ? 'badge-red' : 'badge-green');
  if (l) {
    if (osc.limit_w != null) {
      l.textContent = Number(osc.limit_w).toFixed(0)+' W';
      if (limRow) limRow.style.display = '';
    } else {
      l.textContent = '';
      if (limRow) limRow.style.display = 'none';
    }
  }
  if (d) {
    const parts = [];
    if (osc.holder_active)    parts.push(osc.holder_oscillating    ? '<b style="color:var(--red)">H</b>' : '<span style="color:var(--muted)">H</span>');
    if (osc.predictor_active) parts.push(osc.predictor_oscillating ? '<b style="color:var(--red)">P</b>' : '<span style="color:var(--muted)">P</span>');
    d.innerHTML = parts.length ? '['+parts.join('·')+']' : '';
  }
}

// ── Regulator settings panel ───────────────────────────────────────────────
async function fetchRegulators() {
  const res = await fetch('/api/regulators');
  regulators = await res.json();
  const sel = document.getElementById('reg-select');
  sel.innerHTML = regulators.map(r =>
    `<option value="${r.name}" ${r.is_active?'selected':''}>${r.name}${r.is_active?' ✓':''}</option>`
  ).join('');
  renderRegSettings();
}

function onRegSelectChange() { renderRegSettings(); }

function renderRegSettings() {
  const name = document.getElementById('reg-select').value;
  const reg = regulators.find(r => r.name === name);
  if (!reg) return;
  document.getElementById('reg-desc').textContent = reg.description || '';

  // Group fields
  const groups = {};
  for (const [key, def] of Object.entries(reg.settings_schema)) {
    const g = def.group || 'General';
    if (!groups[g]) groups[g] = [];
    groups[g].push([key, def]);
  }

  fillSettingsContainer('general-settings', groups['General'] || [], reg.current_settings);

  ['A','B','C'].forEach(ph => {
    const gKey = Object.keys(groups).find(g => g.startsWith('Phase '+ph));
    const card = document.getElementById('card-phase-'+ph);
    if (!card) return;
    if (!gKey) { card.style.display = 'none'; return; }
    const hd = document.getElementById('ph-head-'+ph);
    if (hd) hd.textContent = gKey;
    fillSettingsContainer('phase-settings-'+ph, groups[gKey], reg.current_settings);
    card.style.display = '';
  });
}

function fillSettingsContainer(containerId, fields, cur) {
  const el = document.getElementById(containerId);
  if (!el) return;
  if (!fields || !fields.length) { el.innerHTML = ''; return; }
  el.innerHTML = fields.map(([key, def]) => {
    const val = cur[key] ?? def.default ?? '';
    if (def.type === 'boolean') {
      return `<label style="margin-top:5px;display:flex;align-items:center;gap:5px">
        <input type="checkbox" id="s_${key}" ${val?'checked':''} style="width:auto"> ${def.title}</label>`;
    }
    if (def.type === 'string' && def.enum) {
      const opts = def.enum.map(v => `<option value="${v}" ${v===String(val)?'selected':''}>${v}</option>`).join('');
      return `<label>${def.title}</label><select id="s_${key}">${opts}</select>`;
    }
    return `<label>${def.title}</label>
      <input type="number" id="s_${key}" value="${val}"
        min="${def.minimum??''}" max="${def.maximum??''}" step="${def.step??'any'}"/>`;
  }).join('');
}

function collectGroupSettings(groupPrefix) {
  const name = document.getElementById('reg-select').value;
  const reg = regulators.find(r => r.name === name);
  if (!reg) return null;
  const settings = {};
  for (const [key, def] of Object.entries(reg.settings_schema)) {
    const g = def.group || 'General';
    if (!g.startsWith(groupPrefix)) continue;
    const el = document.getElementById('s_'+key);
    if (!el) continue;
    if (def.type==='boolean') settings[key] = el.checked;
    else if (def.type==='string') settings[key] = el.value;
    else settings[key] = Number(el.value);
  }
  return {name, settings};
}

async function applyGroupSettings(groupPrefix) {
  const r = collectGroupSettings(groupPrefix);
  if (!r) return;
  await fetch(`/api/regulators/${r.name}/settings`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(r.settings)
  });
  fetchRegulators(); showToast('Gespeichert');
}

async function applyGroupSettingsAndActivate(groupPrefix) {
  const r = collectGroupSettings(groupPrefix);
  if (!r) return;
  // First activate (select) the regulator
  await fetch('/api/regulators/select', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({name: r.name})
  });
  // Then apply settings
  await fetch(`/api/regulators/${r.name}/settings`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(r.settings)
  });
  fetchRegulators(); showToast('Regler aktiviert & Einstellungen gespeichert');
}

// ── Mode control ───────────────────────────────────────────────────────────
function openChargeDialog() {
  const p = document.getElementById('charge-panel');
  const wasOpen = p.style.display !== 'none';
  document.getElementById('auto-panel').style.display = 'none';
  p.style.display = wasOpen ? 'none' : 'block';
}
function openAutoDialog() {
  const p = document.getElementById('auto-panel');
  const wasOpen = p.style.display !== 'none';
  document.getElementById('charge-panel').style.display = 'none';
  p.style.display = wasOpen ? 'none' : 'block';
}
function setMode(mode) {
  const body = {mode};
  if (mode === 'ac_charge') {
    body.charge_power_w = parseInt(document.getElementById('charge-w').value);
    document.getElementById('charge-panel').style.display = 'none';
  }
  fetch('/api/mode', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body)});
}

async function activateAuto() {
  const broker = document.getElementById('auto-broker').value.trim();
  const device_id = document.getElementById('auto-device-id').value.trim();
  if (!broker || !device_id) { showToast('Broker und Device ID erforderlich'); return; }
  const res = await fetch('/api/auto/connect', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({mqtt_broker:broker, device_id, topic_prefix:'gridpythia', status_interval_s:60})
  });
  if (res.ok) { document.getElementById('auto-panel').style.display='none'; showToast('Auto-Modus aktiviert'); }
  else { const err = await res.json().catch(()=>({})); showToast('Fehler: '+(err.detail||res.status)); }
}
async function deactivateAuto() {
  await fetch('/api/auto/disconnect', {method:'POST'});
  document.getElementById('auto-panel').style.display = 'none';
  showToast('Auto-Modus deaktiviert');
}

// ── Auto plan rendering ────────────────────────────────────────────────────
function renderAutoStatus(as, fullState) {
  const connEl = document.getElementById('auto-conn-badge');
  connEl.textContent = as.connected ? (as.has_plan ? 'Verbunden ✓ Plan' : 'Verbunden (kein Plan)') : 'Getrennt';
  connEl.className = 'badge ' + (as.connected ? (as.has_plan ? 'badge-green' : 'badge-yellow') : 'badge-red');
  document.getElementById('auto-plan-ts').textContent = as.plan_published_at || '–';
  const effEl = document.getElementById('auto-effective');
  effEl.textContent = as.effective_mode || '–';
  effEl.className = 'badge ' + effectiveBadgeClass(as.effective_mode);
  const list = document.getElementById('plan-list');
  if (!as.plan_summary?.length) {
    list.innerHTML = '<div style="color:var(--muted);font-size:11px;padding:4px 0">Kein Plan</div>'; return;
  }
  list.innerHTML = as.plan_summary.map(e => {
    const dateStr = e.date ? `<span style="color:var(--muted);font-size:10px">${e.date} </span>` : '';
    const pwrStr = e.power_w != null ? e.power_w+' W' : (e.mode_label.includes('Zero-Feed')||e.mode_label.includes('Entladen') ? 'HW' : '');
    return `<div class="plan-entry">
      <span class="plan-time">${dateStr}${e.from_time}–${e.to_time}</span>
      <span class="plan-mode">${planModeIcon(e.mode_label)} ${e.mode_label}</span>
      <span class="plan-pwr">${pwrStr}</span></div>`;
  }).join('');
}
function effectiveBadgeClass(l) {
  if (!l||l==='–') return 'badge-gray';
  if (l.includes('Lade')) return 'badge-yellow';
  if (l.includes('Fallback')||l==='Idle') return 'badge-gray';
  return 'badge-green';
}
function planModeIcon(l) {
  if (!l) return '';
  if (l.includes('Laden')) return '⬆';
  if (l.includes('Zero-Feed')||l.includes('Entladen')) return '⬇';
  return '⏸';
}

function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = '✓ ' + msg; t.style.display = 'block';
  setTimeout(() => t.style.display = 'none', 2500);
}

connect();
</script>
</body>
</html>"""


# ── FastAPI app factory ───────────────────────────────────────────────────────


def create_app(runtime: ControlRuntime) -> FastAPI:
    """Create and return the FastAPI application bound to a ControlRuntime."""
    app = FastAPI(title="Zendure Dashboard", docs_url=None, redoc_url=None)

    # ── HTML ──────────────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def root() -> HTMLResponse:
        return HTMLResponse(_HTML)

    # ── REST API ──────────────────────────────────────────────────────────────

    @app.get("/api/state")
    async def get_state() -> DashboardState:
        return runtime.get_state()

    @app.get("/api/regulators")
    async def list_regulators():
        return runtime.list_regulators()

    @app.post("/api/mode")
    async def set_mode(cmd: SetModeCommand) -> dict[str, str]:
        await runtime.set_mode(
            cmd.mode,
            charge_power_w=cmd.charge_power_w,
            max_discharge_w=cmd.max_discharge_w,
        )
        return {"status": "ok"}

    @app.post("/api/regulators/select")
    async def select_regulator(cmd: SelectRegulatorCommand) -> dict[str, str]:
        try:
            await runtime.set_active_regulator(cmd.name)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        return {"status": "ok", "active": cmd.name}

    @app.post("/api/regulators/{name}/settings")
    async def update_settings(name: str, settings: dict[str, Any]) -> dict[str, str]:
        try:
            await runtime.update_regulator_settings(name, settings)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        return {"status": "ok"}

    @app.post("/api/auto/connect")
    async def auto_connect(cmd: AutoConnectCommand) -> dict[str, str]:
        try:
            await runtime.enable_auto_mode(
                mqtt_broker=cmd.mqtt_broker,
                device_id=cmd.device_id,
                topic_prefix=cmd.topic_prefix,
                status_interval_s=cmd.status_interval_s,
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
        return {"status": "ok", "device_id": cmd.device_id}

    @app.post("/api/auto/disconnect")
    async def auto_disconnect() -> dict[str, str]:
        await runtime.disable_auto_mode()
        return {"status": "ok"}

    # ── WebSocket ─────────────────────────────────────────────────────────────

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        queue: asyncio.Queue[DashboardState] = asyncio.Queue(maxsize=5)

        async def push(state: DashboardState) -> None:
            try:
                queue.put_nowait(state)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(state)
                except asyncio.QueueEmpty:
                    pass

        runtime.add_state_callback(push)
        try:
            # Send current state immediately on connect
            await ws.send_text(runtime.get_state().model_dump_json())

            while True:
                state = await asyncio.wait_for(queue.get(), timeout=5.0)
                await ws.send_text(state.model_dump_json())
        except (WebSocketDisconnect, asyncio.TimeoutError):
            pass
        except Exception:
            logger.debug("WebSocket error", exc_info=True)
        finally:
            runtime.remove_state_callback(push)

    return app
