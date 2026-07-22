// Ops view — the technical status running in parallel with the Show view.
// Panels: server/controls, unassigned devices (enrollment), per-person cards
// (connection / raw signal + quality / abstract), and the synchrony heatmap.

import { subscribe, isConnected } from "../store.js";
import { post } from "../ws.js";
import { drawGlyph } from "../shapes.js";
import { initGateways, renderGateways } from "./gateways.js";

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

// Card ordering state: active sessions sort above disconnected ones, and a person who
// transitions disconnected -> active (a band just came online / was handed to them)
// jumps to the top. `reconnectSeq` is a monotonic stamp assigned on that transition;
// higher = more recent = higher in the list. People active since load carry stamp 0.
let reconnectCounter = 0;
const reconnectSeq = new Map(); // person_id -> stamp
const wasActive = new Map(); // person_id -> was non-disconnected last frame
let peopleOrderSig = "";
const isActive = (p) => p.connection !== "disconnected";

// Latest roster (all people), so both panels can offer reassignment targets.
let roster = [];
const notRetired = (p) => p.enrollment !== "retired";
const otherPeople = (self) => roster.filter((p) => notRetired(p) && p.person_id !== self);
const parkedPeople = () => roster.filter((p) => notRetired(p) && !p.device_id);

// Repopulate a <select> only when its option set changes and the user isn't using
// it (avoids flicker / fighting an open dropdown at 10 Hz).
function syncSelect(sel, opts) {
  if (document.activeElement === sel) return;
  const sig = opts.map((o) => `${o.value}:${o.label}`).join("|");
  if (sel.dataset.sig === sig) return;
  sel.dataset.sig = sig;
  sel.innerHTML = "";
  for (const o of opts) {
    const el = document.createElement("option");
    el.value = o.value;
    el.textContent = o.label;
    sel.appendChild(el);
  }
}

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
          <input class="nameinput" placeholder="new name…" />
          <button class="enrollbtn">Enroll</button>
        </div>
        <select class="assignsel" title="assign this band to an existing (parked) person"></select>`;
      node.querySelector(".enrollbtn").addEventListener("click", async () => {
        const name = node.querySelector(".nameinput").value.trim() || d.device_id;
        await post("/api/enroll", { device_id: d.device_id, display_name: name });
      });
      const assignSel = node.querySelector(".assignsel");
      assignSel.addEventListener("change", async () => {
        const v = assignSel.value;
        assignSel.value = "__new__";
        assignSel.dataset.sig = "";
        if (v && v !== "__new__") {
          await post("/api/reassign", { device_id: d.device_id, person_id: v });
        }
      });
      deviceNodes.set(d.device_id, node);
      wrap.appendChild(node);
    }
    node.querySelector(".devid").textContent = d.device_id;
    node.querySelector(".devhr").textContent = d.hr_bpm != null ? `${d.hr_bpm} bpm` : "—";
    const parked = parkedPeople();
    const assignSel = node.querySelector(".assignsel");
    assignSel.style.display = parked.length ? "" : "none";
    syncSelect(assignSel, [
      { value: "__new__", label: "assign to parked…" },
      ...parked.map((p) => ({ value: p.person_id, label: `→ ${p.display_name} (#${p.seat})` })),
    ]);
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
    const active = isActive(p);
    if (!reconnectSeq.has(p.person_id)) {
      reconnectSeq.set(p.person_id, 0); // first sighting: below any future reconnects
    } else if (active && wasActive.get(p.person_id) === false) {
      reconnectSeq.set(p.person_id, ++reconnectCounter); // came back -> jump to the top
    }
    wasActive.set(p.person_id, active);
    let node = peopleNodes.get(p.person_id);
    if (!node) {
      node = makePersonCard(p);
      peopleNodes.set(p.person_id, node);
      wrap.appendChild(node.root);
    }
    updatePersonCard(node, p);
  }
  for (const [id, node] of peopleNodes) if (!seen.has(id)) {
    node.root.remove();
    peopleNodes.delete(id);
    reconnectSeq.delete(id);
    wasActive.delete(id);
  }

  // Order: active sessions first (most-recently-reconnected at the very top), then
  // disconnected people by seat. Only touch the DOM when the order actually changes,
  // so an open dropdown mid-interaction isn't yanked around every frame.
  const ordered = [...people].sort((a, b) => {
    const aa = isActive(a), ba = isActive(b);
    if (aa !== ba) return aa ? -1 : 1;
    if (aa && ba) {
      const d = (reconnectSeq.get(b.person_id) || 0) - (reconnectSeq.get(a.person_id) || 0);
      if (d) return d;
    }
    return (a.seat || 0) - (b.seat || 0);
  });
  const sig = ordered.map((p) => p.person_id).join(",");
  if (sig !== peopleOrderSig) {
    peopleOrderSig = sig;
    for (const p of ordered) {
      const node = peopleNodes.get(p.person_id);
      if (node) wrap.appendChild(node.root);
    }
  }
}

function makePersonCard(p) {
  const root = document.createElement("div");
  root.className = "card";
  root.innerHTML = `
    <div class="cardhead">
      <canvas class="glyph" width="22" height="22"></canvas>
      <span class="seat"></span>
      <span class="name"></span>
      <span class="conn"></span>
      <span class="enroll"></span>
      <span class="grow"></span>
      <select class="reassign" title="hand this band to another person, or release it"></select>
      <button class="baselinebtn">Baseline</button>
      <button class="removebtn" title="remove this person from the roster">Remove</button>
    </div>
    <div class="metrics">
      <div class="metric"><label>HR</label><span class="hr">—</span></div>
      <div class="metric"><label>HR± var</label><span class="hrvar">—</span></div>
      <div class="metric"><label>RMSSD</label><span class="rmssd">—</span></div>
      <div class="metric"><label>ΔHRV</label><span class="hrvd">—</span></div>
    </div>
    <div class="qualwrap"><div class="qualbar"><div class="qualfill"></div></div><span class="qflags"></span></div>
    <div class="tracewrap"><canvas class="trace" width="320" height="48"></canvas><canvas class="dial" width="48" height="48"></canvas></div>
    <div class="baseprog"><div class="basefill"></div></div>`;
  root.querySelector(".baselinebtn").addEventListener("click", () =>
    post("/api/baseline/start", { person_id: p.person_id }));
  const n = {
    root,
    data: p, // latest person state, refreshed each frame
    glyph: root.querySelector(".glyph"),
    seat: root.querySelector(".seat"),
    name: root.querySelector(".name"),
    conn: root.querySelector(".conn"),
    enroll: root.querySelector(".enroll"),
    hr: root.querySelector(".hr"),
    hrvar: root.querySelector(".hrvar"),
    rmssd: root.querySelector(".rmssd"),
    hrvd: root.querySelector(".hrvd"),
    qualfill: root.querySelector(".qualfill"),
    qflags: root.querySelector(".qflags"),
    trace: root.querySelector(".trace"),
    dial: root.querySelector(".dial"),
    baseprog: root.querySelector(".baseprog"),
    basefill: root.querySelector(".basefill"),
    baselinebtn: root.querySelector(".baselinebtn"),
    reassign: root.querySelector(".reassign"),
    removebtn: root.querySelector(".removebtn"),
  };
  // Remove a person from the roster (frees their band back to the pool). Two-click
  // confirm — the first click arms it, a second within 3s commits — so a stray click
  // can't drop someone mid-session.
  n.removebtn.addEventListener("click", async () => {
    if (n.removebtn.classList.contains("confirm")) {
      await post("/api/retire", { person_id: n.data.person_id });
    } else {
      n.removebtn.classList.add("confirm");
      n.removebtn.textContent = "Confirm?";
      setTimeout(() => {
        n.removebtn.classList.remove("confirm");
        n.removebtn.textContent = "Remove";
      }, 3000);
    }
  });
  n.reassign.addEventListener("change", async () => {
    const v = n.reassign.value;
    n.reassign.value = "__keep__";
    n.reassign.dataset.sig = "";
    n.reassign.blur();
    const cur = n.data;
    if (v === "__release__") {
      await post("/api/release", { person_id: cur.person_id });
    } else if (v && !v.startsWith("__") && cur.device_id) {
      await post("/api/reassign", { device_id: cur.device_id, person_id: v });
    }
  });
  return n;
}

function updatePersonCard(n, p) {
  n.data = p;
  const c = connColor(p.connection);
  // Reassign control: only meaningful when this person currently holds a band.
  n.reassign.style.display = p.device_id ? "" : "none";
  if (p.device_id) {
    syncSelect(n.reassign, [
      { value: "__keep__", label: "band ▸" },
      { value: "__release__", label: "release band" },
      ...otherPeople(p.person_id).map((o) => ({
        value: o.person_id, label: `→ ${o.display_name} (#${o.seat})`,
      })),
    ]);
  }
  // identity glyph (color x shape) + seat number
  const g = n.glyph.getContext("2d");
  g.clearRect(0, 0, n.glyph.width, n.glyph.height);
  drawGlyph(g, p.shape || "disc", 11, 11, 7, p.color);
  n.seat.textContent = p.seat ? `#${p.seat}` : "";
  n.name.textContent = p.display_name;
  n.conn.textContent = p.connection;
  n.conn.style.color = c;
  n.enroll.textContent = ENROLL_LABEL[p.enrollment] || p.enrollment;
  n.hr.textContent = p.hr != null ? `${p.hr.toFixed(0)}` : "—";
  // HR variability over the sync window. Below ~1.5 bpm the signal is too flat for
  // shape-based synchrony (zscore) to be trustworthy — flag it amber.
  if (p.hr_var != null) {
    n.hrvar.textContent = `${p.hr_var.toFixed(1)}`;
    n.hrvar.style.color = p.hr_var < 1.5 ? theme().warn : "";
    n.hrvar.title = p.hr_var < 1.5 ? "flat signal — zscore/shape sync unreliable; trust raw" : "";
  } else {
    n.hrvar.textContent = "—";
    n.hrvar.style.color = "";
  }
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
  const t = theme();
  el("serverState").textContent = isConnected() ? "connected" : "disconnected";
  el("serverState").style.color = isConnected() ? t.good : t.bad;
  el("sourceLabel").textContent = frame.source || "—";
  el("scenarioLabel").textContent = frame.scenario || "—";
  roster = frame.people || [];
  reconcilePeople(roster);
  reconcileDevices(frame.unassigned || []);
  drawHeatmap(frame.synchrony);
  renderGateways(frame);
}

initControls();
initGateways();
subscribe(render);
