// Chord-diagram renderer — the 30 people sit evenly around a ring; each pair with enough
// synchrony (frame.synchrony.matrix) is joined by an arc that bows toward the centre, its
// thickness + opacity scaling with the sync strength. Same StateFrame contract as every other
// preset. Reuses the data-driven FilterStack (bloom) for the glow.

import { Container, Graphics, Text } from "../../vendor/pixi.min.mjs";
import { FilterStack, defaultFilters } from "./filters.js";

const hexNum = (hex) => (typeof hex === "string" ? parseInt(hex.replace("#", ""), 16) : hex);
const clamp01 = (x) => (x < 0 ? 0 : x > 1 ? 1 : x);
const rgb = (n) => [(n >> 16) & 255, (n >> 8) & 255, n & 255];
const lerpColor = (a, b, t) => {
  const A = rgb(a), B = rgb(b);
  return ((Math.round(A[0] + (B[0] - A[0]) * t) << 16) + (Math.round(A[1] + (B[1] - A[1]) * t) << 8) + Math.round(A[2] + (B[2] - A[2]) * t));
};

const CFG = {
  ringR: 0.38,        // ring radius (fraction of the smaller screen dimension)
  nodeR: 7,           // node dot radius
  spin: 0.03,         // slow rotation of the ring (rad/s)
  beatPulse: 3,       // node growth per heartbeat (px)
  chordThresh: 0.35,  // minimum synchrony to draw a chord
  chordWidth: 5,      // max chord thickness (at full sync)
  chordAlpha: 0.55,   // max chord opacity (at full sync)
  chordCurve: 0.85,   // how much chords bow toward the centre (0 = straight)
  filters: defaultFilters(),
  bg: "#0b0a12",
  labelColor: "#e8e0f0",
};

export const CONTROLS = [
  { group: "Ring", key: "ringR", label: "Radius", min: 0.2, max: 0.48, step: 0.01, tip: "Ring radius (fraction of the smaller screen dimension)." },
  { group: "Ring", key: "nodeR", label: "Node size", min: 2, max: 20, step: 1, tip: "Node dot radius (px)." },
  { group: "Ring", key: "spin", label: "Spin", min: -0.3, max: 0.3, step: 0.01, tip: "Slow rotation of the ring (rad/s). 0 = still." },
  { group: "Ring", key: "beatPulse", label: "Beat pulse", min: 0, max: 10, step: 0.5, tip: "Node growth on each heartbeat (px)." },
  { group: "Chords", key: "chordThresh", label: "Threshold", min: 0, max: 1, step: 0.02, tip: "Minimum synchrony to draw a chord between two people." },
  { group: "Chords", key: "chordWidth", label: "Width", min: 0.5, max: 16, step: 0.5, tip: "Max chord thickness (at full sync)." },
  { group: "Chords", key: "chordAlpha", label: "Opacity", min: 0, max: 1, step: 0.05, tip: "Max chord opacity (at full sync)." },
  { group: "Chords", key: "chordCurve", label: "Curve", min: 0, max: 1, step: 0.05, tip: "How much chords bow toward the centre (0 = straight lines)." },
  { group: "Colors", key: "bg", label: "Background", type: "color", tip: "Stage background color." },
  { group: "Colors", key: "labelColor", label: "Label", type: "color", tip: "Label text color." },
];

export function createChord(app) {
  const container = new Container();
  const bloomGroup = new Container();
  const fstack = new FilterStack();
  let filterSig = "";
  const chordsG = new Graphics();
  const nodesG = new Graphics();
  bloomGroup.addChild(chordsG, nodesG);
  const labels = new Container();
  container.addChild(bloomGroup, labels);

  const nodes = new Map(); // pid -> record
  let rot = 0;

  function update(frame, dt) {
    const w = app.screen.width, h = app.screen.height, cx = w / 2, cy = h / 2;
    bloomGroup.filterArea = app.screen;
    app.renderer.background.color = hexNum(CFG.bg);
    rot += CFG.spin * dt;

    const people = (frame?.people || []).filter((p) => p.enrollment === "active");
    const ids = frame?.synchrony?.person_ids || [];
    const matrix = frame?.synchrony?.matrix || [];
    const idx = new Map(ids.map((id, i) => [id, i]));

    const seen = new Set();
    for (const p of people) {
      seen.add(p.person_id);
      let n = nodes.get(p.person_id);
      if (!n) {
        const label = new Text({ text: p.display_name, style: { fill: 0xffffff, fontSize: 11, fontFamily: "system-ui", fontWeight: "600" } });
        label.anchor.set(0.5); labels.addChild(label);
        n = { pid: p.person_id, phase: p.phase ?? 0, hr: p.hr ?? 60, colorNum: hexNum(p.color), label, alpha: 0, seat: p.seat ?? 0 };
        nodes.set(p.person_id, n);
      }
      n.hr = p.hr ?? n.hr; n.colorNum = hexNum(p.color); n.seat = p.seat ?? n.seat;
      if (n.label.text !== p.display_name) n.label.text = p.display_name;
      n.phase += (n.hr / 60) * 2 * Math.PI * dt;
      n.alpha += (1 - n.alpha) * Math.min(1, dt * 3);
    }
    for (const [id, n] of nodes) {
      if (seen.has(id)) continue;
      n.alpha += (0 - n.alpha) * Math.min(1, dt * 3);
      if (n.alpha < 0.02) { n.label.destroy(); nodes.delete(id); }
    }

    const arr = [...nodes.values()].sort((a, b) => a.seat - b.seat || (a.pid < b.pid ? -1 : 1));
    const N = arr.length;
    const R = Math.min(w, h) * CFG.ringR;
    arr.forEach((n, i) => { n.angle = rot + (i / Math.max(1, N)) * Math.PI * 2; n.x = cx + Math.cos(n.angle) * R; n.y = cy + Math.sin(n.angle) * R; });

    // --- chords: bow toward centre, thickness/opacity by pairwise synchrony ---
    chordsG.clear();
    const denom = Math.max(0.001, 1 - CFG.chordThresh);
    for (let i = 0; i < N; i++) {
      for (let j = i + 1; j < N; j++) {
        const a = arr[i], b = arr[j];
        const ki = idx.get(a.pid), kj = idx.get(b.pid);
        const s = (ki != null && kj != null) ? (matrix[ki]?.[kj] ?? 0) : 0;
        if (s < CFG.chordThresh) continue;
        const t = clamp01((s - CFG.chordThresh) / denom);
        const alpha = CFG.chordAlpha * t * Math.min(a.alpha, b.alpha);
        if (alpha < 0.02) continue;
        const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
        const ctrlx = mx + (cx - mx) * CFG.chordCurve, ctrly = my + (cy - my) * CFG.chordCurve;
        chordsG.moveTo(a.x, a.y).quadraticCurveTo(ctrlx, ctrly, b.x, b.y)
          .stroke({ width: CFG.chordWidth * (0.3 + 0.7 * t), color: lerpColor(a.colorNum, b.colorNum, 0.5), alpha, cap: "round" });
      }
    }

    // --- nodes + labels ---
    nodesG.clear();
    for (const n of arr) {
      const beat = 0.5 + 0.5 * Math.cos(n.phase % (2 * Math.PI));
      const r = CFG.nodeR + CFG.beatPulse * beat;
      nodesG.circle(n.x, n.y, r).fill({ color: n.colorNum, alpha: n.alpha });
      n.label.x = n.x + Math.cos(n.angle) * (r + 13);
      n.label.y = n.y + Math.sin(n.angle) * (r + 13);
      n.label.alpha = n.alpha;
      n.label.style.fill = hexNum(CFG.labelColor);
    }

    fstack.refresh(CFG.filters, w, h);
    if (fstack.sig !== filterSig) { filterSig = fstack.sig; bloomGroup.filters = fstack.list.length ? fstack.list : null; }
  }

  function destroy() { for (const [, n] of nodes) n.label.destroy(); nodes.clear(); fstack.destroy(); container.destroy({ children: true }); }
  function getState() { return { version: 1, params: JSON.parse(JSON.stringify(CFG)) }; }
  function setState(state) { if (!state) return; if (state.params) Object.assign(CFG, JSON.parse(JSON.stringify(state.params))); fstack.sig = ""; filterSig = ""; }
  return { container, update, destroy, params: CFG, controls: CONTROLS, getState, setState };
}
