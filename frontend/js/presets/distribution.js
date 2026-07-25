// Distribution renderer — one selectable variable (default HR) laid along the horizontal axis;
// each person is a dot dropped into its bin and stacked upward (a beeswarm / dot-histogram), so
// as people converge on a value they pile into a tall peak. Same StateFrame; FilterStack bloom.

import { Container, Graphics, Text } from "../../vendor/pixi.min.mjs";
import { FilterStack, defaultFilters } from "./filters.js";
import { VARS } from "./scatter.js";

const hexNum = (hex) => (typeof hex === "string" ? parseInt(hex.replace("#", ""), 16) : hex);
const clamp01 = (x) => (x < 0 ? 0 : x > 1 ? 1 : x);
const VAR_KEYS = Object.keys(VARS);
const norm = (v, key) => clamp01((v - VARS[key].min) / (VARS[key].max - VARS[key].min));

const CFG = {
  variable: "hr",
  bins: 24, dotR: 6, gap: 2, beatPulse: 3, ease: 6,
  filters: defaultFilters(),
  bg: "#0b0a12", axisColor: "#5a5470", labelColor: "#e8e0f0",
};

export const CONTROLS = [
  { group: "Axis", key: "variable", label: "Variable", type: "select", options: VAR_KEYS, tip: "Variable spread along the horizontal axis." },
  { group: "Axis", key: "bins", label: "Bins", min: 6, max: 60, step: 1, tip: "Number of columns the axis is divided into." },
  { group: "Dots", key: "dotR", label: "Dot size", min: 2, max: 16, step: 1, tip: "Dot radius (px)." },
  { group: "Dots", key: "gap", label: "Gap", min: 0, max: 8, step: 0.5, tip: "Vertical gap between stacked dots (px)." },
  { group: "Dots", key: "beatPulse", label: "Beat pulse", min: 0, max: 8, step: 0.5, tip: "Dot growth on each heartbeat (px)." },
  { group: "Dots", key: "ease", label: "Ease", min: 1, max: 20, step: 0.5, tip: "How fast dots glide to their new bin/stack (higher = snappier)." },
  { group: "Colors", key: "bg", label: "Background", type: "color", tip: "Stage background color." },
  { group: "Colors", key: "axisColor", label: "Axis", type: "color", tip: "Axis + tick color." },
  { group: "Colors", key: "labelColor", label: "Label", type: "color", tip: "Label text color." },
];

export function createDistribution(app) {
  const container = new Container();
  const bloomGroup = new Container();
  const fstack = new FilterStack();
  let filterSig = "";
  const axisG = new Graphics();
  const dotsG = new Graphics();
  bloomGroup.addChild(dotsG);
  container.addChild(axisG, bloomGroup);

  const mkText = (size, anchor) => { const t = new Text({ text: "", style: { fill: 0xffffff, fontSize: size, fontFamily: "system-ui" } }); t.anchor.set(...anchor); container.addChild(t); return t; };
  const axT = { title: mkText(12, [0.5, 1]), min: mkText(10, [0, 1]), max: mkText(10, [1, 1]) };

  const nodes = new Map(); // pid -> record

  function update(frame, dt) {
    const w = app.screen.width, h = app.screen.height;
    bloomGroup.filterArea = app.screen;
    app.renderer.background.color = hexNum(CFG.bg);
    const ML = 24, MR = 24, MB = 40, MT = 24;
    const baseY = h - MB, plotW = w - ML - MR;
    const key = VARS[CFG.variable] ? CFG.variable : "hr";
    const bins = Math.max(1, Math.round(CFG.bins));
    const binW = plotW / bins, spacing = CFG.dotR * 2 + CFG.gap;

    const people = (frame?.people || []).filter((p) => p.enrollment === "active");
    const seen = new Set();
    const easeK = 1 - Math.exp(-dt * CFG.ease);

    // reconcile + compute each person's bin
    const binOf = new Map();
    for (const p of people) {
      seen.add(p.person_id);
      let n = nodes.get(p.person_id);
      if (!n) { n = { pid: p.person_id, x: null, y: null, phase: p.phase ?? 0, hr: p.hr ?? 60, colorNum: hexNum(p.color), alpha: 0 }; nodes.set(p.person_id, n); }
      n.hr = p.hr ?? n.hr; n.colorNum = hexNum(p.color);
      n.phase += (n.hr / 60) * 2 * Math.PI * dt;
      n.alpha += (1 - n.alpha) * Math.min(1, dt * 3);
      const b = Math.min(bins - 1, Math.max(0, Math.floor(norm(VARS[key].get(p), key) * bins)));
      binOf.set(p.person_id, b);
    }
    for (const [id, n] of nodes) {
      if (seen.has(id)) continue;
      n.alpha += (0 - n.alpha) * Math.min(1, dt * 3);
      if (n.alpha < 0.02) nodes.delete(id);
    }

    // assign stack index within each bin (stable order by pid), set targets, ease
    const stacks = new Map(); // bin -> pids[]
    for (const [pid, b] of binOf) { (stacks.get(b) || stacks.set(b, []).get(b)).push(pid); }
    for (const [b, pids] of stacks) {
      pids.sort();
      pids.forEach((pid, s) => {
        const n = nodes.get(pid); if (!n) return;
        const tx = ML + (b + 0.5) * binW;
        const ty = baseY - (s + 0.5) * spacing;
        if (n.x == null) { n.x = tx; n.y = ty; } else { n.x += (tx - n.x) * easeK; n.y += (ty - n.y) * easeK; }
      });
    }

    // --- axis ---
    axisG.clear();
    axisG.moveTo(ML, baseY).lineTo(w - MR, baseY).stroke({ width: 1.5, color: hexNum(CFG.axisColor), alpha: 0.9 });
    const lc = hexNum(CFG.labelColor), ac = hexNum(CFG.axisColor);
    axT.title.text = VARS[key].label; axT.title.x = w / 2; axT.title.y = h - 6; axT.title.style.fill = lc;
    axT.min.text = "" + Math.round(VARS[key].min); axT.min.x = ML; axT.min.y = baseY + 15; axT.min.style.fill = ac;
    axT.max.text = "" + Math.round(VARS[key].max); axT.max.x = w - MR; axT.max.y = baseY + 15; axT.max.style.fill = ac;

    // --- dots ---
    dotsG.clear();
    for (const [, n] of nodes) {
      if (n.x == null) continue;
      const beat = 0.5 + 0.5 * Math.cos(n.phase % (2 * Math.PI));
      const r = CFG.dotR + CFG.beatPulse * beat;
      dotsG.circle(n.x, n.y, r).fill({ color: n.colorNum, alpha: n.alpha });
    }

    fstack.refresh(CFG.filters, w, h);
    if (fstack.sig !== filterSig) { filterSig = fstack.sig; bloomGroup.filters = fstack.list.length ? fstack.list : null; }
  }

  function destroy() { nodes.clear(); for (const k in axT) axT[k].destroy(); fstack.destroy(); container.destroy({ children: true }); }
  function getState() { return { version: 1, params: JSON.parse(JSON.stringify(CFG)) }; }
  function setState(state) { if (!state) return; if (state.params) Object.assign(CFG, JSON.parse(JSON.stringify(state.params))); fstack.sig = ""; filterSig = ""; }
  return { container, update, destroy, params: CFG, controls: CONTROLS, getState, setState };
}
