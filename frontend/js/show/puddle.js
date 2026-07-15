// The Puddle — the clean Show view.
//
// Each active person is a blob placed on a phase ring (angle = oscillator phase).
// When people synchronize, their phases converge and the blobs clump into one
// pulsing mass. Edge brightness between blobs = pairwise synchrony; the central
// bloom = group cohesion (Kuramoto order parameter). Roaming bands fade out/in.

import { getFrame, isConnected } from "../store.js";

const canvas = document.getElementById("puddle");
const ctx = canvas.getContext("2d");

// Theme colors come from theme.css (single source of truth), resolved for canvas.
function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
let TH = readTheme();
function readTheme() {
  return {
    bg: cssVar("--show-bg") || "#150a10",
    bloom: cssVar("--bloom-rgb") || "255,150,110",
    edge: cssVar("--edge-rgb") || "255,185,150",
    warn: cssVar("--warn") || "#e0a83c",
  };
}

// Local per-person animation state so pulsing is smooth between 10 Hz frames.
const anim = new Map(); // person_id -> {phase, hr, alpha, x, y, color, name}

function resize() {
  const dpr = window.devicePixelRatio || 1;
  canvas.width = innerWidth * dpr;
  canvas.height = innerHeight * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
addEventListener("resize", () => { resize(); TH = readTheme(); });
resize();

let last = performance.now();

function targetAlpha(p) {
  if (p.enrollment !== "active") return 0;
  switch (p.connection) {
    case "connected": return 1;
    case "stale": return 0.55;
    case "reconnecting": return 0.35;
    case "disconnected": return 0.12;
    default: return 0.5;
  }
}

function frameTick(nowMs) {
  const dt = Math.min(0.1, (nowMs - last) / 1000);
  last = nowMs;
  const frame = getFrame();

  const W = innerWidth, H = innerHeight;
  const cx = W / 2, cy = H / 2;
  const ring = Math.min(W, H) * 0.30;

  // Backdrop.
  ctx.fillStyle = TH.bg;
  ctx.fillRect(0, 0, W, H);

  const people = (frame?.people || []).filter((p) => p.enrollment === "active");
  const order = frame?.synchrony?.order_param ?? 0;
  const cohesion = frame?.synchrony?.cohesion ?? 0;
  const ids = frame?.synchrony?.person_ids || [];
  const matrix = frame?.synchrony?.matrix || [];

  // Central bloom scales with group cohesion / order parameter.
  const bloom = Math.max(order, (cohesion + 1) / 2);
  const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, ring * (0.6 + bloom));
  g.addColorStop(0, `rgba(${TH.bloom},${0.05 + 0.30 * bloom})`);
  g.addColorStop(1, `rgba(${TH.bloom},0)`);
  ctx.fillStyle = g;
  ctx.beginPath();
  ctx.arc(cx, cy, ring * (0.6 + bloom), 0, Math.PI * 2);
  ctx.fill();

  // Reconcile animation state with the latest frame.
  const seen = new Set();
  for (const p of people) {
    seen.add(p.person_id);
    let a = anim.get(p.person_id);
    if (!a) {
      a = { phase: p.phase ?? 0, hr: p.hr ?? 60, alpha: 0, color: p.color, name: p.display_name };
      anim.set(p.person_id, a);
    }
    a.color = p.color;
    a.name = p.display_name;
    a.hr = p.hr ?? a.hr;
    // Advance local phase by heart rate; nudge toward the server phase.
    a.phase += (a.hr / 60) * 2 * Math.PI * dt;
    if (typeof p.phase === "number") {
      let err = p.phase - (a.phase % (2 * Math.PI));
      while (err > Math.PI) err -= 2 * Math.PI;
      while (err < -Math.PI) err += 2 * Math.PI;
      a.phase += 0.15 * err;
    }
    a.targetAlpha = targetAlpha(p);
    a.alpha += (a.targetAlpha - a.alpha) * Math.min(1, dt * 3);
  }
  for (const id of [...anim.keys()]) if (!seen.has(id)) {
    const a = anim.get(id);
    a.alpha += (0 - a.alpha) * Math.min(1, dt * 3);
    if (a.alpha < 0.02) anim.delete(id);
  }

  // Position each blob on the ring by its phase; compute pulse from phase.
  const pos = new Map();
  for (const p of people) {
    const a = anim.get(p.person_id);
    if (!a) continue;
    const ang = a.phase % (2 * Math.PI);
    const x = cx + ring * Math.cos(ang);
    const y = cy + ring * Math.sin(ang);
    a.x = x; a.y = y;
    pos.set(p.person_id, a);
  }

  // Synchrony edges.
  for (let i = 0; i < ids.length; i++) {
    for (let j = i + 1; j < ids.length; j++) {
      const s = matrix[i]?.[j] ?? 0;
      if (s <= 0.05) continue;
      const ai = pos.get(ids[i]), aj = pos.get(ids[j]);
      if (!ai || !aj) continue;
      ctx.strokeStyle = `rgba(${TH.edge},${0.5 * s * Math.min(ai.alpha, aj.alpha)})`;
      ctx.lineWidth = 1 + 3 * s;
      ctx.beginPath();
      ctx.moveTo(ai.x, ai.y);
      ctx.lineTo(aj.x, aj.y);
      ctx.stroke();
    }
  }

  // Blobs.
  for (const a of pos.values()) {
    const beatPulse = 0.5 + 0.5 * Math.cos(a.phase % (2 * Math.PI)); // 1 at beat
    const r = 14 + 16 * beatPulse;
    ctx.globalAlpha = a.alpha;
    const bg = ctx.createRadialGradient(a.x, a.y, 0, a.x, a.y, r);
    bg.addColorStop(0, a.color);
    bg.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = bg;
    ctx.beginPath();
    ctx.arc(a.x, a.y, r, 0, Math.PI * 2);
    ctx.fill();
    ctx.globalAlpha = 1;
  }

  // Connection banner.
  if (!isConnected()) {
    ctx.fillStyle = TH.warn;
    ctx.font = "16px system-ui, sans-serif";
    ctx.fillText("reconnecting…", 20, 30);
  }

  requestAnimationFrame(frameTick);
}
requestAnimationFrame(frameTick);
