// The choreography runtime — turns the preset's CFG.events into live effects.
//
// Each renderer event is either a STATE the node is currently in (activated = alive,
// joined = in a cohort, disconnected = link lost) or a momentary HIT (the instant it joins,
// leaves, beats, or is removed). Reactions carry a `trigger`:
//   • "hit"                 → fired once, at the moment (particle burst, shockwave, property pop)
//   • "continuous"/"modulate" → refreshed every frame the state holds (bound aura, sustained hold)
//
// Per frame the renderer calls frameBegin() → for every node, state()/hit() for whichever
// events apply → frameEnd(). Locations resolve to a world point: the node, its cohort's
// centroid, or screen center. See events.js for the schema the editor drives.

// Apply a property reaction to a node. HIT = a decaying pop/blink/flash; CONTINUOUS = a steady
// hold refreshed each frame (cleared by the renderer before dispatch). `graphic` is a no-op
// (there's no alternate node sprite to swap yet).
// Programmatic waveform of the node's HR phase (0..1), for continuous property reactions.
function propCurve(name, phase) {
  const t = (phase / (2 * Math.PI)) % 1; // position within the current beat, 0..1
  switch (name) {
    case "cosine": return 0.5 + 0.5 * Math.cos(phase);        // smooth breathe, peak on the beat
    case "bounce": { const b = 0.5 + 0.5 * Math.cos(phase); return b * b * b; } // sharp thump
    case "triangle": return 1 - Math.abs(t * 2 - 1);          // linear up/down
    case "pulse": return t < 0.3 ? 1 : 0;                     // blip on the beat
    default: return 1;                                        // static
  }
}

function applyProp(node, prop, ctx, isHit, params, curve) {
  const amt = params?.amount, dur = params?.dur;
  if (isHit) {
    const p = (node.pulse = node.pulse || {});
    if (prop === "scale") p.scale = { amt: amt ?? 0.5, t: 0, dur: dur ?? 0.4 };        // pop, decays out
    else if (prop === "opacity") p.opacity = { amt: -(amt ?? 0.6), t: 0, dur: dur ?? 0.4 }; // dip
    else if (prop === "color") p.color = { to: 0xffffff, t: 0, dur: dur ?? 0.4 };          // white flash
  } else {
    const h = (node.hold = node.hold || {});
    if (curve && curve !== "static" && (prop === "scale" || prop === "opacity")) {
      const c = propCurve(curve, node.phase * (params?.rate ?? 1)); // HR-driven waveform (rate = ×BPM)
      if (prop === "scale") h.scale = (amt ?? 0.15) * c;       // grows on the beat
      else h.opacity = -(amt ?? 0.15) * (1 - c);               // full alpha on beat, dims between
    } else {
      if (prop === "scale") h.scale = amt ?? 0.15;             // steady offset
      else if (prop === "opacity") h.opacity = -(amt ?? 0.15); // steady dim
      else if (prop === "color") h.color = ctx.cohortColor ?? node.colorNum;
    }
  }
}

export class Choreographer {
  constructor(field, eventFX) { this.field = field; this.eventFX = eventFX; }

  frameBegin() { this.field.beginContinuous(); }
  frameEnd() { this.field.endContinuous(); }

  _loc(location, ctx) {
    if (location === "cohort centroid") return [ctx.cenX ?? ctx.nodeX, ctx.cenY ?? ctx.nodeY];
    if (location === "world") return [ctx.worldX, ctx.worldY];
    return [ctx.nodeX, ctx.nodeY];
  }
  // Particle color follows the target system's binding hint (cohort vs node color).
  _pcolor(ref, ctx) {
    const sys = this.field.systems[ref];
    return sys?.color === "cohort" ? (ctx.cohortColor ?? ctx.nodeColor) : ctx.nodeColor;
  }

  // Continuous reactions for a state the node is currently in (skip one-shot "hit" ones).
  state(eventId, node, ctx, events) {
    const ev = events && events.find((e) => e.id === eventId); if (!ev) return;
    for (const r of ev.reactions) {
      if (r.active === false || r.trigger === "hit") continue;
      const [x, y] = this._loc(r.location, ctx);
      if (r.type === "particle" && r.ref) this.field.attach(r.ref, node.pid, x, y, this._pcolor(r.ref, ctx));
      else if (r.type === "property" && r.ref) applyProp(node, r.ref, ctx, false, r.params, r.curve);
      // continuous filters are intentionally unsupported — event filters are momentary ripples.
    }
  }

  // Hit reactions fired once, the instant an event occurs (skip the continuous ones).
  hit(eventId, node, ctx, events) {
    const ev = events && events.find((e) => e.id === eventId); if (!ev) return;
    for (const r of ev.reactions) {
      if (r.active === false || r.trigger !== "hit") continue;
      const [x, y] = this._loc(r.location, ctx);
      if (r.type === "particle" && r.ref) this.field.burstSystem(r.ref, x, y, this._pcolor(r.ref, ctx));
      else if (r.type === "filter" && r.ref) this.eventFX.spawn(r.ref, x, y, r.params);
      else if (r.type === "property" && r.ref) applyProp(node, r.ref, ctx, true, r.params, r.curve);
    }
  }
}

// Advance a node's decaying hit-pulses and return the render offsets to apply this frame:
// { scale (multiplier delta), alpha (additive), colorTo/colorMix (flash toward a color) }.
// Combines the steady `hold` (continuous reactions) with the fading `pulse` (hit reactions).
export function nodeFx(node, dt) {
  let scale = 0, alpha = 0, colorTo = null, colorMix = 0;
  const h = node.hold;
  if (h) { if (h.scale) scale += h.scale; if (h.opacity) alpha += h.opacity; if (h.color != null) { colorTo = h.color; colorMix = Math.max(colorMix, 0.5); } }
  const p = node.pulse;
  if (p) {
    for (const key of Object.keys(p)) {
      const e = p[key]; e.t += dt;
      const k = e.t / e.dur;
      if (k >= 1) { delete p[key]; continue; }
      const env = Math.sin(Math.PI * k); // 0 → 1 → 0
      if (key === "scale") scale += e.amt * env;
      else if (key === "opacity") alpha += e.amt * env;
      else if (key === "color") { colorTo = e.to; colorMix = Math.max(colorMix, env); }
    }
  }
  return { scale, alpha, colorTo, colorMix };
}
