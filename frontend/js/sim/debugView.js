// Temporary data inspector — NOT a preset. A plain-canvas readout that proves the
// mock source produces correct, controllable data: nodes pulsing on the beat, edges
// lit by pairwise concordance, and a concordance heatmap. Replaced by real PixiJS
// presets; kept around as ground-truth to check a preset against.

import { getFrame, subscribe } from "../store.js";
import { CohortTracker, pairKey } from "./cohort.js";

const cohort = new CohortTracker();

const canvas = document.getElementById("view");
const ctx = canvas.getContext("2d");

let onFrame = null;
export function setDebugFrameHook(fn) { onFrame = fn; }

const panelW = () => document.getElementById("simctl")?.offsetWidth || 0;
let lastW = -1;

function resize() {
  const dpr = window.devicePixelRatio || 1;
  const w = window.innerWidth - panelW(); // leave room for the control panel
  lastW = w;
  canvas.width = w * dpr; canvas.height = window.innerHeight * dpr;
  canvas.style.width = w + "px"; canvas.style.height = window.innerHeight + "px";
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
addEventListener("resize", resize);
resize();

subscribe((f) => { if (onFrame) onFrame(f); });

const anim = new Map(); // id -> {phase}
let last = performance.now();

function hexA(hex, a) {
  const h = hex.replace("#", "");
  return `rgba(${parseInt(h.slice(0, 2), 16)},${parseInt(h.slice(2, 4), 16)},${parseInt(h.slice(4, 6), 16)},${a})`;
}

function tick(now) {
  const dt = Math.min(0.1, (now - last) / 1000); last = now;
  const W = window.innerWidth - panelW(), H = window.innerHeight;
  if (W !== lastW) resize(); // panel mounted/resized after import -> refit canvas
  ctx.fillStyle = "#150a10"; ctx.fillRect(0, 0, W, H);

  const frame = getFrame();
  const people = (frame?.people || []).filter((p) => p.enrollment === "active");
  const ids = frame?.synchrony?.person_ids || [];
  const matrix = frame?.synchrony?.matrix || [];
  const idx = new Map(ids.map((id, k) => [id, k]));

  const cx = W / 2, cy = H / 2, R = Math.min(W, H) * 0.34;
  const pos = new Map();
  people.forEach((p, i) => {
    const a = (i / Math.max(1, people.length)) * 2 * Math.PI - Math.PI / 2;
    pos.set(p.person_id, { x: cx + R * Math.cos(a), y: cy + R * Math.sin(a), p });
  });

  // Cohort edges — only drawn once a pair's concordance has been SUSTAINED (via the
  // shared CohortTracker: flat-gate -> EMA -> dwell timer). Transient value overlaps
  // never qualify, so a line means a real, held cohort. Green = in-phase, blue = anti.
  const liveKeys = new Set();
  for (let i = 0; i < ids.length; i++) {
    for (let j = i + 1; j < ids.length; j++) {
      const a = pos.get(ids[i]), b = pos.get(ids[j]);
      if (!a || !b) continue;
      const key = pairKey(ids[i], ids[j]);
      liveKeys.add(key);
      const e = cohort.update(key, matrix[i]?.[j] ?? 0, a.p.hr_var, b.p.hr_var, dt);
      if (e.vis < 0.02) continue;
      const mag = Math.min(1, Math.abs(e.s));
      ctx.strokeStyle = e.s >= 0 ? `rgba(90,220,140,${0.6 * e.vis * mag})` : `rgba(90,150,240,${0.6 * e.vis * mag})`;
      ctx.lineWidth = 0.5 + 3 * mag * e.vis;
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
    }
  }
  cohort.prune(liveKeys);

  // Nodes: pulse on the beat, size by HR deviation, dimmed by connection.
  for (const { x, y, p } of pos.values()) {
    let a = anim.get(p.person_id);
    if (!a) { a = { phase: p.phase ?? 0 }; anim.set(p.person_id, a); }
    a.phase += ((p.hr ?? 60) / 60) * 2 * Math.PI * dt;
    if (typeof p.phase === "number") {
      let err = p.phase - (a.phase % (2 * Math.PI));
      while (err > Math.PI) err -= 2 * Math.PI;
      while (err < -Math.PI) err += 2 * Math.PI;
      a.phase += 0.15 * err;
    }
    const beat = 0.5 + 0.5 * Math.cos(a.phase % (2 * Math.PI));
    const r = 10 + 9 * beat;
    const alpha = p.connection === "connected" ? 1 : p.connection === "stale" ? 0.55 : p.connection === "disconnected" ? 0.2 : 0.35;
    ctx.globalAlpha = alpha;
    const halo = ctx.createRadialGradient(x, y, 0, x, y, r * 2);
    halo.addColorStop(0, hexA(p.color, 0.4)); halo.addColorStop(1, hexA(p.color, 0));
    ctx.fillStyle = halo; ctx.beginPath(); ctx.arc(x, y, r * 2, 0, 2 * Math.PI); ctx.fill();
    ctx.fillStyle = p.color; ctx.beginPath(); ctx.arc(x, y, r, 0, 2 * Math.PI); ctx.fill();
    ctx.globalAlpha = 1;
    ctx.fillStyle = "rgba(255,255,255,0.55)"; ctx.font = "10px system-ui"; ctx.textAlign = "center";
    ctx.fillText(`${p.display_name} ${Math.round(p.hr ?? 0)}`, x, y + r + 12);
  }

  // Concordance heatmap (top-left) — the clearest in/out-of-sync readout.
  const n = ids.length;
  if (n > 0) {
    const cell = Math.min(9, Math.floor(160 / n)), ox = 16, oy = 16;
    for (let i = 0; i < n; i++) for (let j = 0; j < n; j++) {
      const s = matrix[i]?.[j] ?? 0;
      ctx.fillStyle = s >= 0 ? `rgba(90,220,140,${Math.min(1, s)})` : `rgba(90,150,240,${Math.min(1, -s)})`;
      ctx.fillRect(ox + j * cell, oy + i * cell, cell - 1, cell - 1);
    }
    ctx.fillStyle = "rgba(255,255,255,0.5)"; ctx.font = "10px system-ui"; ctx.textAlign = "left";
    ctx.fillText("concordance", ox, oy + n * cell + 12);
  }

  requestAnimationFrame(tick);
}
requestAnimationFrame(tick);
