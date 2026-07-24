// Data-driven filter stack (pixi-filters).
//
// FILTERS registry: each entry knows how to CONSTRUCT its pixi filter, its PARAM SCHEMA
// (for the controls UI), and how to APPLY a params object to the live filter. A preset
// stores `CFG.filters = [{ type, active, params }, …]` (array order = stack order), and
// FilterStack turns that into the container's actual `filters` list, updating params live.
//
// Add a filter: import its class, add an entry here. It shows up in the stack editor
// automatically. Everything is wrapped so a bad filter never crashes the render.

import {
  AdvancedBloomFilter, GlowFilter, OutlineFilter, BulgePinchFilter,
  ZoomBlurFilter, RGBSplitFilter, AdjustmentFilter, ShockwaveFilter, TwistFilter, BevelFilter,
} from "../../vendor/pixi-filters.min.mjs";
import { BlurFilter } from "../../vendor/pixi.min.mjs";

const hexNum = (hex) => (typeof hex === "string" ? parseInt(hex.replace("#", ""), 16) : hex);
// Set a filter's center/offset point, whether it's a PointData {x,y} or a [x,y] array.
const setPt = (f, prop, x, y) => {
  try { const p = f[prop]; if (p && "x" in p) { p.x = x; p.y = y; } else f[prop] = [x, y]; } catch {}
};
const setCenter = (f, cx, cy, w, h, px) => setPt(f, "center", px ? cx * w : cx, px ? cy * h : cy);

export const FILTERS = {
  bloom: {
    label: "Bloom",
    make: () => new AdvancedBloomFilter(),
    params: [
      { key: "bloomScale", label: "Intensity", min: 0, max: 3, step: 0.05, def: 1.1 },
      { key: "threshold", label: "Threshold", min: 0, max: 1, step: 0.02, def: 0.35 },
      { key: "brightness", label: "Brightness", min: 0, max: 2, step: 0.05, def: 1.0 },
      { key: "blur", label: "Spread", min: 0, max: 16, step: 0.5, def: 7 },
    ],
    apply: (f, p) => { f.bloomScale = p.bloomScale; f.threshold = p.threshold; f.brightness = p.brightness; if (f.blur !== p.blur) f.blur = p.blur; },
  },
  glow: {
    label: "Glow",
    make: () => new GlowFilter({ distance: 12, quality: 0.3 }),
    params: [
      { key: "outerStrength", label: "Outer", min: 0, max: 12, step: 0.5, def: 4 },
      { key: "innerStrength", label: "Inner", min: 0, max: 12, step: 0.5, def: 0 },
      { key: "color", label: "Color", type: "color", def: "#ffffff" },
    ],
    apply: (f, p) => { f.outerStrength = p.outerStrength; f.innerStrength = p.innerStrength; f.color = hexNum(p.color); },
  },
  outline: {
    label: "Outline",
    make: () => new OutlineFilter(),
    params: [
      { key: "thickness", label: "Thickness", min: 0, max: 10, step: 0.5, def: 2 },
      { key: "color", label: "Color", type: "color", def: "#ffffff" },
      { key: "alpha", label: "Alpha", min: 0, max: 1, step: 0.05, def: 1 },
    ],
    apply: (f, p) => { f.thickness = p.thickness; f.color = hexNum(p.color); f.alpha = p.alpha; },
  },
  bulgePinch: {
    label: "Bulge / Pinch",
    make: () => new BulgePinchFilter(),
    params: [
      { key: "radius", label: "Radius", min: 20, max: 600, step: 10, def: 200 },
      { key: "strength", label: "Strength", min: -1, max: 1, step: 0.05, def: 0.4 },
      { key: "cx", label: "Center X", min: 0, max: 1, step: 0.02, def: 0.5 },
      { key: "cy", label: "Center Y", min: 0, max: 1, step: 0.02, def: 0.5 },
    ],
    apply: (f, p, w, h) => { f.radius = p.radius; f.strength = p.strength; setCenter(f, p.cx, p.cy, w, h, false); },
    // As an event filter: an expanding bulge that swells then relaxes, centered at the hit.
    fx: { dur: 0.7, animate: (f, age, dur, x, y) => { const k = age / dur; f.radius = 40 + k * 260; f.strength = 0.7 * Math.sin(Math.PI * k); setPt(f, "center", x, y); } },
  },
  zoomBlur: {
    label: "Zoom Blur",
    make: () => new ZoomBlurFilter(),
    params: [
      { key: "strength", label: "Strength", min: 0, max: 0.5, step: 0.01, def: 0.1 },
      { key: "innerRadius", label: "Inner R", min: 0, max: 400, step: 10, def: 80 },
      { key: "cx", label: "Center X", min: 0, max: 1, step: 0.02, def: 0.5 },
      { key: "cy", label: "Center Y", min: 0, max: 1, step: 0.02, def: 0.5 },
    ],
    apply: (f, p, w, h) => { f.strength = p.strength; f.innerRadius = p.innerRadius; setCenter(f, p.cx, p.cy, w, h, true); },
    // As an event filter: a quick zoom-burst from the hit point.
    fx: { dur: 0.6, animate: (f, age, dur, x, y) => { const k = age / dur; f.strength = 0.3 * Math.sin(Math.PI * k); f.innerRadius = k * 160; setPt(f, "center", x, y); } },
  },
  rgbSplit: {
    label: "RGB Split",
    make: () => new RGBSplitFilter(),
    params: [{ key: "amount", label: "Amount", min: 0, max: 20, step: 1, def: 4 }],
    apply: (f, p) => { try { f.red = [-p.amount, 0]; f.green = [0, 0]; f.blue = [p.amount, 0]; } catch {} },
  },
  adjust: {
    label: "Color Grade",
    make: () => new AdjustmentFilter(),
    params: [
      { key: "brightness", label: "Brightness", min: 0, max: 2, step: 0.05, def: 1 },
      { key: "contrast", label: "Contrast", min: 0, max: 2, step: 0.05, def: 1 },
      { key: "saturation", label: "Saturation", min: 0, max: 2, step: 0.05, def: 1 },
      { key: "gamma", label: "Gamma", min: 0, max: 2, step: 0.05, def: 1 },
    ],
    apply: (f, p) => { f.brightness = p.brightness; f.contrast = p.contrast; f.saturation = p.saturation; f.gamma = p.gamma; },
  },
  shockwave: {
    label: "Shockwave",
    make: () => new ShockwaveFilter(),
    params: [
      { key: "amplitude", label: "Amplitude", min: 0, max: 80, step: 1, def: 30 },
      { key: "wavelength", label: "Wavelength", min: 20, max: 400, step: 5, def: 160 },
      { key: "brightness", label: "Brightness", min: 0, max: 2, step: 0.05, def: 1.1 },
      { key: "cx", label: "Center X", min: 0, max: 1, step: 0.02, def: 0.5 },
      { key: "cy", label: "Center Y", min: 0, max: 1, step: 0.02, def: 0.5 },
    ],
    apply: (f, p, w, h) => { try { f.amplitude = p.amplitude; f.wavelength = p.wavelength; f.brightness = p.brightness; setCenter(f, p.cx, p.cy, w, h, true); } catch {} },
    // The marquee event filter: a ripple expanding outward from the hit point (time drives
    // the ring radius). This is the "shockwave on cohort-join, centered on the node" effect.
    fx: { dur: 1.1, animate: (f, age, dur, x, y) => { try { setPt(f, "center", x, y); f.time = age; } catch {} } },
  },
  twist: {
    label: "Twist",
    make: () => new TwistFilter(),
    params: [
      { key: "radius", label: "Radius", min: 0, max: 600, step: 10, def: 200 },
      { key: "angle", label: "Angle", min: -10, max: 10, step: 0.5, def: 4 },
      { key: "cx", label: "Center X", min: 0, max: 1, step: 0.02, def: 0.5 },
      { key: "cy", label: "Center Y", min: 0, max: 1, step: 0.02, def: 0.5 },
    ],
    apply: (f, p, w, h) => { try { f.radius = p.radius; f.angle = p.angle; setPt(f, "offset", p.cx * w, p.cy * h); } catch {} },
    // As an event filter: a swirling ripple that winds up then unwinds.
    fx: { dur: 0.8, animate: (f, age, dur, x, y) => { const k = age / dur; try { f.radius = 40 + k * 220; f.angle = 5 * Math.sin(Math.PI * k); setPt(f, "offset", x, y); } catch {} } },
  },
  blur: {
    label: "Blur",
    make: () => new BlurFilter(),
    params: [
      { key: "strength", label: "Strength", min: 0, max: 20, step: 0.5, def: 4 },
      { key: "quality", label: "Quality", min: 1, max: 10, step: 1, def: 4 },
    ],
    apply: (f, p) => {
      try { f.strength = p.strength; } catch {}
      try { if (typeof f.strength !== "number") f.blur = p.strength; } catch {}
      try { f.quality = p.quality; } catch {}
    },
    // As an event filter: a quick blur pulse that clears.
    fx: { dur: 0.5, animate: (f, age, dur) => { const s = 12 * Math.sin(Math.PI * (age / dur)); try { f.strength = s; } catch { try { f.blur = s; } catch {} } } },
  },
  bevel: {
    label: "Bevel",
    make: () => new BevelFilter(),
    params: [
      { key: "rotation", label: "Angle", min: 0, max: 360, step: 5, def: 45 },
      { key: "thickness", label: "Thickness", min: 0, max: 10, step: 0.5, def: 2 },
      { key: "lightAlpha", label: "Light α", min: 0, max: 1, step: 0.05, def: 0.7 },
      { key: "shadowAlpha", label: "Shadow α", min: 0, max: 1, step: 0.05, def: 0.7 },
      { key: "lightColor", label: "Light", type: "color", def: "#ffffff" },
      { key: "shadowColor", label: "Shadow", type: "color", def: "#000000" },
    ],
    apply: (f, p) => { try { f.rotation = p.rotation; f.thickness = p.thickness; f.lightAlpha = p.lightAlpha; f.shadowAlpha = p.shadowAlpha; f.lightColor = hexNum(p.lightColor); f.shadowColor = hexNum(p.shadowColor); } catch {} },
  },
};

// Order the stack editor lists / seeds filters in. Shockwave + Twist are here so they can be
// picked as event-filter reactions; they're inert in the static stack (they need animating).
export const FILTER_ORDER = ["bloom", "glow", "outline", "bevel", "adjust", "rgbSplit", "blur", "bulgePinch", "zoomBlur", "twist", "shockwave"];

// A fresh stack: every filter present (so the UI can toggle any), only bloom active.
export function defaultFilters() {
  return FILTER_ORDER.map((type) => {
    const params = {};
    for (const p of FILTERS[type].params) params[p.key] = p.def;
    return { type, active: type === "bloom", params };
  });
}

export class FilterStack {
  constructor() { this.instances = {}; this.sig = ""; this.list = []; } // list = active instances, in order
  _inst(type) {
    if (!this.instances[type]) { try { this.instances[type] = FILTERS[type].make(); } catch { this.instances[type] = null; } }
    return this.instances[type];
  }
  // Update params + recompute the active instance list (does NOT assign container.filters —
  // the renderer composes this.list with any event filters and assigns once).
  refresh(list, w, h) {
    const active = (list || []).filter((f) => f.active && FILTERS[f.type]);
    const sig = active.map((f) => f.type).join(",");
    if (sig !== this.sig) { this.sig = sig; this.list = active.map((f) => this._inst(f.type)).filter(Boolean); }
    for (const f of active) {
      const inst = this._inst(f.type);
      if (inst) { try { FILTERS[f.type].apply(inst, f.params, w, h); } catch {} }
    }
    return this.list;
  }
  destroy() { for (const k in this.instances) { try { this.instances[k]?.destroy(); } catch {} } this.instances = {}; this.list = []; }
}

// Positioned, one-shot animated filters fired by events (e.g. a Shockwave rippling out from a
// node the moment it joins a cohort). Each spawn gets a fresh filter instance that animates via
// its FILTERS[type].fx descriptor for `fx.dur` seconds, then destroys itself. The renderer
// concatenates `this.list` onto the static stack each frame; `sig` flips whenever the set of
// live instances changes so the renderer knows to reassign the container's filter list.
export class EventFilters {
  // maxLive caps concurrent instances — each is a full-screen post-process pass, so a burst of
  // simultaneous joins (30 nodes syncing at once) can't stack into dozens of passes.
  constructor(maxLive = 4) { this.items = []; this.list = []; this.sig = ""; this._id = 0; this.max = maxLive; }
  // Fire filter `type` centered at world (x, y). `params` (optional) tunes the instance — the
  // filter's own params (amplitude/wavelength/…) are applied once, and params.dur overrides the
  // animation length. fx.animate then drives time/center each frame.
  spawn(type, x, y, params) {
    const def = FILTERS[type]; if (!def) return;
    if (this.items.length >= this.max) return; // at capacity — drop this one
    let inst;
    try { inst = def.make(); if (params && def.apply) def.apply(inst, params, 0, 0); } catch { return; }
    const dur = params?.dur ?? def.fx?.dur ?? 0.6;
    this.items.push({ type, inst, age: 0, dur, x, y, id: ++this._id });
  }
  update(dt, w, h) {
    for (let i = this.items.length - 1; i >= 0; i--) {
      const it = this.items[i]; it.age += dt;
      if (it.age >= it.dur) { try { it.inst.destroy(); } catch {} this.items.splice(i, 1); continue; }
      const fx = FILTERS[it.type]?.fx;
      if (fx?.animate) { try { fx.animate(it.inst, it.age, it.dur, it.x, it.y, w, h); } catch {} }
    }
    const sig = this.items.map((it) => it.id).join(",");
    if (sig !== this.sig) { this.sig = sig; this.list = this.items.map((it) => it.inst); }
    return this.list;
  }
  clear() { for (const it of this.items) { try { it.inst.destroy(); } catch {} } this.items = []; this.list = []; this.sig = ""; }
}
