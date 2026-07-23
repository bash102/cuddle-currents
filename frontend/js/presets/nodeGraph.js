// Node-graph preset — cohort formation with momentum physics + particles.
//
// Idle: floating circles in their own colors, drifting and gently repelling. When hearts
// sync they form a COHORT (connected component of qualified CohortTracker pairs) and a
// per-node time-in-cohort timer drives a lifecycle:
//   1.0s  gravity ramps in (spring toward the cohort centroid)
//   1.5s  a stable MASTER (color identity) is chosen; links appear to rotating partners
//   2.0s  child nodes fade toward the master color
//   3.0s  members scale up to ~120%
// Connecting lines start thin and thicken the longer the bond holds.
//
// Motion is a spring-mass-damper with real velocity: nodes accelerate in, overshoot,
// and wobble to rest (tune with Gravity / Bounce / Damping). Leaving a cohort runs one
// quick unified exit. Particles: every node continuously emits a world-space aura, and a
// one-shot burst fires the moment a node first joins a cohort.
//
// Preset = { container, update(frame, dt), destroy(), params, controls }.

import { Container, Graphics, Sprite, Text, Texture } from "../../vendor/pixi.min.mjs";
import { FilterStack, EventFilters, defaultFilters } from "./filters.js";
import { CohortTracker, pairKey } from "../sim/cohort.js";
import { ParticleSystem, defaultParticleSystems } from "./particles.js";
import { defaultEvents } from "./events.js";
import { Choreographer, nodeFx } from "./dispatch.js";

// Metaball connector — fills a smooth gooey neck between two circles (the classic
// tangent + cubic-bezier blob bridge). Drawn beneath the node cores so cohort mates read
// as one fluid mass. `v` (0..1) is the neck spread: thin strand -> fat bridge.
function metaball(g, x1, y1, r1, x2, y2, r2, color, alpha, v, handle = 2.2) {
  const dx = x2 - x1, dy = y2 - y1, d = Math.hypot(dx, dy);
  if (d < 0.01 || d <= Math.abs(r1 - r2)) return;
  const maxDist = (r1 + r2) * 6;
  if (d > maxDist) return;
  let u1 = 0, u2 = 0;
  if (d < r1 + r2) {
    u1 = Math.acos(Math.min(1, Math.max(-1, (r1 * r1 + d * d - r2 * r2) / (2 * r1 * d))));
    u2 = Math.acos(Math.min(1, Math.max(-1, (r2 * r2 + d * d - r1 * r1) / (2 * r2 * d))));
  }
  const a = Math.atan2(dy, dx);
  const maxSpread = Math.acos(Math.min(1, Math.max(-1, (r1 - r2) / d)));
  const a1 = a + u1 + (maxSpread - u1) * v;
  const a2 = a - u1 - (maxSpread - u1) * v;
  const a3 = a + Math.PI - u2 - (Math.PI - u2 - maxSpread) * v;
  const a4 = a - Math.PI + u2 + (Math.PI - u2 - maxSpread) * v;
  const P = (cx, cy, ang, r) => [cx + r * Math.cos(ang), cy + r * Math.sin(ang)];
  const p1 = P(x1, y1, a1, r1), p2 = P(x1, y1, a2, r1);
  const p3 = P(x2, y2, a3, r2), p4 = P(x2, y2, a4, r2);
  const tot = r1 + r2;
  const d2 = Math.min(v * handle, Math.hypot(p1[0] - p3[0], p1[1] - p3[1]) / tot);
  const h1 = r1 * d2, h2 = r2 * d2;
  const H = (px, py, ang, len) => [px + len * Math.cos(ang), py + len * Math.sin(ang)];
  const c1 = H(p1[0], p1[1], a1 - Math.PI / 2, h1);
  const c3 = H(p3[0], p3[1], a3 + Math.PI / 2, h2);
  const c4 = H(p4[0], p4[1], a4 - Math.PI / 2, h2);
  const c2 = H(p2[0], p2[1], a2 + Math.PI / 2, h1);
  g.moveTo(p1[0], p1[1]);
  g.bezierCurveTo(c1[0], c1[1], c3[0], c3[1], p3[0], p3[1]);
  g.lineTo(p4[0], p4[1]);
  g.bezierCurveTo(c4[0], c4[1], c2[0], c2[1], p2[0], p2[1]);
  g.closePath();
  g.fill({ color, alpha });
}

const R0 = 40; // reference radius the (white) node circle is baked at; tinted + scaled per frame

// Default node sprite = a solid white disc with a soft 1px edge (tinted per node). Used for both
// the core and the halo unless a preset points them at a PNG.
function makeDisc(size = 128) {
  const c = document.createElement("canvas");
  c.width = c.height = size;
  const g = c.getContext("2d"), R = size / 2;
  const grd = g.createRadialGradient(R, R, 0, R, R, R);
  grd.addColorStop(0, "rgba(255,255,255,1)");
  grd.addColorStop(0.92, "rgba(255,255,255,1)");
  grd.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = grd;
  g.beginPath(); g.arc(R, R, R, 0, Math.PI * 2); g.fill();
  return Texture.from(c);
}

// Default edge beam = a horizontal white bar with soft top/bottom edges (tinted per cohort).
// Stretched center-to-center; the node cores render on top and hide its ends.
function makeBeam(w = 128, h = 48) {
  const c = document.createElement("canvas");
  c.width = w; c.height = h;
  const g = c.getContext("2d"), R = h / 2;
  for (let y = 0; y < h; y++) {
    const t = Math.abs(y - R + 0.5) / R;      // 0 center .. 1 edge
    g.fillStyle = `rgba(255,255,255,${Math.max(0, 1 - t * t)})`;
    g.fillRect(0, y, w, 1);
  }
  return Texture.from(c);
}

const CFG = {
  // idle motion
  drift: 6, driftTurn: 1.6, center: 0.04,
  // cohort attraction (spring to centroid) + collision bounce + cohort separation
  gravityK: 2.4,      // pull toward the cohort centroid once ramped
  minDist: 0.06,      // rest spacing / collision-spring rest length (·scale)
  collideK: 30,       // collision-spring stiffness (bounce firmness)
  crossDist: 0.19,    // different cohorts / singletons push apart out to this gap (·scale)
  sepK: 3.0,          // cohort-separation stiffness (de-mixes interleaved groups)
  // momentum
  drag: 3.2,          // velocity damping — low = wobbly/momentum, high = quick settle
  maxSpeed: 0.7,      // hard velocity cap (·scale) for stability
  // lifecycle thresholds (seconds in cohort)
  tGravity: 1.0, tMaster: 1.5, tFade: 2.0, tScale: 3.0,
  gravRamp: 1.0, fadeRamp: 1.0, scaleRamp: 1.0, scaleMax: 1.2,
  // edges: thin -> thick the longer a bond holds
  edgeMinW: 0.8, edgeMaxW: 5.5, edgeGrow: 8.0,
  // edge style: "metaball" (generated gooey neck, edge-to-edge) or "png" (a stretched sprite
  // from node CENTER to node center — set edgeTexture, scale thickness with edgePngWidth).
  edgeStyle: "metaball", edgeTexture: "", edgePngWidth: 14,
  // links (jostle)
  linkMin: 1.4, linkMax: 3.2, linkFade: 0.4, linkPull: 0.4,
  // node graphic: a solid disc (core) + a bigger, dimmer disc (halo), each tinted per node.
  // coreTexture/haloTexture = optional PNG path/URL (blank = generated disc). See particles for
  // the same path convention.
  coreTexture: "", haloTexture: "", haloScale: 1.9, haloAlpha: 0.16, haloOn: true,
  beatPulse: 0, haloPulse: 0,  // intrinsic px heartbeat pulse (both 0 — the HR event drives core + halo)
  // particle systems (named, friendly-param; see particles.js) — referenced by events
  particleSystems: defaultParticleSystems(),
  // events / choreography (see events.js) — reactions bound to renderer events
  events: defaultEvents(),
  // post-process filter stack (data-driven; see filters.js). Array order = stack order.
  filters: defaultFilters(),
  // cohort ambient glow behind each cohort (generated additive circles, or a PNG when set)
  cohortGlowTexture: "", cohortGlowSize: 1.0, cohortGlowAlpha: 1.0, cohortGlowFade: 2.0, cohortGlowOnset: 1.0,
  cohortGlowTween: 6,    // how fast the glow eases to a new position/size on membership change (higher = snappier)
  // colors (hex strings so they bind to color inputs)
  cohortHueSep: 45,      // min hue gap (°) between concurrent cohorts — 0 disables de-confliction
  bg: "#150a10",         // stage background
  labelSolo: "#ffffff",  // label text when a node is alone
  labelCohort: "#0e0a10",// label text when in a cohort (glow supplies contrast)
  // exit
  exitDur: 0.6, grace: 0.3, baseR: 12,
};

// Live-tunable knobs surfaced by the preset-controls panel. Grouped into sections; each
// control has a `type` (range | color | toggle | select). getState/setState capture ALL
// of CFG, so even params not listed here still save — this is just what's editable in-UI.
export const CONTROLS = [
  { group: "Physics", key: "gravityK", label: "Gravity", min: 0, max: 6, step: 0.1,
    tip: "How strongly cohort members are pulled toward their cohort's center. Higher = tighter, faster gathering." },
  { group: "Physics", key: "linkPull", label: "Jostle", min: 0, max: 1.5, step: 0.05,
    tip: "Tug of each member toward its current rotating link partner. Keeps the cohort in motion. 0 = still." },
  { group: "Physics", key: "drag", label: "Damping", min: 0.5, max: 8, step: 0.1,
    tip: "Velocity damping. Low = springy momentum (overshoot + wobble); high = settles quickly." },
  { group: "Physics", key: "collideK", label: "Bounce", min: 5, max: 60, step: 1,
    tip: "Collision-spring stiffness when nodes get too close. Higher = firmer, snappier bounce." },
  { group: "Physics", key: "minDist", label: "Spacing", min: 0.03, max: 0.16, step: 0.005,
    tip: "Settle distance between cohort mates — the collision rest length (× screen). Higher = looser." },
  { group: "Physics", key: "crossDist", label: "Separation", min: 0.05, max: 0.35, step: 0.01,
    tip: "Gap different cohorts / unattached nodes keep between each other." },
  { group: "Physics", key: "sepK", label: "Sep firm", min: 0.5, max: 6, step: 0.1,
    tip: "Firmness of the cohort-separation push (de-mixes interleaved groups)." },

  { group: "Motion", key: "drift", label: "Drift", min: 0, max: 16, step: 0.5,
    tip: "Idle wander force for solo / not-yet-gathered nodes." },
  { group: "Motion", key: "driftTurn", label: "Drift turn", min: 0, max: 4, step: 0.1,
    tip: "How fast the idle wander changes heading (rad/s)." },
  { group: "Motion", key: "center", label: "Centering", min: 0, max: 0.2, step: 0.01,
    tip: "Gentle pull toward screen center that keeps everything on stage." },
  { group: "Motion", key: "maxSpeed", label: "Max speed", min: 0.1, max: 1.5, step: 0.05,
    tip: "Hard velocity cap (× screen) — nothing darts." },

  { group: "Cohort Lifecycle", key: "tGravity", label: "Grav onset", min: 0, max: 4, step: 0.1,
    tip: "Seconds in cohort before gravity ramps in." },
  { group: "Cohort Lifecycle", key: "tMaster", label: "Master @", min: 0, max: 5, step: 0.1,
    tip: "Seconds in cohort before a master is chosen + links appear." },
  // Color-fade, scale-up, and exit unwind moved to the 'Node Joins Cohort' event as MODULATE
  // property reactions (edit their Amount / Onset / Duration there).
  { group: "Cohort Lifecycle", key: "grace", label: "Grace", min: 0, max: 1, step: 0.05,
    tip: "Flicker tolerance before a node counts as having left." },

  { group: "Edges", key: "linkMin", label: "Rewire min", min: 0.5, max: 6, step: 0.5,
    tip: "Shortest time a link holds a partner before rewiring (s)." },
  { group: "Edges", key: "linkMax", label: "Rewire max", min: 0.5, max: 8, step: 0.5,
    tip: "Longest time a link holds a partner before rewiring (s)." },
  { group: "Edges", key: "linkFade", label: "Link fade", min: 0.1, max: 1.5, step: 0.05,
    tip: "Fade in/out time as links rewire (s)." },
  { group: "Edges", key: "edgeGrow", label: "Thicken", min: 1, max: 16, step: 0.5,
    tip: "Seconds over which a cohort's necks fatten toward their max (metaball style)." },
  { group: "Edges", key: "edgeStyle", label: "Edge style", type: "select", options: ["metaball", "png"],
    tip: "metaball = generated gooey neck edge-to-edge; png = a stretched image from node center to node center (set Edge PNG below)." },
  { group: "Edges", key: "edgeTexture", label: "Edge PNG", type: "text", placeholder: "/assets/beam.png",
    emptyLabel: "generated", setLabel: "PNG", pick: { dir: "/assets", exts: ["png", "jpg", "jpeg", "webp", "svg", "gif"] },
    tip: "PNG stretched along each connection (png style). Blank = a generated soft beam. Tinted to the cohort color — use white/grayscale art." },
  { group: "Edges", key: "edgePngWidth", label: "Edge width", min: 2, max: 60, step: 1,
    tip: "Thickness of the PNG edge beam (px)." },

  { group: "Nodes", key: "baseR", label: "Node size", min: 4, max: 30, step: 1,
    tip: "Base radius of the node core (px). Beat pulse + cohort scale-up are added on top." },
  { group: "Nodes", key: "coreTexture", label: "Core PNG", type: "text", placeholder: "/assets/node.png",
    emptyLabel: "generated", setLabel: "PNG", pick: { dir: "/assets", exts: ["png", "jpg", "jpeg", "webp", "svg", "gif"] },
    tip: "Path/URL to a PNG for the node core (blank = generated disc). Tinted to the node/cohort color — use white/grayscale art." },
  { group: "Nodes", key: "haloOn", label: "Halo", type: "toggle",
    tip: "Show the soft outer halo behind each node." },
  { group: "Nodes", key: "haloTexture", label: "Halo PNG", type: "text", placeholder: "/assets/glow.png",
    emptyLabel: "generated", setLabel: "PNG", pick: { dir: "/assets", exts: ["png", "jpg", "jpeg", "webp", "svg", "gif"] },
    tip: "Path/URL to a PNG for the halo (blank = generated soft disc). Tinted to the node/cohort color." },
  { group: "Nodes", key: "haloScale", label: "Halo size", min: 1, max: 4, step: 0.1,
    tip: "Halo diameter relative to the node core." },
  { group: "Nodes", key: "haloAlpha", label: "Halo alpha", min: 0, max: 1, step: 0.02,
    tip: "Opacity of the halo (multiplied by the node's own fade-in)." },
  { group: "Nodes", key: "beatPulse", label: "Beat pulse", min: 0, max: 12, step: 0.5,
    tip: "How much the node core grows on each heartbeat (px), driven by HR. 0 = no pulse." },
  { group: "Nodes", key: "haloPulse", label: "Halo pulse", min: 0, max: 24, step: 0.5,
    tip: "How much the halo grows on each heartbeat (px), independent of the core. 0 = steady halo." },

  { group: "Colors", key: "cohortGlowTexture", label: "Cohort glow PNG", type: "text", placeholder: "/assets/glow.png",
    emptyLabel: "generated", setLabel: "PNG", pick: { dir: "/assets", exts: ["png", "jpg", "jpeg", "webp", "svg", "gif"] },
    tip: "PNG for the soft circle behind each cohort (blank = generated additive circles). Tinted to the cohort color, additive blend — use a white/grayscale radial glow." },
  { group: "Colors", key: "cohortGlowSize", label: "Cohort glow size", min: 0.3, max: 3, step: 0.05,
    tip: "Scale of the cohort glow relative to the cohort's extent." },
  { group: "Colors", key: "cohortGlowAlpha", label: "Cohort glow α", min: 0, max: 3, step: 0.05,
    tip: "Brightness of the cohort glow (× the fade-in strength)." },
  { group: "Colors", key: "cohortGlowOnset", label: "Cohort glow onset", min: 0, max: 5, step: 0.1,
    tip: "Seconds in cohort before the glow begins to appear." },
  { group: "Colors", key: "cohortGlowFade", label: "Cohort glow fade", min: 0.1, max: 8, step: 0.1,
    tip: "Seconds over which the cohort glow ramps from nothing to full (higher = gentler fade-in, no pop)." },
  { group: "Colors", key: "cohortGlowTween", label: "Cohort glow tween", min: 1, max: 20, step: 0.5,
    tip: "How fast the glow eases to its new position/size when a cohort gains or loses a member (higher = snappier, lower = slower glide). It no longer jumps." },
  { group: "Colors", key: "cohortHueSep", label: "Cohort hue sep", min: 0, max: 90, step: 5,
    tip: "Minimum hue gap (°) forced between concurrent cohorts so two groups never read as the same color. The most senior cohort keeps its true color; others rotate away. 0 = off." },
  { group: "Colors", key: "bg", label: "Background", type: "color",
    tip: "Stage background color." },
  { group: "Colors", key: "labelSolo", label: "Label solo", type: "color",
    tip: "Label text color when a node is alone (over black)." },
  { group: "Colors", key: "labelCohort", label: "Label cohort", type: "color",
    tip: "Label text color when in a cohort (the cohort-color glow supplies contrast)." },
];

const ALPHA_BY_CONN = { connected: 1, stale: 0.55, reconnecting: 0.35, disconnected: 0.12 };
const clamp01 = (x) => (x < 0 ? 0 : x > 1 ? 1 : x);
const hexNum = (hex) => parseInt(hex.replace("#", ""), 16);
const rgb = (n) => [(n >> 16) & 255, (n >> 8) & 255, n & 255];
const lerpColor = (a, b, t) => {
  const A = rgb(a), B = rgb(b);
  return ((Math.round(A[0] + (B[0] - A[0]) * t) << 16) +
          (Math.round(A[1] + (B[1] - A[1]) * t) << 8) +
           Math.round(A[2] + (B[2] - A[2]) * t));
};
// HSL round-trip (h in degrees) — used to rotate a cohort's hue away from another cohort's.
const rgbToHsl = (num) => {
  const r = ((num >> 16) & 255) / 255, g = ((num >> 8) & 255) / 255, b = (num & 255) / 255;
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), l = (mx + mn) / 2, d = mx - mn;
  let h = 0, s = 0;
  if (d !== 0) {
    s = d / (1 - Math.abs(2 * l - 1));
    h = (mx === r ? ((g - b) / d) % 6 : mx === g ? (b - r) / d + 2 : (r - g) / d + 4) * 60;
    if (h < 0) h += 360;
  }
  return [h, s, l];
};
const hslToRgb = (h, s, l) => {
  const c = (1 - Math.abs(2 * l - 1)) * s, x = c * (1 - Math.abs(((h / 60) % 2) - 1)), m = l - c / 2;
  let r, g, b;
  if (h < 60) [r, g, b] = [c, x, 0]; else if (h < 120) [r, g, b] = [x, c, 0];
  else if (h < 180) [r, g, b] = [0, c, x]; else if (h < 240) [r, g, b] = [0, x, c];
  else if (h < 300) [r, g, b] = [x, 0, c]; else [r, g, b] = [c, 0, x];
  return ((Math.round((r + m) * 255) << 16) + (Math.round((g + m) * 255) << 8) + Math.round((b + m) * 255));
};
const hueDist = (a, b) => { const d = Math.abs(a - b) % 360; return Math.min(d, 360 - d); };

function unionFind(n) {
  const p = Array.from({ length: n }, (_, i) => i);
  const find = (i) => { while (p[i] !== i) { p[i] = p[p[i]]; i = p[i]; } return i; };
  return { find, union: (a, b) => { p[find(a)] = find(b); } };
}

export function createNodeGraph(app) {
  const container = new Container();
  // Bloom group: everything that should glow (edges/metaballs, particles, node bodies).
  // A real post-process AdvancedBloomFilter runs over it. Labels stay OUT of the group so
  // text renders crisp.
  const bloomGroup = new Container();
  const fstack = new FilterStack(); // the data-driven post-process stack (CFG.filters)
  const eventFX = new EventFilters(); // positioned one-shot filters fired by events (shockwave…)
  let filterSig = "";                 // combined static+event filter signature (reassign on change)
  const glowLayer = new Graphics();   // generated soft ambient under each cohort (bloom amplifies it)
  glowLayer.blendMode = "add";
  const glowSprites = new Container(); // PNG cohort glow when cohortGlowTexture is set
  const glowPool = [];                 // reused glow sprites, one per cohort
  const glows = [];                    // smoothed { x, y, R } per cohort, matched by proximity so
                                       // the glow tweens across member AND master changes
  const edges = new Graphics();       // metaball connector necks (edgeStyle "metaball")
  const edgeSprites = new Container(); // stretched-PNG connectors (edgeStyle "png")
  const edgePool = [];                 // reused edge sprites, one per drawn link
  const field = new ParticleSystem();  // per-node emitters + cohort-join bursts
  field.setSystems(CFG.particleSystems);
  const nodesLayer = new Container();
  bloomGroup.addChild(glowLayer, glowSprites, edges, edgeSprites, field.container, nodesLayer);
  const labelsLayer = new Container();
  container.addChild(bloomGroup, labelsLayer);

  const tracker = new CohortTracker();
  const choreo = new Choreographer(field, eventFX); // fires CFG.events reactions
  const nodes = new Map(); // pid -> record

  // Node core/halo textures: the generated disc by default, or a preset PNG (loaded async with a
  // disc fallback, like the particle textures). applyNodeGraphics() swaps every node's sprite
  // textures when the paths change or a PNG finishes loading.
  const disc = makeDisc(), beam = makeBeam();
  let coreTex = disc, haloTex = disc, edgeTex = beam, glowTex = null; // glowTex null => generated circles
  const texCache = {}; // path -> Texture (loaded) | null (pending) | false (failed)
  function loadTex(path, fallback) {
    if (!path) return fallback;
    const c = texCache[path];
    if (c) return c;
    if (c === undefined) {
      texCache[path] = null;
      const img = new Image(); img.crossOrigin = "anonymous";
      img.onload = () => { try { texCache[path] = Texture.from(img); applyTextures(); } catch { texCache[path] = false; } };
      img.onerror = () => { console.warn(`texture failed to load: ${path}`); texCache[path] = false; };
      img.src = path;
    }
    return fallback; // pending / failed -> the generated fallback
  }
  // Reload node core/halo + edge textures and push them onto the live display objects.
  function applyTextures() {
    coreTex = loadTex(CFG.coreTexture, disc);
    haloTex = loadTex(CFG.haloTexture, disc);
    edgeTex = loadTex(CFG.edgeTexture, beam);
    glowTex = CFG.cohortGlowTexture ? loadTex(CFG.cohortGlowTexture, null) : null; // null -> generated circles
    for (const [, n] of nodes) { n.core.texture = coreTex; n.halo.texture = haloTex; }
    for (const s of edgePool) s.texture = edgeTex;
    for (const s of glowPool) if (glowTex) s.texture = glowTex;
  }
  function edgeSprite(i) {
    if (!edgePool[i]) { const s = new Sprite(edgeTex); s.anchor.set(0, 0.5); edgeSprites.addChild(s); edgePool[i] = s; }
    return edgePool[i];
  }
  function glowSprite(i) {
    let s = glowPool[i];
    if (!s) { s = new Sprite(glowTex); s.anchor.set(0.5); s.blendMode = "add"; glowSprites.addChild(s); glowPool[i] = s; }
    else s.texture = glowTex;
    return s;
  }

  function makeNode(p, w, h) {
    const g = new Container();
    const halo = new Sprite(haloTex); halo.anchor.set(0.5);
    const core = new Sprite(coreTex); core.anchor.set(0.5);
    const label = new Text({ text: p.display_name, style: { fill: 0xffffff, fontSize: 12, fontFamily: "system-ui", fontWeight: "600" } });
    label.anchor.set(0.5, 0);
    g.addChild(halo, core);
    nodesLayer.addChild(g);
    labelsLayer.addChild(label); // outside the bloom group
    return {
      pid: p.person_id, g, halo, core, label,
      x: w / 2 + (Math.random() - 0.5) * w * 0.55,
      y: h / 2 + (Math.random() - 0.5) * h * 0.55,
      vx: 0, vy: 0, driftAngle: Math.random() * 2 * Math.PI, emitAcc: 0,
      phase: p.phase ?? 0, hr: p.hr ?? 60, hrVar: p.hr_var,
      alpha: 0, colorNum: hexNum(p.color), color: p.color, name: p.display_name, seat: p.seat ?? 0,
      cohortTime: 0, outTime: 0, exitT: 0, exitColor: hexNum(p.color), exitScale: 1,
      renderTint: hexNum(p.color), renderScale: 1, master: null, masterColor: null,
      linkTo: null, linkAge: 0, linkDur: 2, labelDark: false, labelGlow: -1,
      // choreography state (event detection): connection, first-seen, last beat bucket, fx mods
      conn: p.connection ?? "connected", prevConn: p.connection ?? "connected",
      everActive: false, lastBeatK: undefined, hold: null, pulse: null,
    };
  }

  function update(frame, dt) {
    const w = app.screen.width, h = app.screen.height;
    const cx = w / 2, cy = h / 2, scale = Math.min(w, h);
    bloomGroup.filterArea = app.screen; // filter over the whole viewport
    app.renderer.background.color = hexNum(CFG.bg); // stage background (per-preset)
    const people = (frame?.people || []).filter((p) => p.enrollment === "active");
    const ids = frame?.synchrony?.person_ids || [];
    const matrix = frame?.synchrony?.matrix || [];
    const idx = new Map(ids.map((id, i) => [id, i]));

    // --- reconcile node set ---
    const seen = new Set();
    for (const p of people) {
      seen.add(p.person_id);
      let n = nodes.get(p.person_id);
      if (!n) { n = makeNode(p, w, h); nodes.set(p.person_id, n); }
      n.color = p.color; n.colorNum = hexNum(p.color); n.hr = p.hr ?? n.hr; n.hrVar = p.hr_var; n.seat = p.seat ?? n.seat;
      n.conn = p.connection ?? n.conn;
      if (n.name !== p.display_name) { n.name = p.display_name; n.label.text = p.display_name; }
      n.phase += (n.hr / 60) * 2 * Math.PI * dt;
      if (typeof p.phase === "number") {
        let err = p.phase - (n.phase % (2 * Math.PI));
        while (err > Math.PI) err -= 2 * Math.PI;
        while (err < -Math.PI) err += 2 * Math.PI;
        n.phase += 0.15 * err;
      }
      const ta = ALPHA_BY_CONN[p.connection] ?? 0.6;
      n.alpha += (ta - n.alpha) * Math.min(1, dt * 3);
    }
    const removedList = [];
    for (const [id, n] of nodes) {
      if (seen.has(id)) continue;
      n.alpha += (0 - n.alpha) * Math.min(1, dt * 3);
      if (n.alpha < 0.02) {
        removedList.push({ node: n, ctx: { pid: n.pid, nodeX: n.x, nodeY: n.y, cenX: n.x, cenY: n.y, worldX: cx, worldY: cy, nodeColor: n.colorNum, cohortColor: n.masterColor ?? n.colorNum } });
        n.g.destroy({ children: true }); n.label.destroy(); field.detachPid(id); nodes.delete(id);
      }
    }

    const arr = people.map((p) => nodes.get(p.person_id)).filter(Boolean);
    const N = arr.length;
    const pos = new Map(arr.map((n, i) => [n, i]));

    // --- pass 1: cohort tracker + union-find ---
    const uf = unionFind(N);
    const pd = [];
    const liveKeys = new Set();
    for (let i = 0; i < N; i++) {
      for (let j = i + 1; j < N; j++) {
        const a = arr[i], b = arr[j];
        const ki = idx.get(a.pid), kj = idx.get(b.pid);
        const raw = (ki != null && kj != null) ? (matrix[ki]?.[kj] ?? 0) : 0;
        const key = pairKey(a.pid, b.pid); liveKeys.add(key);
        const e = tracker.update(key, raw, a.hrVar, b.hrVar, dt);
        pd.push({ a, b });
        if (e.qual) uf.union(i, j);
      }
    }
    tracker.prune(liveKeys);

    // --- pass 2: components, cohortTime, master, cohort-join detection ---
    const comp = arr.map((_, i) => uf.find(i));
    const members = new Map();
    arr.forEach((n, i) => { const r = comp[i]; (members.get(r) || members.set(r, []).get(r)).push(n); });
    const joined = [], left = [];
    for (let i = 0; i < N; i++) {
      const n = arr[i];
      const inCohort = members.get(comp[i]).length >= 2;
      if (inCohort) {
        if (n.cohortTime === 0) joined.push(n); // first frame in a cohort -> burst later
        n.cohortTime += dt; n.outTime = 0; n.exitT = 0;
      } else {
        n.outTime += dt;
        if (n.outTime >= CFG.grace) {
          if (n.cohortTime > 0) {
            n.exitT = CFG.exitDur; n.exitColor = n.renderTint; n.exitScale = n.renderScale;
            n.cohortTime = 0; n.master = null; n.masterColor = null;
            left.push(n); // just dropped out of its cohort -> 'left' event
          } else if (n.exitT > 0) { n.exitT = Math.max(0, n.exitT - dt); }
        }
      }
    }
    const inCohortOf = (n) => members.get(comp[pos.get(n)]).length >= 2;
    for (const grp of members.values()) {
      if (grp.length < 2) continue;
      let m = grp[0];
      for (const n of grp) if (n.cohortTime > m.cohortTime || (n.cohortTime === m.cohortTime && n.seat < m.seat)) m = n;
      for (const n of grp) { n.master = m; n.masterColor = m.colorNum; }
    }

    // --- de-conflict cohort colors: no two concurrent cohorts read as the same hue ---
    // Seniority order (oldest cohort first) keeps its master's true color; each later cohort
    // whose hue lands too close to an already-taken one rotates away until it's distinct.
    if (CFG.cohortHueSep > 0) {
      const cohorts = [];
      for (const grp of members.values()) if (grp.length >= 2) cohorts.push(grp);
      cohorts.sort((a, b) => b[0].master.cohortTime - a[0].master.cohortTime);
      const usedHues = [];
      for (const grp of cohorts) {
        let [h, s, l] = rgbToHsl(grp[0].master.colorNum);
        let color = grp[0].master.colorNum, tries = 0;
        while (usedHues.some((u) => hueDist(u, h) < CFG.cohortHueSep) && tries < 16) { h = (h + 53) % 360; tries++; }
        if (tries > 0) color = hslToRgb(h, Math.max(s, 0.55), Math.min(Math.max(l, 0.5), 0.62));
        usedHues.push(h);
        for (const n of grp) n.masterColor = color;
      }
    }

    // --- links: each member points at a rotating partner in its cohort ---
    for (const grp of members.values()) {
      if (grp.length < 2) continue;
      for (const n of grp) {
        if (n.cohortTime < CFG.tMaster) { n.linkTo = null; continue; }
        n.linkAge += dt;
        const cur = n.linkTo ? nodes.get(n.linkTo) : null;
        if (!(cur && grp.includes(cur) && cur !== n) || n.linkAge >= n.linkDur) {
          // Bias toward NEARBY partners: a metaball neck only draws within (r1+r2)*6, so a random
          // far partner renders nothing. Prefer members within reach, weighted to the closest few
          // (keeps the rotating jostle but guarantees the edge actually shows). Fall back to the
          // nearest overall if the cohort is so spread that nobody's in reach.
          const rr = n.r || CFG.baseR;
          const scored = grp
            .filter((m) => m !== n)
            .map((m) => ({ m, d: Math.hypot(m.x - n.x, m.y - n.y), reach: (rr + (m.r || CFG.baseR)) * 6 }))
            .sort((a, b) => a.d - b.d);
          let pool = scored.filter((s) => s.d <= s.reach && s.m.pid !== n.linkTo);
          if (!pool.length) pool = scored.filter((s) => s.d <= s.reach);
          if (!pool.length) pool = scored; // nobody reachable — take the nearest anyway
          if (pool.length) {
            const topK = pool.slice(0, Math.min(pool.length, 4)); // choose among the closest handful
            n.linkTo = topK[Math.floor(Math.random() * topK.length)].m.pid;
            n.linkAge = 0; n.linkDur = CFG.linkMin + Math.random() * (CFG.linkMax - CFG.linkMin);
          }
        }
      }
    }

    // --- pass 3: momentum physics (spring-mass-damper: ease in, overshoot, wobble) ---
    const cent = new Map(); // root -> {sx, sy, cnt}
    for (let i = 0; i < N; i++) {
      const r = comp[i]; if (members.get(r).length < 2) continue;
      const n = arr[i]; let c = cent.get(r);
      if (!c) { c = { sx: 0, sy: 0, cnt: 0 }; cent.set(r, c); }
      c.sx += n.x; c.sy += n.y; c.cnt++;
    }
    const fx = new Map(), fy = new Map();
    for (const n of arr) {
      n.driftAngle += (Math.random() - 0.5) * CFG.driftTurn * dt;
      const gAmt = clamp01((n.cohortTime - CFG.tGravity) / CFG.gravRamp);
      const driftK = CFG.drift * (1 - 0.85 * gAmt);
      fx.set(n, (cx - n.x) * CFG.center + Math.cos(n.driftAngle) * driftK);
      fy.set(n, (cy - n.y) * CFG.center + Math.sin(n.driftAngle) * driftK);
    }
    for (let i = 0; i < N; i++) {
      const c = cent.get(comp[i]); if (!c) continue;
      const n = arr[i]; const gAmt = clamp01((n.cohortTime - CFG.tGravity) / CFG.gravRamp);
      fx.set(n, fx.get(n) + (c.sx / c.cnt - n.x) * CFG.gravityK * gAmt);
      fy.set(n, fy.get(n) + (c.sy / c.cnt - n.y) * CFG.gravityK * gAmt);
    }
    const minD = CFG.minDist * scale, crossD = CFG.crossDist * scale;
    for (const { a, b } of pd) {
      let dx = b.x - a.x, dy = b.y - a.y; const d = Math.hypot(dx, dy) || 0.001;
      const ux = dx / d, uy = dy / d;
      const same = inCohortOf(a) && comp[pos.get(a)] === comp[pos.get(b)];
      let f = 0;
      if (same) { if (d < minD) f = (minD - d) * CFG.collideK; }        // collision bounce
      else { if (d < crossD) f = (crossD - d) * CFG.sepK; }             // cohort separation
      if (f === 0) continue;
      fx.set(a, fx.get(a) - f * ux); fy.set(a, fy.get(a) - f * uy);     // push apart
      fx.set(b, fx.get(b) + f * ux); fy.set(b, fy.get(b) + f * uy);
    }
    for (const n of arr) {
      if (!n.linkTo) continue;
      const t = nodes.get(n.linkTo); if (!t) continue;
      fx.set(n, fx.get(n) + (t.x - n.x) * CFG.linkPull);
      fy.set(n, fy.get(n) + (t.y - n.y) * CFG.linkPull);
    }
    // integrate with momentum + drag, cap velocity, bounce off walls
    const maxV = CFG.maxSpeed * scale;
    for (const n of arr) {
      const ax = fx.get(n) - CFG.drag * n.vx, ay = fy.get(n) - CFG.drag * n.vy;
      n.vx += ax * dt; n.vy += ay * dt;
      const sp = Math.hypot(n.vx, n.vy);
      if (sp > maxV) { n.vx = n.vx / sp * maxV; n.vy = n.vy / sp * maxV; }
      n.x += n.vx * dt; n.y += n.vy * dt;
      if (n.x < 30) { n.x = 30; n.vx = Math.abs(n.vx) * 0.4; }
      else if (n.x > w - 30) { n.x = w - 30; n.vx = -Math.abs(n.vx) * 0.4; }
      if (n.y < 30) { n.y = 30; n.vy = Math.abs(n.vy) * 0.4; }
      else if (n.y > h - 30) { n.y = h - 30; n.vy = -Math.abs(n.vy) * 0.4; }
    }

    // --- choreography: fire event reactions (particles / positioned filters / property pulses) ---
    // Location resolves to a world point: the node, its cohort centroid, or screen center.
    const ctxFor = (n) => {
      const root = comp[pos.get(n)];
      const c = cent.get(root);
      const inCo = !!c && members.get(root).length >= 2;
      return {
        pid: n.pid, nodeX: n.x, nodeY: n.y,
        cenX: inCo ? c.sx / c.cnt : n.x, cenY: inCo ? c.sy / c.cnt : n.y,
        worldX: cx, worldY: cy,
        nodeColor: n.colorNum, cohortColor: n.masterColor ?? n.colorNum,
      };
    };
    for (const n of arr) n.hold = null; // continuous holds are refreshed each frame
    choreo.frameBegin();
    const EV = CFG.events;
    for (const n of arr) {
      const ctx = ctxFor(n);
      choreo.state("activated", n, ctx, EV);                              // continuous: alive
      choreo.state("hr", n, ctx, EV);                                     // continuous: HR-driven curves
      if (inCohortOf(n)) choreo.state("joined", n, ctx, EV);              // continuous: in a cohort
      if (n.conn === "disconnected") choreo.state("disconnected", n, ctx, EV);
      if (!n.everActive) { n.everActive = true; choreo.hit("activated", n, ctx, EV); }
      const bk = Math.floor(n.phase / (2 * Math.PI));                     // heartbeat = phase wrap
      if (n.lastBeatK === undefined) n.lastBeatK = bk;
      else if (bk > n.lastBeatK) { n.lastBeatK = bk; choreo.hit("beat", n, ctx, EV); }
      if (n.conn !== n.prevConn) { if (n.conn === "disconnected") choreo.hit("disconnected", n, ctx, EV); n.prevConn = n.conn; }
    }
    for (const n of joined) choreo.hit("joined", n, ctxFor(n), EV);       // momentary: just joined
    for (const n of left) choreo.hit("left", n, ctxFor(n), EV);          // momentary: just left
    for (const rm of removedList) choreo.hit("removed", rm.node, rm.ctx, EV);
    choreo.frameEnd(); // tear down continuous emitters that weren't refreshed this frame

    // --- cohort ambient glow behind each cohort: generated additive circles, or a PNG sprite ---
    // The centre + radius are eased through a pool of glow states matched to cohorts by spatial
    // proximity, so a glow tracks its cohort across member OR master changes and tweens instead of
    // snapping. A cohort with no nearby prior glow is new (snaps in; the strength fade hides it).
    glowLayer.clear();
    const glowPng = !!glowTex;
    glowLayer.visible = !glowPng;
    glowSprites.visible = glowPng;
    for (const g of glows) g.seen = false;
    const gk = 1 - Math.exp(-dt * CFG.cohortGlowTween); // frame-rate-independent ease factor
    let gi = 0;
    for (const [root, c] of cent) {
      const grp = members.get(root); const m = grp[0].master; if (!m) continue;
      const tx = c.sx / c.cnt, ty = c.sy / c.cnt;
      let tR = 40;
      for (const n of grp) tR = Math.max(tR, Math.hypot(n.x - tx, n.y - ty));
      let g = null, bestD = Infinity;
      for (const cand of glows) { if (cand.seen) continue; const d = Math.hypot(tx - cand.x, ty - cand.y); if (d < bestD) { bestD = d; g = cand; } }
      if (!g || bestD > Math.max(tR, g.R) + 60) { g = { x: tx, y: ty, R: tR }; glows.push(g); } // no nearby prior glow -> new
      g.seen = true;
      g.x += (tx - g.x) * gk; g.y += (ty - g.y) * gk; g.R += (tR - g.R) * gk;
      const gx = g.x, gy = g.y, R = g.R;
      const strength = clamp01((m.cohortTime - CFG.cohortGlowOnset) / Math.max(0.05, CFG.cohortGlowFade));
      if (glowPng) {
        const s = glowSprite(gi++);
        s.visible = true; s.tint = m.masterColor; s.x = gx; s.y = gy;
        s.alpha = clamp01(0.12 * strength * CFG.cohortGlowAlpha);
        s.width = s.height = (R + 70) * 2 * CFG.cohortGlowSize;
      } else {
        glowLayer.circle(gx, gy, R + 70).fill({ color: m.masterColor, alpha: 0.05 * strength });
        glowLayer.circle(gx, gy, R + 30).fill({ color: m.masterColor, alpha: 0.06 * strength });
      }
    }
    for (; gi < glowPool.length; gi++) glowPool[gi].visible = false;
    for (let i = glows.length - 1; i >= 0; i--) if (!glows[i].seen) glows.splice(i, 1); // cohort dissolved / merged away

    // --- render nodes (compute radius + tint) + continuous particle aura ---
    const lifeK = 1 - Math.exp(-dt * 8); // ease for the modulate lifecycle (smooth ramp + release)
    for (const n of arr) {
      // Cohort lifecycle (scale-up, fade-to-master) comes from the `joined` MODULATE reactions:
      // the dispatcher sets targets in n.hold each frame (0 when out of a cohort), and these eased
      // values follow them up on join and back down on leave (replacing the old exit unwind).
      const h = n.hold;
      n.smScale = (n.smScale ?? 0) + (((h && h.modScale) || 0) - (n.smScale ?? 0)) * lifeK;
      n.smHalo = (n.smHalo ?? 0) + (((h && h.modHalo) || 0) - (n.smHalo ?? 0)) * lifeK;
      n.smColorMix = (n.smColorMix ?? 0) + (((h && h.modColorMix) || 0) - (n.smColorMix ?? 0)) * lifeK;
      if (h && h.modColorTo != null) n.smColorTo = h.modColorTo; // latch the cohort color while supplied
      let cohortScale = 1 + n.smScale;
      let tint = (n.smColorMix > 0.002 && n.smColorTo != null) ? lerpColor(n.colorNum, n.smColorTo, clamp01(n.smColorMix)) : n.colorNum;
      // per-node reaction pulses/holds/HR-curves (scale pop, color flash, HR breathe) on top
      const baseCohortScale = cohortScale; // lifecycle scale before the HR core pulse — the halo uses this
      const efx = nodeFx(n, dt);
      if (efx.scale) cohortScale *= 1 + efx.scale;
      if (efx.colorMix > 0 && efx.colorTo != null) tint = lerpColor(tint, efx.colorTo, clamp01(efx.colorMix));
      const nodeAlpha = clamp01(n.alpha + efx.alpha);
      n.renderTint = tint; n.renderScale = cohortScale;
      const beat = 0.5 + 0.5 * Math.cos(n.phase % (2 * Math.PI));
      const r = CFG.baseR * cohortScale + CFG.beatPulse * beat;
      n.r = r;
      n.g.x = n.x; n.g.y = n.y; n.g.alpha = nodeAlpha;
      n.core.tint = tint; n.halo.tint = tint;
      n.core.width = n.core.height = 2 * r;                        // sprite sized to the core diameter
      n.halo.visible = CFG.haloOn;
      // halo uses the pre-reaction (core) scale so it's independent of the core; its own pulse comes
      // from the "halo" property reaction (efx.haloScale) plus the intrinsic haloPulse px.
      if (CFG.haloOn) { const hr = CFG.baseR * baseCohortScale * CFG.haloScale * (1 + efx.haloScale + n.smHalo) + CFG.haloPulse * beat; n.halo.width = n.halo.height = 2 * hr; n.halo.alpha = CFG.haloAlpha; }
      n.label.x = n.x; n.label.y = n.y + CFG.baseR * cohortScale + 7; n.label.alpha = nodeAlpha;
      // White while solo (over black); dark once in a cohort but wrapped in a cohort-color
      // outer glow so the dark text stays legible over the bright center AND the black edges.
      const wantDark = n.cohortTime > 0.9;
      const glow = n.masterColor ?? n.colorNum;
      if (wantDark !== n.labelDark || (wantDark && n.labelGlow !== glow)) {
        n.labelDark = wantDark; n.labelGlow = wantDark ? glow : -1;
        if (wantDark) {
          // Dark text with a BRIGHT cohort-colored halo: a crisp stroke (solid bright edge)
          // + a glow, both pushed toward white so it's legible even where it sits over black.
          const halo = lerpColor(glow, 0xffffff, 0.62);
          n.label.style.fill = hexNum(CFG.labelCohort);
          n.label.style.stroke = { color: halo, width: 3 };
          n.label.style.dropShadow = { color: halo, blur: 4, distance: 0, alpha: 1 };
        } else {
          n.label.style.fill = hexNum(CFG.labelSolo);
          n.label.style.stroke = null;
          n.label.style.dropShadow = false;
        }
      }
    }

    // --- edges: connect each node to its rotating partner (metaball neck OR stretched PNG) ---
    edges.clear();
    const usePng = CFG.edgeStyle === "png";
    edgeSprites.visible = usePng;
    let ei = 0;
    for (const n of arr) {
      if (n.cohortTime < CFG.tMaster || !n.linkTo || n.masterColor == null) continue;
      const t = nodes.get(n.linkTo); if (!t) continue;
      const inF = clamp01(n.linkAge / CFG.linkFade);
      const outF = clamp01((n.linkDur - n.linkAge) / CFG.linkFade);
      const alpha = 0.85 * clamp01((n.cohortTime - CFG.tMaster) / 0.5) * Math.min(inF, outF) * Math.min(n.alpha, t.alpha);
      if (alpha < 0.02) continue;
      if (usePng) {
        // Stretched sprite from node CENTER to node center; node cores render on top and hide
        // its ends. Thickness = edgePngWidth, easing up over the link's fade-in like the neck.
        const dx = t.x - n.x, dy = t.y - n.y, d = Math.hypot(dx, dy);
        const grow = clamp01(n.linkAge / (CFG.linkFade * 1.5));
        const s = edgeSprite(ei++);
        s.visible = true; s.tint = n.masterColor; s.alpha = alpha;
        s.x = n.x; s.y = n.y; s.rotation = Math.atan2(dy, dx);
        s.width = d; s.height = CFG.edgePngWidth * (0.5 + 0.5 * grow);
      } else {
        // Neck spread: EVERY link starts thin and grows over its own fade-in (so a rewired
        // link on a mature cohort no longer blinks on already-fat). The max it grows to
        // scales with cohort maturity — older cohorts get fatter necks.
        const mature = clamp01((n.cohortTime - CFG.tMaster) / CFG.edgeGrow);
        const target = 0.18 + 0.5 * mature;
        const v = 0.12 + (target - 0.12) * clamp01(n.linkAge / (CFG.linkFade * 1.5));
        metaball(edges, n.x, n.y, n.r, t.x, t.y, t.r, n.masterColor, alpha, v);
      }
    }
    for (; ei < edgePool.length; ei++) edgePool[ei].visible = false; // hide unused beams

    field.update(dt);

    // --- compose the post-process stack: static filters (CFG.filters) + live event filters ---
    fstack.refresh(CFG.filters, w, h);
    eventFX.update(dt, w, h);
    const fsig = fstack.sig + "|" + eventFX.sig;
    if (fsig !== filterSig) {
      filterSig = fsig;
      const all = [...fstack.list, ...eventFX.list];
      bloomGroup.filters = all.length ? all : null;
    }
  }

  function destroy() {
    eventFX.clear();
    for (const [, n] of nodes) n.g.destroy({ children: true });
    nodes.clear();
    field.destroy();
    fstack.destroy();
    container.destroy({ children: true });
  }

  // Full serializable snapshot of this preset's settings (for save / switch-away-and-back).
  // Deep-clone so nested structures (filters, particle systems, events) don't alias CFG.
  function getState() {
    return { version: 1, params: JSON.parse(JSON.stringify(CFG)) };
  }
  function setState(state) {
    if (!state) return;
    if (state.params) Object.assign(CFG, JSON.parse(JSON.stringify(state.params)));
    field.setSystems(CFG.particleSystems); // rebuild emitters for the loaded systems
    applyTextures();                        // reload node core/halo + edge textures
    fstack.sig = ""; eventFX.clear(); filterSig = ""; // force the composed filter stack to rebuild
  }
  // Let the UI re-apply particle-system / node-graphic edits live.
  function applyParticles() { field.setSystems(CFG.particleSystems); }

  return { container, update, destroy, params: CFG, controls: CONTROLS, getState, setState, applyParticles, applyTextures };
}
