// Particle system built on @pixi/particle-emitter (v8-compatible fork).
//
// Design emitters in the interactive editor:
//   https://userland.pixijs.io/particle-emitter-editor/
// then paste its exported JSON over AURA_CONFIG / BURST_CONFIG below. The editor emits the
// OLDER config format — `build()` runs it through `upgradeConfig()` automatically, so you
// can paste it as-is (both old and new "behaviors" formats work). Our soft-dot texture is
// injected into the texture behavior, and a `colorStatic` behavior (if present) is
// overridden per node so a node's aura matches its color.
//
// One continuous Emitter follows each node (world-space particles); short-lived Emitters
// fire the cohort-join bursts. Particles use normal blending — the preset's bloom filter
// provides the glow.

import { Container, Sprite, Texture } from "../../vendor/pixi.min.mjs";
import { Emitter, upgradeConfig } from "../../vendor/particle-emitter.es.js";

const TEX_R = 24;
function makeSoftDot() {
  const size = TEX_R * 2;
  const c = document.createElement("canvas");
  c.width = c.height = size;
  const g = c.getContext("2d");
  const grd = g.createRadialGradient(TEX_R, TEX_R, 0, TEX_R, TEX_R, TEX_R);
  grd.addColorStop(0, "rgba(255,255,255,1)");
  grd.addColorStop(0.35, "rgba(255,255,255,0.5)");
  grd.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = grd;
  g.fillRect(0, 0, size, size);
  return Texture.from(c);
}

// --- particle systems: named, friendly-param definitions (edited in the UI, saved with
// the preset). buildFromSys() turns a def into a @pixi/particle-emitter config. ---

// The editable params surfaced per system (min/max/step). `only` limits a param to a type.
export const SYSTEM_PARAMS = [
  { key: "rate", label: "Rate", min: 0, max: 30, step: 1, only: "continuous" },
  { key: "count", label: "Burst", min: 1, max: 80, step: 1, only: "hit" },
  { key: "lifeMin", label: "Life min", min: 0.2, max: 3, step: 0.1 },
  { key: "lifeMax", label: "Life max", min: 0.2, max: 3, step: 0.1 },
  { key: "speedStart", label: "Speed 0", min: 0, max: 300, step: 5 },
  { key: "speedEnd", label: "Speed 1", min: 0, max: 120, step: 5 },
  { key: "scaleStart", label: "Scale 0", min: 0.05, max: 1.5, step: 0.05 },
  { key: "scaleEnd", label: "Scale 1", min: 0.02, max: 1, step: 0.02 },
  { key: "spawnR", label: "Spawn R", min: 0, max: 40, step: 1 },
];

// The renderer's built-in systems. `color` is the binding hint (node vs cohort color).
export function defaultParticleSystems() {
  return {
    aura: { label: "Aura", type: "continuous", color: "node", shape: "scatter", texture: "", rate: 8, lifeMin: 0.9, lifeMax: 1.7, speedStart: 26, speedEnd: 6, scaleStart: 0.4, scaleEnd: 0.12, spawnR: 8 },
    joinBurst: { label: "Join Burst", type: "hit", color: "cohort", shape: "scatter", texture: "", count: 26, lifeMin: 0.5, lifeMax: 0.95, speedStart: 150, speedEnd: 20, scaleStart: 0.55, scaleEnd: 0.1, spawnR: 4 },
    // Per-node ripple: a thin ring of particles flung radially outward from the node. Scales to
    // every node (unlike a full-screen shockwave filter). Fired per-node on cohort-join.
    ringBurst: { label: "Ring Burst", type: "hit", color: "cohort", shape: "ring", texture: "", count: 28, lifeMin: 0.45, lifeMax: 0.7, speedStart: 220, speedEnd: 40, scaleStart: 0.35, scaleEnd: 0.05, spawnR: 6 },
  };
}

// A fresh user-created system.
export function newParticleSystem(label) {
  return { label, type: "hit", color: "node", shape: "scatter", texture: "", rate: 8, count: 20, lifeMin: 0.5, lifeMax: 1.2, speedStart: 80, speedEnd: 15, scaleStart: 0.4, scaleEnd: 0.1, spawnR: 6 };
}

const hexStr = (num) => num.toString(16).padStart(6, "0");

// Force our resolved texture + the node/cohort color into whatever texture/color behaviors
// an advanced config already has (so the PNG field and color-binding still apply).
function injectTexColor(cfg, tex, colorHex) {
  for (const b of cfg.behaviors || []) {
    if (b.type === "textureSingle") b.config.texture = tex;
    else if (b.type === "textureRandom") b.config.textures = [tex];
    else if (b.type === "colorStatic" && colorHex) b.config.color = colorHex;
    else if (b.type === "color" && colorHex && b.config?.color?.list) {
      // keep the gradient's shape but tint every stop toward the node color's hue is overkill;
      // simplest useful behavior: leave user gradients alone. (Only static color is bound.)
    }
  }
  return cfg;
}

// Turn a system def into an emitter config. If `cfgRaw` (JSON loaded from sys.config's file
// path) is present, it wins — friendly sliders are ignored for that system. The Pixi editor
// exports the OLD format, so run it through upgradeConfig(); a pre-upgraded config (has
// `behaviors`) is used as-is. Either way the resolved texture + color are injected. cfgRaw is
// deep-cloned first so per-node color/texture injection never mutates the shared cache.
function buildConfig(sys, tex, colorHex, cfgRaw) {
  if (cfgRaw) {
    try {
      const src = JSON.parse(JSON.stringify(cfgRaw));
      const cfg = src.behaviors ? src : upgradeConfig(src, [tex]);
      return injectTexColor(cfg, tex, colorHex);
    } catch (e) {
      console.warn(`particle system "${sys.label}" config JSON invalid — using sliders`, e);
    }
  }
  return buildFromSys(sys, tex, colorHex);
}

// Friendly system def + texture + color -> @pixi/particle-emitter config.
// shape "ring" = particles spawn on a thin torus and fly RADIALLY outward (an expanding ring
// ripple); "scatter" (default) = random directions with a full spawn disc.
function buildFromSys(sys, tex, colorHex) {
  const hit = sys.type === "hit";
  const ring = sys.shape === "ring";
  const behaviors = [
    { type: "alpha", config: { alpha: { list: hit ? [{ time: 0, value: 0.9 }, { time: 1, value: 0 }] : [{ time: 0, value: 0 }, { time: 0.25, value: 0.6 }, { time: 1, value: 0 }] } } },
    { type: "scale", config: { scale: { list: [{ time: 0, value: sys.scaleStart }, { time: 1, value: sys.scaleEnd }] }, minMult: ring ? 1 : 0.7 } },
    // Floor the speed: the emitter's SpeedBehavior normalizes velocity every frame (1/length),
    // so a speed of exactly 0 divides by zero -> NaN positions -> crash. 0.05 px/s reads as still.
    { type: "moveSpeed", config: { speed: { list: [{ time: 0, value: Math.max(0.05, sys.speedStart) }, { time: 1, value: Math.max(0.05, sys.speedEnd) }] }, minMult: ring ? 1 : 0.6 } },
  ];
  // Ring: no random rotation — the torus (affectRotation) points each particle outward so
  // moveSpeed drives it radially. Scatter: random heading.
  if (!ring) behaviors.push({ type: "rotationStatic", config: { min: 0, max: 360 } });
  behaviors.push({ type: "spawnShape", config: { type: "torus", data: { x: 0, y: 0, radius: sys.spawnR, innerRadius: ring ? sys.spawnR * 0.7 : 0, affectRotation: ring } } });
  behaviors.push({ type: "colorStatic", config: { color: colorHex || "ffffff" } });
  behaviors.push({ type: "textureSingle", config: { texture: tex } });
  return {
    lifetime: { min: sys.lifeMin, max: sys.lifeMax },
    frequency: hit ? 0.001 : Math.max(0.001, 1 / Math.max(0.001, sys.rate || 8)),
    spawnChance: 1, particlesPerWave: hit ? (sys.count || 20) : 1,
    emitterLifetime: hit ? 0.02 : -1, maxParticles: 240, pos: { x: 0, y: 0 }, addAtBack: false,
    behaviors,
  };
}

export class ParticleSystem {
  constructor() {
    this.container = new Container();
    this.tex = makeSoftDot();
    this.cont = new Map();  // "systemRef|pid" -> { em, color, pid, seen } continuous emitters
    this.bursts = [];       // active one-shot emitters
    this.systems = defaultParticleSystems(); // named friendly-param defs (from CFG.particleSystems)
    this.texCache = {};     // path/URL -> Texture (or null while loading)
    this.cfgCache = {};     // path/URL -> parsed emitter JSON (null while loading, false if failed)
  }

  // Resolve a system's emitter-JSON path to a parsed config object. Empty path -> null (use
  // the friendly sliders). Fetches asynchronously; returns null until it arrives, then
  // rebuilds auras so they pick it up. A bad path falls back to the sliders permanently.
  getConfig(path) {
    if (!path) return null;
    const c = this.cfgCache[path];
    if (c !== undefined) return c || null; // loaded object, or null/false -> null (sliders)
    this.cfgCache[path] = null; // pending — don't kick off a second fetch
    fetch(path)
      .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
      .then((j) => { this.cfgCache[path] = j; this._rebuildCont(); })
      .catch((e) => { console.warn(`particle config failed to load: ${path}`, e); this.cfgCache[path] = false; });
    return null;
  }

  // Resolve a system's texture path to a Texture. Empty path -> the generated soft dot.
  // Loads asynchronously; returns the soft dot until the PNG arrives, then rebuilds auras so
  // they pick it up. A bad path falls back to the soft dot permanently.
  getTex(path) {
    if (!path) return this.tex;
    const cached = this.texCache[path];
    if (cached) return cached;
    if (cached === undefined) {
      this.texCache[path] = null; // pending — don't kick off a second load
      const img = new Image();
      img.crossOrigin = "anonymous";
      img.onload = () => { try { this.texCache[path] = Texture.from(img); this._rebuildCont(); } catch { this.texCache[path] = this.tex; } };
      img.onerror = () => { console.warn(`particle texture failed to load: ${path}`); this.texCache[path] = this.tex; };
      img.src = path;
    }
    return this.tex;
  }

  _rebuildCont() { for (const a of this.cont.values()) a.em.destroy(); this.cont.clear(); }
  _emitter(sys, colorNum) {
    const em = new Emitter(this.container, buildConfig(sys, this.getTex(sys.texture), hexStr(colorNum), this.getConfig(sys.config)));
    em.autoUpdate = false;
    return em;
  }

  // Point the field at the preset's particle-system defs; rebuild live emitters so param
  // edits (rate, spawn radius, speed, texture, advanced JSON…) take effect. Pre-warm textures.
  setSystems(s) {
    if (!s) return;
    this.systems = s;
    for (const sys of Object.values(s)) { if (sys?.texture) this.getTex(sys.texture); if (sys?.config) this.getConfig(sys.config); }
    this._rebuildCont();
  }

  // ---- continuous emitters (bound to a node for the duration of a state, e.g. 'activated') ----
  // Each frame: beginContinuous() → attach() for every (system, node) that should be emitting →
  // endContinuous() to tear down any that weren't refreshed this frame.
  beginContinuous() { for (const a of this.cont.values()) a.seen = false; }

  // Bind/refresh continuous system `ref` to node `pid` at (x, y). rate 0 pauses (friendly
  // systems only — a config-driven system keeps its own frequency).
  attach(ref, pid, x, y, colorNum) {
    const sys = this.systems[ref]; if (!sys) return;
    const key = ref + "|" + pid;
    let a = this.cont.get(key);
    if (!a || a.color !== colorNum) {
      if (a) a.em.destroy();
      a = { em: this._emitter(sys, colorNum), color: colorNum, pid };
      this.cont.set(key, a);
    }
    a.seen = true;
    a.em.updateOwnerPos(x, y);
    if (sys.config) { a.em.emit = true; }
    else { const rate = sys.rate ?? 8; a.em.emit = rate > 0; a.em.frequency = rate > 0 ? 1 / rate : 1000; }
  }

  endContinuous() {
    for (const [key, a] of this.cont) { if (!a.seen) { a.em.destroy(); this.cont.delete(key); } }
  }

  // Tear down every continuous emitter bound to a node (called when the node is removed).
  detachPid(pid) {
    for (const [key, a] of this.cont) { if (a.pid === pid) { a.em.destroy(); this.cont.delete(key); } }
  }

  // ---- one-shot bursts (hit reactions fired at a world position) ----
  burstSystem(ref, x, y, colorNum) {
    const sys = this.systems[ref]; if (!sys) return;
    const em = this._emitter(sys, colorNum);
    em.updateOwnerPos(x, y);
    em.emit = true;
    if (em.emitterLifetime < 0) em.emitterLifetime = 0.05; // don't let a continuous system leak as a burst
    this.bursts.push(em);
  }

  update(dt) {
    for (const a of this.cont.values()) a.em.update(dt);
    for (let i = this.bursts.length - 1; i >= 0; i--) {
      const em = this.bursts[i];
      em.update(dt);
      if (!em.emit && em.particleCount === 0) { em.destroy(); this.bursts.splice(i, 1); }
    }
  }

  destroy() {
    for (const a of this.cont.values()) a.em.destroy();
    for (const em of this.bursts) em.destroy();
    this.cont.clear(); this.bursts.length = 0;
    this.container.destroy({ children: true });
  }
}
