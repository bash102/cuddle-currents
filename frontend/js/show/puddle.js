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
    panel: cssVar("--panel-2") || "#2e1622",
  };
}

// Human words for the identity channels, so the activation cue can say
// "teal triangle" rather than a hex code.
const COLOR_NAMES = {
  "#3b6fe0": "sapphire", "#e8663f": "coral", "#17a2a2": "teal", "#e0245e": "ruby",
  "#b07914": "gold", "#9b5de5": "amethyst", "#1f9e6f": "emerald", "#c14fa0": "orchid",
};
const SHAPE_WORDS = {
  disc: "circle", ring: "ring", triangle: "triangle", square: "square",
  diamond: "diamond", star: "star", hexagon: "hexagon", plus: "cross",
};
function describeIdentity(p) {
  const col = COLOR_NAMES[(p.color || "").toLowerCase()] || "";
  const sh = SHAPE_WORDS[p.shape] || p.shape || "";
  return [col, sh].filter(Boolean).join(" ");
}

// Hex color -> rgba string (for glow halos).
function hexToRgb(hex) {
  const h = hex.replace("#", "");
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}
function hexA(hex, a) {
  const [r, g, b] = hexToRgb(hex);
  return `rgba(${r},${g},${b},${a})`;
}
function avgRgb(hexes) {
  const s = [0, 0, 0];
  for (const hx of hexes) { const c = hexToRgb(hx); s[0] += c[0]; s[1] += c[1]; s[2] += c[2]; }
  const n = Math.max(1, hexes.length);
  return `${Math.round(s[0] / n)},${Math.round(s[1] / n)},${Math.round(s[2] / n)}`;
}

// Force-directed layout constants, scaled to the viewport. Tuned for gentle motion:
// a hard speed cap (maxSpeed) keeps dots slow; the force ratios set the layout.
//
// Spacing is set by a *target distance* per pair that depends on their concordance:
// highly concordant pairs want to sit close (restMin) and clump, uncorrelated/anti
// pairs want to be far (restMax). The centering pull is deliberately gentle so this
// pairwise spacing dominates — otherwise everything collapses toward the middle and
// non-matching dots never get to spread out.
function FORCE(ring, n) {
  const scale = ring / 0.3; // ~ min(W,H)
  return {
    center: 0.10,                    // gentle centering; pairwise spacing dominates
    repulse: scale * scale * 0.04,   // close-range anti-overlap
    attract: 95,                     // pairwise spring magnitude (beats the center pull)
    rest: scale * 0.055,             // spring width (linear region of the tanh)
    // Target distance is a continuous function of the smoothed concordance s in
    // [-1, 1], monotonically increasing as s falls: sync clumps, 0 is distant, and
    // anti-correlation is more distant still (see the target calc below). Distances are
    // fractions of min(W,H), sized so the max-distance (anti) case reaches ~85% of the
    // way to the edge — the constellation uses the screen instead of huddling centrally.
    restMin: scale * 0.045,          // s >= sHigh : fully-concordant pairs (tight clump)
    restZero: scale * 0.46,          // s  = 0     : uncorrelated (distant)
    restMax: scale * 0.86,           // s  = -1    : anti-correlated (most distant)
    sHigh: 0.55,                     // concordance >= this = full clump
    varLo: 1.0,                      // HR SD (bpm) below this = flat signal, correlation
    varHi: 3.0,                      //   is noise -> distrust; full trust at/above varHi.
    clusterThresh: 0.35,             // concordance above this = same group (for glow)
    mobility: 1.0,                   // overdamped: velocity = force * mobility
    maxSpeed: scale * 0.12,          // px/sec hard cap -> never fast
  };
}

// Union-find clustering: group people whose pairwise concordance exceeds a threshold.
// ``sOf(i, j)`` returns the concordance for a pair (the gated/smoothed value, so the
// glow matches the layout).
function detectClusters(ids, sOf, thresh) {
  const n = ids.length;
  const parent = ids.map((_, i) => i);
  const find = (i) => { while (parent[i] !== i) { parent[i] = parent[parent[i]]; i = parent[i]; } return i; };
  for (let i = 0; i < n; i++) {
    for (let j = i + 1; j < n; j++) {
      if (sOf(i, j) > thresh) parent[find(i)] = find(j);
    }
  }
  const groups = new Map();
  ids.forEach((id, i) => {
    const r = find(i);
    if (!groups.has(r)) groups.set(r, []);
    groups.get(r).push(id);
  });
  return [...groups.values()];
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

// Time-smoothed pairwise concordance driving the layout. The windowed correlation
// of near-flat signals is very noisy — it swings across ±0.9 with a mean near zero
// (spurious peaks from correlating sensor noise, amplified by the lag scan). Reacting
// to each frame would clump *uncorrelated* people on transient spikes; an EMA over
// CONC_TAU keeps only *sustained* concordance, so independent pairs settle toward
// their ~0 mean and spread apart while genuine, held synchrony still gathers.
const concEMA = new Map(); // "pidA|pidB" (sorted) -> smoothed concordance
const CONC_TAU = 8.0;      // seconds

function resize() {
  const dpr = window.devicePixelRatio || 1;
  canvas.width = innerWidth * dpr;
  canvas.height = innerHeight * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
addEventListener("resize", () => { resize(); TH = readTheme(); });
resize();

let last = performance.now();

// Activation cue: announce a person when they become active (enrollment finished, or
// a band handed to them), so each person sees their glyph + seat at hand-off.
const CUE_MS = 5000;
let cues = []; // {name, seat, shape, color, desc, born}
let prevActive = null; // Set of active person_ids; null until the first frame

function detectActivations(people, nowMs) {
  const activeIds = new Set(people.map((p) => p.person_id));
  if (prevActive === null) { prevActive = activeIds; return; } // seed; don't cue on load
  for (const p of people) {
    if (!prevActive.has(p.person_id)) {
      cues.push({
        name: p.display_name, seat: p.seat, shape: p.shape || "disc",
        color: p.color, desc: describeIdentity(p), born: nowMs,
      });
    }
  }
  prevActive = activeIds;
  cues = cues.filter((c) => nowMs - c.born < CUE_MS);
}

function drawCues(nowMs) {
  let y = innerHeight * 0.13;
  for (const c of cues) {
    const age = (nowMs - c.born) / CUE_MS;
    const alpha = age < 0.7 ? 1 : Math.max(0, 1 - (age - 0.7) / 0.3);
    drawCuePill(c, innerWidth / 2, y, alpha);
    y += 58;
  }
}

function drawCuePill(c, midX, y, alpha) {
  ctx.save();
  ctx.globalAlpha = alpha;
  ctx.font = "600 16px system-ui, sans-serif";
  const title = `${c.name}   ·   #${c.seat}`;
  const sub = c.desc;
  ctx.font = "13px system-ui, sans-serif";
  const subW = ctx.measureText(sub).width;
  ctx.font = "600 16px system-ui, sans-serif";
  const titleW = ctx.measureText(title).width;
  const padL = 56, padR = 22, h = 46;
  const w = padL + Math.max(titleW, subW) + padR;
  const x = midX - w / 2;
  if (ctx.roundRect) { ctx.beginPath(); ctx.roundRect(x, y, w, h, 13); }
  else { ctx.beginPath(); ctx.rect(x, y, w, h); }
  ctx.fillStyle = TH.panel;
  ctx.fill();
  ctx.lineWidth = 1.5; ctx.strokeStyle = c.color; ctx.stroke();
  drawGlyph(ctx, c.shape, x + 28, y + h / 2, 12, c.color, { glow: 8, alpha });
  ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";
  ctx.fillStyle = TH.text; ctx.font = "600 16px system-ui, sans-serif";
  ctx.fillText(title, x + padL, y + 20);
  ctx.fillStyle = hexA(TH.text, 0.6); ctx.font = "13px system-ui, sans-serif";
  ctx.fillText(sub, x + padL, y + 37);
  ctx.restore();
}

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
  detectActivations(people, nowMs);
  const ids = frame?.synchrony?.person_ids || [];
  const matrix = frame?.synchrony?.matrix || [];
  const idIndex = new Map(ids.map((id, k) => [id, k]));

  // Reconcile per-person animation state with the latest frame.
  const seen = new Set();
  for (const p of people) {
    seen.add(p.person_id);
    let a = anim.get(p.person_id);
    if (!a) {
      const ang = Math.random() * 2 * Math.PI, rad = ring * (0.25 + 0.25 * Math.random());
      a = {
        pid: p.person_id, phase: p.phase ?? 0, hr: p.hr ?? 60, alpha: 0,
        color: p.color, name: p.display_name,
        x: cx + rad * Math.cos(ang), y: cy + rad * Math.sin(ang),
      };
      anim.set(p.person_id, a);
    }
    a.color = p.color;
    a.name = p.display_name;
    a.shape = p.shape || "disc";
    a.seat = p.seat || 0;
    a.hr = p.hr ?? a.hr;
    a.hrVar = typeof p.hr_var === "number" ? p.hr_var : a.hrVar; // for the flat-signal gate
    // Advance local phase by heart rate; nudge toward the server phase (beat pulse).
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

  // ---- Force-directed layout: matching dots attract, so groups cluster ----
  // Concordance is the attraction between a pair; all dots mildly repel to keep
  // spacing; a weak pull to center keeps the whole thing on screen. Overdamped with a
  // hard speed cap so nothing ever moves fast — clusters ease into place. When two
  // sub-groups each sync internally but not across, they settle into separate clumps.
  const nodes = people.map((p) => anim.get(p.person_id)).filter(Boolean);
  const K = FORCE(ring, nodes.length);
  // How much to trust a person's correlations: 0 when their HR is flat (SD <= varLo),
  // ramping to 1 by varHi. Unknown variance (null) is trusted (don't damp on missing data).
  const varTrust = (a) =>
    a.hrVar == null ? 1 : Math.max(0, Math.min(1, (a.hrVar - K.varLo) / (K.varHi - K.varLo)));
  const fx = new Map(), fy = new Map();
  for (const a of nodes) { fx.set(a, (cx - a.x) * K.center); fy.set(a, (cy - a.y) * K.center); }
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      const ai = nodes[i], aj = nodes[j];
      let dx = aj.x - ai.x, dy = aj.y - ai.y;
      let d = Math.hypot(dx, dy) || 0.001;
      const ux = dx / d, uy = dy / d;
      const rep = K.repulse / (d * d);           // close-range anti-overlap
      const ki = idIndex.get(ai.pid), kj = idIndex.get(aj.pid);
      const raw = (ki != null && kj != null) ? (matrix[ki]?.[kj] ?? 0) : 0;
      // Flat-signal gate: a correlation is only trustworthy if BOTH hearts actually
      // vary — near-flat HR (SD ~ the sensor noise floor) yields spurious correlations.
      // Genuine breathing sync raises HR variability (RSA), so it stays trusted while
      // resting/independent people (low SD) have their concordance damped toward 0.
      const gated = raw * Math.min(varTrust(ai), varTrust(aj));
      // EMA-smooth so only sustained sync moves the layout (kills transient spikes).
      const key = ai.pid < aj.pid ? ai.pid + "|" + aj.pid : aj.pid + "|" + ai.pid;
      const prev = concEMA.get(key);
      const s = prev == null ? gated : prev + (gated - prev) * (1 - Math.exp(-dt / CONC_TAU));
      concEMA.set(key, s);
      // The smoothed concordance sets the pair's target distance, continuously:
      //   s >= sHigh -> restMin  (genuine sync: tight clump)
      //   s  = 0     -> restZero (uncorrelated: distant)
      //   s  = -1    -> restMax  (anti-correlated: most distant)
      // so 0 already sits far apart and anti-phase sits farther still, and the gap a
      // pair holds is a readout of their correlation over the smoothing window. A
      // spring toward that target pulls matching dots in and pushes the rest out; tanh
      // keeps the force bounded so motion stays gentle and clusters settle.
      let target;
      if (s >= K.sHigh) {
        target = K.restMin;
      } else if (s >= 0) {
        const u = s / K.sHigh;                    // 1 at sHigh -> 0 at s=0
        target = K.restZero + (K.restMin - K.restZero) * u;
      } else {
        const u = Math.min(1, -s);                // 0 at s=0 -> 1 at s=-1
        target = K.restZero + (K.restMax - K.restZero) * u;
      }
      const stretch = Math.tanh((d - target) / (K.rest * 1.2));
      const f = K.attract * stretch - rep;       // + => together, - => apart
      fx.set(ai, fx.get(ai) + f * ux); fy.set(ai, fy.get(ai) + f * uy);
      fx.set(aj, fx.get(aj) - f * ux); fy.set(aj, fy.get(aj) - f * uy);
    }
  }
  for (const a of nodes) {
    let vx = fx.get(a) * K.mobility, vy = fy.get(a) * K.mobility;
    const sp = Math.hypot(vx, vy);
    if (sp > K.maxSpeed) { vx = vx / sp * K.maxSpeed; vy = vy / sp * K.maxSpeed; }
    a.x += vx * dt; a.y += vy * dt;
    a.x = Math.max(30, Math.min(W - 30, a.x));
    a.y = Math.max(30, Math.min(H - 30, a.y));
  }
  const pos = new Map(nodes.map((a) => [a.pid, a]));

  // Prune smoothed-concordance memory for pairs whose people have left (only when it
  // has grown past the current pair count, so this stays cheap).
  if (concEMA.size > nodes.length * nodes.length) {
    const live = new Set(nodes.map((a) => a.pid));
    for (const k of concEMA.keys()) {
      const [x, y] = k.split("|");
      if (!live.has(x) || !live.has(y)) concEMA.delete(k);
    }
  }

  // Gated, smoothed concordance for a pair — the same value that drives the spacing,
  // so the edges and cluster glow below agree with the layout rather than re-reading
  // the raw noisy matrix (which would draw faint "sync" between people we've spread).
  const sAt = (i, j) => {
    const a = ids[i], b = ids[j];
    return concEMA.get(a < b ? a + "|" + b : b + "|" + a) ?? 0;
  };

  // ---- Cluster detection (union-find on concordance) + per-group glow ----
  const clusters = detectClusters(ids, sAt, K.clusterThresh);
  for (const grp of clusters) {
    const members = grp.map((id) => pos.get(id)).filter(Boolean);
    if (members.length < 2) continue;
    const gx = members.reduce((s, a) => s + a.x, 0) / members.length;
    const gy = members.reduce((s, a) => s + a.y, 0) / members.length;
    let R = 0;
    for (const a of members) R = Math.max(R, Math.hypot(a.x - gx, a.y - gy));
    R += 46;
    const alpha = 0.10 + 0.06 * Math.min(members.length, 5);
    const rgb = avgRgb(members.map((a) => a.color));
    const glow = ctx.createRadialGradient(gx, gy, 0, gx, gy, R);
    glow.addColorStop(0, `rgba(${rgb},${alpha})`);
    glow.addColorStop(1, `rgba(${rgb},0)`);
    ctx.fillStyle = glow;
    ctx.beginPath(); ctx.arc(gx, gy, R, 0, 2 * Math.PI); ctx.fill();
  }

  // Synchrony edges (bright within a matching group).
  for (let i = 0; i < ids.length; i++) {
    for (let j = i + 1; j < ids.length; j++) {
      const s = sAt(i, j);
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

  // Activation cues (drawn on top).
  drawCues(nowMs);

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
