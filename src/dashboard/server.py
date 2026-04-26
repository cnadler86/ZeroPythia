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
  h1 { font-size: 1.2rem; font-weight: 600; }
  h2 { font-size: 0.9rem; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; margin-bottom: .6rem; }
  .layout { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; padding: 16px; max-width: 1400px; margin: 0 auto; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
  .header { display: flex; align-items: center; gap: 12px; padding: 14px 16px; border-bottom: 1px solid var(--border); }
  .dot { width:10px; height:10px; border-radius:50%; background:var(--muted); flex-shrink:0; }
  .dot.live { background:var(--green); box-shadow: 0 0 6px var(--green); }
  .dot.error { background:var(--red); }
  .row { display: flex; justify-content: space-between; align-items: center; padding: 4px 0; border-bottom: 1px solid var(--border); }
  .row:last-child { border-bottom: none; }
  .label { color: var(--muted); }
  .value { font-weight: 600; font-variant-numeric: tabular-nums; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 700; }
  .badge-green { background: #14532d; color: var(--green); }
  .badge-red { background: #450a0a; color: var(--red); }
  .badge-yellow { background: #422006; color: var(--yellow); }
  .badge-blue { background: #1e3a5f; color: var(--accent); }
  .badge-gray { background: #1e293b; color: var(--muted); }
  .mode-btn { padding: 8px 14px; border: 1px solid var(--border); border-radius: 6px; background: var(--card); color: var(--text); cursor: pointer; font-size: 13px; transition: all .15s; }
  .mode-btn:hover { border-color: var(--accent); color: var(--accent); }
  .mode-btn.active { background: var(--accent); border-color: var(--accent); color: #fff; }
  .btn-row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
  input[type=number], input[type=text], select {
    background: var(--bg); border: 1px solid var(--border); color: var(--text);
    border-radius: 6px; padding: 6px 10px; width: 100%; font-size: 13px;
  }
  input:focus, select:focus { outline: none; border-color: var(--accent); }
  label { display: block; color: var(--muted); font-size: 12px; margin: 8px 0 3px; }
  .apply-btn { margin-top: 12px; padding: 8px 16px; background: var(--accent); border: none; border-radius: 6px; color: #fff; cursor: pointer; font-size: 13px; width: 100%; }
  .apply-btn:hover { opacity: .85; }
  .apply-btn.secondary { background: #374151; }
  .apply-btn.secondary:hover { background: #4b5563; }
  .osc-row { display: flex; align-items: center; gap: 8px; padding: 3px 0; }
  .phase-label { width: 18px; color: var(--muted); font-size: 11px; }
  .chart-wrap { width: 100%; height: 90px; overflow: hidden; }
  canvas { width: 100%; height: 100%; display: block; }
  .plan-entry { display: flex; align-items: center; gap: 8px; padding: 5px 0; border-bottom: 1px solid var(--border); }
  .plan-entry:last-child { border-bottom: none; }
  .plan-time { font-variant-numeric: tabular-nums; font-size: 12px; color: var(--muted); min-width: 100px; }
  .plan-mode { font-weight: 600; flex: 1; }
  .plan-pwr { font-size: 12px; color: var(--muted); }
  .plan-active { background: #1e3a5f22; border-radius: 4px; padding: 2px 4px; }
  #toast { position: fixed; bottom: 20px; right: 20px; background: #22c55e; color: #fff;
           padding: 10px 18px; border-radius: 8px; font-size: 13px; display: none; z-index: 99; }
</style>
</head>
<body>
<div class="header">
  <div class="dot" id="ws-dot"></div>
  <h1>Zendure Dashboard</h1>
  <span id="ts" style="margin-left:auto;color:var(--muted);font-size:12px;"></span>
</div>

<div class="layout">

  <!-- Mode Control -->
  <div class="card" id="card-mode">
    <h2>Betriebsmodus</h2>
    <div class="row">
      <span class="label">Aktuell</span>
      <span id="mode-badge" class="badge badge-gray">–</span>
    </div>
    <div class="btn-row">
      <button class="mode-btn" id="btn-idle" onclick="setMode('idle')">Idle</button>
      <button class="mode-btn" id="btn-zf" onclick="setMode('discharge_zero_feed')">Zero-Feed</button>
      <button class="mode-btn" id="btn-charge" onclick="openChargeDialog()">AC Laden ▸</button>
      <button class="mode-btn" id="btn-auto" onclick="openAutoDialog()">Auto ▸</button>
    </div>
    <div id="charge-panel" style="display:none;margin-top:12px">
      <label>Ladeleistung [W]</label>
      <input type="number" id="charge-w" value="400" min="1" max="3000"/>
      <button class="apply-btn" onclick="setMode('ac_charge')">Laden starten</button>
    </div>
    <div id="auto-panel" style="display:none;margin-top:12px">
      <label>MQTT Broker URL</label>
      <input type="text" id="auto-broker" value="mqtt://localhost:1883" placeholder="mqtt://host:1883"/>
      <label>Device ID</label>
      <input type="text" id="auto-device-id" value="SF800Pro" placeholder="SF800Pro"/>
      <label>Topic Prefix</label>
      <input type="text" id="auto-topic" value="gridpythia"/>
      <label>Status-Intervall [s]</label>
      <input type="number" id="auto-interval" value="60" min="10" max="600"/>
      <button class="apply-btn" onclick="activateAuto()">Auto aktivieren</button>
      <button class="apply-btn secondary" style="margin-top:4px" onclick="deactivateAuto()">Deaktivieren</button>
    </div>
    <div style="margin-top:12px">
      <label>Max. Entlade-Limit [W]</label>
      <input type="number" id="max-dis-w" min="1" max="3000" value="800"/>
      <button class="apply-btn" style="margin-top:6px" onclick="applyMaxDis()">Limit setzen</button>
    </div>
  </div>

  <!-- Grid Live -->
  <div class="card">
    <h2>Netz (Shelly 3EM)</h2>
    <div class="row"><span class="label">Phase A</span><span class="value" id="p-a">–</span></div>
    <div class="row"><span class="label">Phase B</span><span class="value" id="p-b">–</span></div>
    <div class="row"><span class="label">Phase C</span><span class="value" id="p-c">–</span></div>
    <div class="row"><span class="label">Total Grid</span><span class="value" id="p-total">–</span></div>
    <div class="row"><span class="label">Verbrauch</span><span class="value" id="p-cons">–</span></div>
    <div class="chart-wrap" style="margin-top:10px">
      <canvas id="grid-chart"></canvas>
    </div>
  </div>

  <!-- Battery -->
  <div class="card">
    <h2>Batterie (Zendure)</h2>
    <div class="row"><span class="label">Output</span><span class="value" id="b-out">–</span></div>
    <div class="row"><span class="label">Charge Input</span><span class="value" id="b-in">–</span></div>
    <div class="row"><span class="label">SoC</span><span class="value" id="b-soc">–</span></div>
    <div class="row"><span class="label">Max Discharge</span><span class="value" id="b-maxdis">–</span></div>
    <div class="chart-wrap" style="margin-top:10px">
      <canvas id="batt-chart"></canvas>
    </div>
  </div>

  <!-- Controller Status -->
  <div class="card">
    <h2>Regler-Status</h2>
    <div class="row"><span class="label">Regler</span><span class="value" id="ctrl-name">–</span></div>
    <div class="row"><span class="label">Setpoint</span><span class="value" id="ctrl-sp">–</span></div>
    <div class="row"><span class="label">Ziel (roh)</span><span class="value" id="ctrl-raw">–</span></div>
    <div class="row"><span class="label">FF</span><span class="value" id="ctrl-ff">–</span></div>
    <div class="row"><span class="label">Feedback</span><span class="value" id="ctrl-fb">–</span></div>
    <div class="row"><span class="label">Osc-Limit</span><span class="value" id="ctrl-osc">–</span></div>
    <div style="margin-top:10px">
      <h2>Oszillation</h2>
      <div class="osc-row"><span class="phase-label">A</span><span id="osc-a" class="badge badge-gray">–</span><span id="osc-a-lim" style="color:var(--muted);font-size:12px"></span></div>
      <div class="osc-row"><span class="phase-label">B</span><span id="osc-b" class="badge badge-gray">–</span><span id="osc-b-lim" style="color:var(--muted);font-size:12px"></span></div>
      <div class="osc-row"><span class="phase-label">C</span><span id="osc-c" class="badge badge-gray">–</span><span id="osc-c-lim" style="color:var(--muted);font-size:12px"></span></div>
      <div class="osc-row"><span class="phase-label">Σ</span><span id="osc-tot" class="badge badge-gray">–</span><span id="osc-tot-lim" style="color:var(--muted);font-size:12px"></span></div>
    </div>
  </div>

  <!-- Regulator Selection -->
  <div class="card">
    <h2>Regler wählen</h2>
    <select id="reg-select" onchange="selectRegulator()"></select>
    <div id="reg-desc" style="color:var(--muted);font-size:12px;margin-top:6px;min-height:30px"></div>
    <div id="reg-settings" style="margin-top:8px"></div>
    <button class="apply-btn" id="reg-apply" onclick="applySettings()">Einstellungen anwenden</button>
  </div>

  <!-- Holder settings -->
  <div class="card" id="card-holder" style="display:none">
    <h2>Holder <span style="color:var(--muted);font-weight:400;font-size:11px">(kurze Schwingungen)</span></h2>
    <div id="holder-settings"></div>
  </div>

  <!-- Predictor settings -->
  <div class="card" id="card-predictor" style="display:none">
    <h2>Predictor <span style="color:var(--muted);font-weight:400;font-size:11px">(periodische Lasten)</span></h2>
    <div id="predictor-settings"></div>
  </div>

  <!-- GridPythia Auto Plan -->
  <div class="card" id="card-auto" style="display:none">
    <h2>GridPythia Plan</h2>
    <div class="row">
      <span class="label">Verbindung</span>
      <span id="auto-conn-badge" class="badge badge-gray">–</span>
    </div>
    <div class="row">
      <span class="label">Plan erstellt</span>
      <span id="auto-plan-ts" style="color:var(--muted);font-size:12px">–</span>
    </div>
    <div class="row">
      <span class="label">Aktuell effektiv</span>
      <span id="auto-effective" class="badge badge-gray">–</span>
    </div>
    <div class="row">
      <span class="label">ZFI Regelung</span>
      <span id="auto-zfi-state" class="badge badge-gray">–</span>
    </div>
    <div id="plan-list" style="margin-top:10px"></div>
  </div>

</div>

<div id="toast">✓ Gespeichert</div>

<script>
// ── WebSocket connection ───────────────────────────────────────────────────
let ws, state = null, regulators = [];
const HISTORY = 60;
const gridHist = { a:[], b:[], c:[], total:[] };
const battHist = { out:[], soc:[] };

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => { dot('live'); fetchRegulators(); };
  ws.onclose = () => { dot('error'); setTimeout(connect, 3000); };
  ws.onerror = () => { dot('error'); };
  ws.onmessage = (ev) => { state = JSON.parse(ev.data); render(state); };
}

function dot(cls) {
  const el = document.getElementById('ws-dot');
  el.className = 'dot ' + cls;
}

// ── Render ─────────────────────────────────────────────────────────────────
function w(v, dec=0) {
  if (v == null) return '–';
  const n = Number(v);
  const s = n.toFixed(dec);
  if (n > 0) return `<span style="color:#ef4444">${s} W</span>`;
  if (n < 0) return `<span style="color:#22c55e">${s} W</span>`;
  return `${s} W`;
}

function render(s) {
  document.getElementById('ts').textContent = new Date(s.timestamp * 1000).toLocaleTimeString();

  // Mode badge
  const modeMap = {
    idle: ['IDLE','gray'], ac_charge:['AC Laden','yellow'],
    discharge_zero_feed:['Zero-Feed','green'],
    auto: ['Auto','blue'],
  };
  const [modeLabel, modeColor] = modeMap[s.mode] || [s.mode, 'gray'];
  const mb = document.getElementById('mode-badge');
  mb.textContent = modeLabel;
  mb.className = `badge badge-${modeColor}`;

  // Highlight active mode button
  ['btn-idle','btn-zf','btn-charge','btn-auto'].forEach(id => {
    document.getElementById(id).classList.remove('active');
  });
  if (s.mode === 'idle') document.getElementById('btn-idle').classList.add('active');
  else if (s.mode === 'discharge_zero_feed') document.getElementById('btn-zf').classList.add('active');
  else if (s.mode === 'ac_charge') document.getElementById('btn-charge').classList.add('active');
  else if (s.mode === 'auto') document.getElementById('btn-auto').classList.add('active');

  // Auto plan card: show only in auto mode
  const cardAuto = document.getElementById('card-auto');
  cardAuto.style.display = s.mode === 'auto' ? '' : 'none';
  if (s.auto_status) renderAutoStatus(s.auto_status, s);

  document.getElementById('b-maxdis').textContent = (s.max_discharge_w ?? '–') + ' W';
  if (s.max_discharge_w) document.getElementById('max-dis-w').value = s.max_discharge_w;

  const sm = s.sample;
  if (sm) {
    document.getElementById('p-a').innerHTML = w(sm.phase_a_w);
    document.getElementById('p-b').innerHTML = w(sm.phase_b_w);
    document.getElementById('p-c').innerHTML = w(sm.phase_c_w);
    const total = (sm.phase_a_w||0)+(sm.phase_b_w||0)+(sm.phase_c_w||0);
    const cons = total + (sm.battery_output_w||0);
    document.getElementById('p-total').innerHTML = w(total);
    document.getElementById('p-cons').innerHTML = w(cons);
    document.getElementById('b-out').innerHTML = w(sm.battery_output_w);
    document.getElementById('b-in').innerHTML = w(sm.charge_input_w);
    document.getElementById('b-soc').textContent = sm.soc_percent != null ? sm.soc_percent + ' %' : '–';
    push(gridHist.a, sm.phase_a_w);
    push(gridHist.b, sm.phase_b_w);
    push(gridHist.c, sm.phase_c_w);
    push(gridHist.total, total);
    push(battHist.out, sm.battery_output_w);
    push(battHist.soc, sm.soc_percent);
    drawChart('grid-chart', [
      {data:gridHist.a, color:'#4f8ef7'},
      {data:gridHist.b, color:'#22c55e'},
      {data:gridHist.c, color:'#eab308'},
    ]);
    drawChart('batt-chart', [
      {data:battHist.out, color:'#a78bfa'},
    ]);
  }

  const c = s.control;
  if (c) {
    document.getElementById('ctrl-name').textContent = c.regulator_name;
    document.getElementById('ctrl-sp').textContent = c.setpoint_w + ' W';
    document.getElementById('ctrl-raw').innerHTML = c.raw_target_w != null ? Number(c.raw_target_w).toFixed(0) + ' W' : '–';
    document.getElementById('ctrl-ff').innerHTML = c.ff_output_w != null ? Number(c.ff_output_w).toFixed(0) + ' W' : '–';
    document.getElementById('ctrl-fb').innerHTML = c.feedback_output_w != null ? Number(c.feedback_output_w).toFixed(0) + ' W' : '–';
    document.getElementById('ctrl-osc').textContent = c.osc_limit_w != null ? Number(c.osc_limit_w).toFixed(0) + ' W' : '∞';
    renderOsc('osc-a', 'osc-a-lim', c.osc_a);
    renderOsc('osc-b', 'osc-b-lim', c.osc_b);
    renderOsc('osc-c', 'osc-c-lim', c.osc_c);
    renderOsc('osc-tot', 'osc-tot-lim', c.osc_total);
  } else {
    ['ctrl-name','ctrl-sp','ctrl-raw','ctrl-ff','ctrl-fb','ctrl-osc'].forEach(id => {
      document.getElementById(id).textContent = '–';
    });
  }
}

function renderOsc(badgeId, limId, osc) {
  if (!osc) return;
  const b = document.getElementById(badgeId);
  b.textContent = osc.oscillating ? 'OSZ!' : 'OK';
  b.className = 'badge ' + (osc.oscillating ? 'badge-red' : 'badge-green');
  document.getElementById(limId).textContent = osc.limit_w != null ? Number(osc.limit_w).toFixed(0) + ' W' : '';
}

// ── Mini sparkline chart ───────────────────────────────────────────────────
function push(arr, val) {
  arr.push(val ?? 0);
  if (arr.length > HISTORY) arr.shift();
}

function drawChart(id, series) {
  const canvas = document.getElementById(id);
  if (!canvas) return;
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const W = rect.width * dpr, H = rect.height * dpr;
  if (canvas.width !== W || canvas.height !== H) {
    canvas.width = W; canvas.height = H;
  }
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, W, H);
  ctx.strokeStyle = '#2a2d3e'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(0, H/2); ctx.lineTo(W, H/2); ctx.stroke();
  const allVals = series.flatMap(s => s.data);
  const mn = Math.min(...allVals, -1), mx = Math.max(...allVals, 1);
  const range = mx - mn || 1;
  for (const {data, color} of series) {
    if (!data.length) continue;
    ctx.strokeStyle = color; ctx.lineWidth = 1.5 * dpr;
    ctx.beginPath();
    data.forEach((v, i) => {
      const x = (i / (HISTORY - 1)) * W;
      const y = H - ((v - mn) / range) * H;
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.stroke();
  }
}

// ── Regulator panel ────────────────────────────────────────────────────────
async function fetchRegulators() {
  const res = await fetch('/api/regulators');
  regulators = await res.json();
  const sel = document.getElementById('reg-select');
  sel.innerHTML = regulators.map(r =>
    `<option value="${r.name}" ${r.is_active?'selected':''}>${r.name}</option>`
  ).join('');
  renderRegSettings();
}

function selectRegulator() {
  const name = document.getElementById('reg-select').value;
  fetch('/api/regulators/select', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({name})
  }).then(() => { fetchRegulators(); showToast('Regler gewechselt'); });
}

function renderRegSettings() {
  const name = document.getElementById('reg-select').value;
  const reg = regulators.find(r => r.name === name);
  if (!reg) return;
  document.getElementById('reg-desc').textContent = reg.description || '';

  // Split schema fields by group into 3 containers
  const containers = {
    'Regler':                       document.getElementById('reg-settings'),
    'Holder (kurze Schwingungen)':  document.getElementById('holder-settings'),
    'Predictor (periodische Lasten)': document.getElementById('predictor-settings'),
  };
  Object.values(containers).forEach(el => { if (el) el.innerHTML = ''; });

  // Track which extra cards have any fields
  const hasHolder = { v: false };
  const hasPredictor = { v: false };

  for (const [key, def] of Object.entries(reg.settings_schema)) {
    const g = def.group || 'Regler';
    const target = containers[g] || containers['Regler'];
    if (g.startsWith('Holder'))    hasHolder.v = true;
    if (g.startsWith('Predictor')) hasPredictor.v = true;
    const val = (reg.current_settings[key] ?? def.default ?? '');
    if (def.type === 'boolean') {
      target.innerHTML += `<label style="margin-top:6px">
        <input type="checkbox" id="s_${key}" ${val?'checked':''}
          style="width:auto;margin-right:6px"> ${def.title}</label>`;
    } else {
      target.innerHTML += `<label>${def.title}</label>
        <input type="number" id="s_${key}" value="${val}"
          min="${def.minimum??''}" max="${def.maximum??''}" step="${def.step??'any'}"/>`;
    }
  }

  // Show/hide the extra cards depending on whether the regulator has those groups
  document.getElementById('card-holder').style.display    = hasHolder.v    ? '' : 'none';
  document.getElementById('card-predictor').style.display = hasPredictor.v ? '' : 'none';
}

function applySettings() {
  const name = document.getElementById('reg-select').value;
  const reg = regulators.find(r => r.name === name);
  if (!reg) return;
  const settings = {};
  for (const [key, def] of Object.entries(reg.settings_schema)) {
    const el = document.getElementById('s_' + key);
    if (!el) continue;
    settings[key] = def.type === 'boolean' ? el.checked : Number(el.value);
  }
  fetch(`/api/regulators/${name}/settings`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(settings)
  }).then(() => { fetchRegulators(); showToast('Einstellungen gespeichert'); });
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
  if (mode === 'discharge_zero_feed') {
    body.max_discharge_w = parseInt(document.getElementById('max-dis-w').value);
  }
  fetch('/api/mode', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body)
  });
}

async function activateAuto() {
  const broker = document.getElementById('auto-broker').value.trim();
  const device_id = document.getElementById('auto-device-id').value.trim();
  const topic_prefix = document.getElementById('auto-topic').value.trim() || 'gridpythia';
  const status_interval_s = parseFloat(document.getElementById('auto-interval').value) || 60;
  if (!broker || !device_id) { showToast('Broker und Device ID erforderlich'); return; }
  const res = await fetch('/api/auto/connect', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({mqtt_broker: broker, device_id, topic_prefix, status_interval_s})
  });
  if (res.ok) {
    document.getElementById('auto-panel').style.display = 'none';
    showToast('Auto-Modus aktiviert');
  } else {
    const err = await res.json().catch(() => ({}));
    showToast('Fehler: ' + (err.detail || res.status));
  }
}

async function deactivateAuto() {
  await fetch('/api/auto/disconnect', {method:'POST'});
  document.getElementById('auto-panel').style.display = 'none';
  showToast('Auto-Modus deaktiviert');
}

// ── Auto status / plan rendering ───────────────────────────────────────────
function renderAutoStatus(as, fullState) {
  const connEl = document.getElementById('auto-conn-badge');
  if (as.connected) {
    connEl.textContent = as.has_plan ? 'Verbunden ✓ Plan' : 'Verbunden (kein Plan)';
    connEl.className = as.has_plan ? 'badge badge-green' : 'badge badge-yellow';
  } else {
    connEl.textContent = 'Getrennt';
    connEl.className = 'badge badge-red';
  }
  document.getElementById('auto-plan-ts').textContent = as.plan_published_at || '–';
  const effEl = document.getElementById('auto-effective');
  effEl.textContent = as.effective_mode || '–';
  effEl.className = 'badge ' + effectiveBadgeClass(as.effective_mode);

  const zfiEl = document.getElementById('auto-zfi-state');
  const pausedLow = !!fullState?.zfi_paused_low_soc;
  const pausedFull = !!fullState?.zfi_paused_full_battery;
  const eff = as.effective_mode || '';
  const zfiEffective = eff.includes('Zero-Feed') || eff.includes('Entladen');
  if (pausedLow) {
    zfiEl.textContent = 'Pausiert (SoC-Minimum)';
    zfiEl.className = 'badge badge-yellow';
  } else if (pausedFull) {
    zfiEl.textContent = 'Pausiert (Batterie voll)';
    zfiEl.className = 'badge badge-yellow';
  } else if (zfiEffective) {
    zfiEl.textContent = 'Aktiv';
    zfiEl.className = 'badge badge-green';
  } else {
    zfiEl.textContent = 'Nicht aktiv';
    zfiEl.className = 'badge badge-gray';
  }

  const list = document.getElementById('plan-list');
  if (!as.plan_summary || as.plan_summary.length === 0) {
    list.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:6px 0">Kein Plan verfügbar</div>';
    return;
  }
  list.innerHTML = as.plan_summary.map(e => {
    const dateStr = e.date ? `<span style="color:var(--muted);font-size:11px">${e.date} </span>` : '';
    const timeStr = `${e.from_time}–${e.to_time}`;
    let pwrStr = '';
    if (e.power_w != null) {
      pwrStr = `${e.power_w} W`;
    } else if (e.mode_label === 'Zero-Feed' || e.mode_label === 'Entladen') {
      pwrStr = 'HW-Limit';
    }
    const icon = planModeIcon(e.mode_label);
    return `<div class="plan-entry">
      <span class="plan-time">${dateStr}${timeStr}</span>
      <span class="plan-mode">${icon} ${e.mode_label}</span>
      <span class="plan-pwr">${pwrStr}</span>
    </div>`;
  }).join('');
}

function effectiveBadgeClass(label) {
  if (!label || label === '–') return 'badge-gray';
  if (label.includes('Lade')) return 'badge-yellow';
  if (label.includes('Fallback') || label === 'Idle') return 'badge-gray';
  return 'badge-green';
}

function planModeIcon(label) {
  if (!label) return '';
  if (label.includes('Laden')) return '⬆';
  if (label.includes('Zero-Feed') || label.includes('Entladen')) return '⬇';
  return '⏸';
}

function applyMaxDis() {
  const max_discharge_w = parseInt(document.getElementById('max-dis-w').value);
  fetch('/api/mode', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({mode: state?.mode || 'idle', max_discharge_w})
  }).then(() => showToast('Limit gesetzt'));
}

function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = '✓ ' + msg;
  t.style.display = 'block';
  setTimeout(() => t.style.display = 'none', 2500);
}

// ── Boot ───────────────────────────────────────────────────────────────────
connect();
document.getElementById('reg-select').addEventListener('change', renderRegSettings);
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
