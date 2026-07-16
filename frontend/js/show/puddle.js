// The Puddle — the clean Show view.
//
// Each active person is a blob placed on a phase ring (angle = oscillator phase).
// When people synchronize, their phases converge and the blobs clump into one
// pulsing mass. Edge brightness between blobs = pairwise synchrony; the central
// bloom = group cohesion (Kuramoto order parameter). Roaming bands fade out/in.

import { getFrame, isConnected } from "../store.js";
import { drawGlyph, initials } from "../shapes.js";

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
    text: cssVar("--text") || "#f2e4de",
  };
}

// Hex color -> rgba string (for glow halos).
function hexA(hex, a) {
  const h = hex.replace("#", "");
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${a})`;
}

// Label toggle: press L to cycle none -> initials -> seat number. Off by default so
// the Show view stays clean; people can still find their dot by color x shape.
let labelMode = "none";
const LABEL_CYCLE = { none: "initials", initials: "seat", seat: "none" };
addEventListener("keydown", (e) => {
  if (e.key === "l" || e.key === "L") labelMode = LABEL_CYCLE[labelMode];
});

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
    a.shape = p.shape || "disc";
    a.seat = p.seat || 0;
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

  // Each person keeps a *stable seat* around the ring (no orbital spin — the beat is
  // shown as an in-place pulse). As group cohesion rises, everyone eases inward and
  // gathers into a tighter puddle; when out of sync they spread back to the rim.
  const ordered = [...people].sort((x, y) => (x.seat || 0) - (y.seat || 0));
  const N = ordered.length;
  const targetRadius = ring * (1 - 0.5 * bloom);
  const pos = new Map();
  ordered.forEach((p, rank) => {
    const a = anim.get(p.person_id);
    if (!a) return;
    const targetAng = -Math.PI / 2 + (2 * Math.PI * rank) / Math.max(1, N);
    if (a.ang === undefined) { a.ang = targetAng; a.rad = targetRadius; }
    // ease angle along the shortest path so joins/leaves glide rather than jump
    let d = targetAng - a.ang;
    while (d > Math.PI) d -= 2 * Math.PI;
    while (d < -Math.PI) d += 2 * Math.PI;
    a.ang += d * Math.min(1, dt * 2);
    a.rad += (targetRadius - a.rad) * Math.min(1, dt * 1.5);
    a.x = cx + a.rad * Math.cos(a.ang);
    a.y = cy + a.rad * Math.sin(a.ang);
    pos.set(p.person_id, a);
  });

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

  // Blobs: a soft color halo + a crisp per-person glyph (color x shape identity).
  for (const a of pos.values()) {
    const beatPulse = 0.5 + 0.5 * Math.cos(a.phase % (2 * Math.PI)); // 1 at beat
    const r = 13 + 11 * beatPulse;
    // halo
    ctx.globalAlpha = a.alpha;
    const halo = ctx.createRadialGradient(a.x, a.y, 0, a.x, a.y, r * 1.9);
    halo.addColorStop(0, hexA(a.color, 0.45));
    halo.addColorStop(1, hexA(a.color, 0));
    ctx.fillStyle = halo;
    ctx.beginPath();
    ctx.arc(a.x, a.y, r * 1.9, 0, Math.PI * 2);
    ctx.fill();
    ctx.globalAlpha = 1;
    // crisp identity glyph
    drawGlyph(ctx, a.shape, a.x, a.y, r, a.color, { glow: 6 + 8 * beatPulse, alpha: a.alpha });
    // optional label
    if (labelMode !== "none") {
      ctx.globalAlpha = a.alpha;
      ctx.fillStyle = TH.text;
      ctx.font = "600 12px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      const text = labelMode === "seat" ? String(a.seat) : initials(a.name);
      ctx.fillText(text, a.x, a.y + r + 5);
      ctx.globalAlpha = 1;
    }
  }
  ctx.textAlign = "left";
  ctx.textBaseline = "alphabetic";

  // Subtle hint + connection banner.
  ctx.fillStyle = hexA(TH.text, 0.28);
  ctx.font = "12px system-ui, sans-serif";
  const hint = labelMode === "none" ? "press L for labels"
    : labelMode === "initials" ? "labels: initials (L)" : "labels: seat # (L)";
  ctx.fillText(hint, 16, H - 16);
  if (!isConnected()) {
    ctx.fillStyle = TH.warn;
    ctx.font = "16px system-ui, sans-serif";
    ctx.fillText("reconnecting…", 16, 28);
  }

  requestAnimationFrame(frameTick);
}
requestAnimationFrame(frameTick);
