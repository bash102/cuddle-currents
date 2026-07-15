// Ops view — the technical status running in parallel with the Show view.
// Panels: server/controls, unassigned devices (enrollment), per-person cards
// (connection / raw signal + quality / abstract), and the synchrony heatmap.

import { subscribe, isConnected } from "../store.js";
import { post } from "../ws.js";

// Theme colors resolved from theme.css (with fallbacks so there's no load race).
function cssVar(n) {
  return getComputedStyle(document.documentElement).getPropertyValue(n).trim();
}
let _theme = null;
function theme() {
  if (_theme && _theme.good) return _theme;
  _theme = {
    good: cssVar("--good") || "#3fae7a",
    warn: cssVar("--warn") || "#e0a83c",
    recon: cssVar("--recon") || "#e8663f",
    bad: cssVar("--bad") || "#c0506a",
    scan: cssVar("--scan") || "#9b5de5",
    trace: cssVar("--trace") || "#ffb27a",
    border: cssVar("--border") || "#3d2230",
    heatNeg: cssVar("--heat-neg") || "#3b6fe0",
    heatMid: cssVar("--heat-mid") || "#241019",
    heatPos: cssVar("--heat-pos") || "#ff8a6b",
  };
  return _theme;
}
function connColor(state) {
  const t = theme();
  return {
    connected: t.good, stale: t.warn, reconnecting: t.recon,
    disconnected: t.bad, connecting: t.scan, scanning: t.scan,
  }[state] || t.bad;
}
const ENROLL_LABEL = {
  discovered: "discovered", assigned: "assigned", baselining: "baselining…",
  calibrated: "calibrated", active: "active", retired: "retired",
};

const el = (id) => document.getElementById(id);
const peopleNodes = new Map();
const deviceNodes = new Map();

// ---- controls ---------------------------------------------------------------

function initControls() {
  el("syncMode").addEventListener("change", (e) =>
    post("/api/sync-mode", { mode: e.target.value }));
  el("scenario").addEventListener("change", (e) =>
    post("/api/scenario", { scenario: e.target.value }));
}

// ---- unassigned devices (enrollment) ---------------------------------------

function reconcileDevices(devices) {
  const wrap = el("unassigned");
  const seen = new Set();
  for (const d of devices) {
    seen.add(d.device_id);
    let node = deviceNodes.get(d.device_id);
    if (!node) {
      node = document.createElement("div");
      node.className = "device";
      node.innerHTML = `
        <div class="devhead"><span class="devid"></span><span class="devhr"></span></div>
        <div class="enrollrow">
          <input class="nameinput" placeholder="name…" />
          <button class="enrollbtn">Enroll</button>
        </div>`;
      node.querySelector(".enrollbtn").addEventListener("click", async () => {
        const name = node.querySelector(".nameinput").value.trim() || d.device_id;
        await post("/api/enroll", { device_id: d.device_id, display_name: name });
      });
      deviceNodes.set(d.device_id, node);
      wrap.appendChild(node);
    }
    node.querySelector(".devid").textContent = d.device_id;
    node.querySelector(".devhr").textContent = d.hr_bpm != null ? `${d.hr_bpm} bpm` : "—";
  }
  for (const [id, node] of deviceNodes) if (!seen.has(id)) { node.remove(); deviceNodes.delete(id); }
  el("unassignedEmpty").style.display = devices.length ? "none" : "block";
}

// ---- per-person cards -------------------------------------------------------

function reconcilePeople(people) {
  const wrap = el("people");
  const seen = new Set();
  for (const p of people) {
    seen.add(p.person_id);
    let node = peopleNodes.get(p.person_id);
    if (!node) {
      node = makePersonCard(p);
      peopleNodes.set(p.person_id, node);
      wrap.appendChild(node.root);
    }
    updatePersonCard(node, p);
  }
  for (const [id, node] of peopleNodes) if (!seen.has(id)) { node.root.remove(); peopleNodes.delete(id); }
}

function makePersonCard(p) {
  const root = document.createElement("div");
  root.className = "card";
  root.innerHTML = `
    <div class="cardhead">
      <span class="dot"></span>
      <span class="name"></span>
      <span class="conn"></span>
      <span class="enroll"></span>
      <span class="grow"></span>
      <button class="baselinebtn">Baseline</button>
    </div>
    <div class="metrics">
      <div class="metric"><label>HR</label><span class="hr">—</span></div>
      <div class="metric"><label>RMSSD</label><span class="rmssd">—</span></div>
      <div class="metric"><label>ΔHRV</label><span class="hrvd">—</span></div>
    </div>
    <div class="qualwrap"><div class="qualbar"><div class="qualfill"></div></div><span class="qflags"></span></div>
    <div class="tracewrap"><canvas class="trace" width="320" height="48"></canvas><canvas class="dial" width="48" height="48"></canvas></div>
    <div class="baseprog"><div class="basefill"></div></div>`;
  root.querySelector(".baselinebtn").addEventListener("click", () =>
    post("/api/baseline/start", { person_id: p.person_id }));
  return {
    root,
    dot: root.querySelector(".dot"),
    name: root.querySelector(".name"),
    conn: root.querySelector(".conn"),
    enroll: root.querySelector(".enroll"),
    hr: root.querySelector(".hr"),
    rmssd: root.querySelector(".rmssd"),
    hrvd: root.querySelector(".hrvd"),
    qualfill: root.querySelector(".qualfill"),
    qflags: root.querySelector(".qflags"),
    trace: root.querySelector(".trace"),
    dial: root.querySelector(".dial"),
    baseprog: root.querySelector(".baseprog"),
    basefill: root.querySelector(".basefill"),
    baselinebtn: root.querySelector(".baselinebtn"),
  };
}

function updatePersonCard(n, p) {
  const c = connColor(p.connection);
  n.dot.style.background = c;
  n.name.textContent = p.display_name;
  n.conn.textContent = p.connection;
  n.conn.style.color = c;
  n.enroll.textContent = ENROLL_LABEL[p.enrollment] || p.enrollment;
  n.hr.textContent = p.hr != null ? `${p.hr.toFixed(0)}` : "—";
  n.rmssd.textContent = p.rmssd != null ? `${p.rmssd.toFixed(0)} ms` : "—";
  n.hrvd.textContent = p.rmssd_delta != null ? `${p.rmssd_delta >= 0 ? "+" : ""}${p.rmssd_delta.toFixed(0)}%` : "—";

  const q = p.quality ?? 0;
  const t = theme();
  n.qualfill.style.width = `${q * 100}%`;
  n.qualfill.style.background = q > 0.7 ? t.good : q > 0.4 ? t.warn : t.bad;
  n.qflags.textContent = (p.quality_flags || []).join(" ");

  n.baselinebtn.style.display = (p.enrollment === "baselining") ? "none" : "";
  const bp = p.baseline_progress;
  n.baseprog.style.display = bp != null ? "block" : "none";
  if (bp != null) n.basefill.style.width = `${bp * 100}%`;

  drawTrace(n.trace, p.hr_trace_tail || []);
  drawDial(n.dial, p.phase, p.color);
}

function drawTrace(cv, data) {
  const ctx = cv.getContext("2d");
  ctx.clearRect(0, 0, cv.width, cv.height);
  if (data.length < 2) return;
  const vals = data.filter((v) => v != null);
  const min = Math.min(...vals), max = Math.max(...vals);
  const span = Math.max(1, max - min);
  ctx.strokeStyle = theme().trace;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  data.forEach((v, i) => {
    const x = (i / (data.length - 1)) * cv.width;
    const y = cv.height - ((v - min) / span) * (cv.height - 6) - 3;
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  ctx.stroke();
}

function drawDial(cv, phase, color) {
  const ctx = cv.getContext("2d");
  const cx = cv.width / 2, cy = cv.height / 2, r = 18;
  ctx.clearRect(0, 0, cv.width, cv.height);
  ctx.strokeStyle = theme().border;
  ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.stroke();
  if (phase == null) return;
  const x = cx + r * Math.cos(phase), y = cy + r * Math.sin(phase);
  ctx.fillStyle = color || theme().trace;
  ctx.beginPath(); ctx.arc(x, y, 4, 0, Math.PI * 2); ctx.fill();
}

// ---- synchrony heatmap ------------------------------------------------------

function drawHeatmap(sync) {
  const cv = el("heatmap");
  const ctx = cv.getContext("2d");
  ctx.clearRect(0, 0, cv.width, cv.height);
  const ids = sync?.person_ids || [];
  const m = sync?.matrix || [];
  const n = ids.length;
  el("cohesion").textContent = (sync?.cohesion ?? 0).toFixed(2);
  el("orderparam").textContent = (sync?.order_param ?? 0).toFixed(2);
  el("modeShown").textContent = sync?.mode ?? "";
  if (n === 0) return;
  const size = Math.min(cv.width, cv.height);
  const cell = size / n;
  for (let i = 0; i < n; i++) {
    for (let j = 0; j < n; j++) {
      const v = m[i]?.[j] ?? 0;
      ctx.fillStyle = heatColor(v);
      ctx.fillRect(j * cell, i * cell, cell - 1, cell - 1);
    }
  }
}

function hexToRgb(h) {
  h = h.replace("#", "");
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}
function lerp(a, b, t) {
  return a.map((x, i) => Math.round(x + (b[i] - x) * t));
}
function heatColor(v) {
  // Diverging ramp with a neutral midpoint: sapphire (anti-phase) — mid — rose-gold.
  const th = theme();
  const mid = hexToRgb(th.heatMid);
  const end = hexToRgb(v >= 0 ? th.heatPos : th.heatNeg);
  const c = lerp(mid, end, Math.min(1, Math.abs(v)));
  return `rgb(${c[0]}, ${c[1]}, ${c[2]})`;
}

// ---- wire up ----------------------------------------------------------------

function render(frame) {
  el("serverState").textContent = isConnected() ? "connected" : "disconnected";
  el("serverState").style.color = isConnected() ? "#5fbf7f" : "#e0724f";
  el("sourceLabel").textContent = frame.source || "—";
  el("scenarioLabel").textContent = frame.scenario || "—";
  reconcileDevices(frame.unassigned || []);
  reconcilePeople(frame.people || []);
  drawHeatmap(frame.synchrony);
}

initControls();
subscribe(render);
