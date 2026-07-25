// Scatter-plot renderer — each person is a dot placed at (x, y) from two selectable variables
// (default x = HR, y = HRV). Dots ease toward their target, so as people converge on similar
// values they visibly cluster. Same StateFrame contract; reuses the FilterStack for bloom.

import { Container, Graphics, Text } from "../../vendor/pixi.min.mjs";
import { FilterStack, defaultFilters } from "./filters.js";

const hexNum = (hex) => (typeof hex === "string" ? parseInt(hex.replace("#", ""), 16) : hex);
const clamp01 = (x) => (x < 0 ? 0 : x > 1 ? 1 : x);

// Selectable per-person variables (value getter + axis range).
export const VARS = {
  hr: { label: "HR (bpm)", get: (p) => p.hr ?? 60, min: 45, max: 105 },
  hrv: { label: "HRV (ms)", get: (p) => p.rmssd ?? p.hr_var ?? 40, min: 5, max: 90 },
  phase: { label: "Beat phase", get: (p) => (((p.phase ?? 0) % (2 * Math.PI)) + 2 * Math.PI) % (2 * Math.PI), min: 0, max: 2 * Math.PI },
};
const VAR_KEYS = Object.keys(VARS);
const norm = (v, key) => clamp01((v - VARS[key].min) / (VARS[key].max - VARS[key].min));

const CFG = {
  xVar: "hr", yVar: "hrv",
  dotR: 6, beatPulse: 3, ease: 6,
  showLabels: true,
  filters: defaultFilters(),
  bg: "#0b0a12", axisColor: "#5a5470", labelColor: "#e8e0f0",
};

export const CONTROLS = [
  { group: "Axes", key: "xVar", label: "X axis", type: "select", options: VAR_KEYS, tip: "Variable driving horizontal position." },
  { group: "Axes", key: "yVar", label: "Y axis", type: "select", options: VAR_KEYS, tip: "Variable driving vertical position." },
  { group: "Dots", key: "dotR", label: "Dot size", min: 2, max: 20, step: 1, tip: "Dot radius (px)." },
  { group: "Dots", key: "beatPulse", label: "Beat pulse", min: 0, max: 10, step: 0.5, tip: "Dot growth on each heartbeat (px)." },
  { group: "Dots", key: "ease", label: "Ease", min: 1, max: 20, step: 0.5, tip: "How fast dots glide to their new position (higher = snappier)." },
  { group: "Dots", key: "showLabels", label: "Labels", type: "toggle", tip: "Show each person's name by their dot." },
  { group: "Colors", key: "bg", label: "Background", type: "color", tip: "Stage background color." },
  { group: "Colors", key: "axisColor", label: "Axes", type: "color", tip: "Axis + tick color." },
  { group: "Colors", key: "labelColor", label: "Label", type: "color", tip: "Label text color." },
];

export function createScatter(app) {
  const container = new Container();
  const bloomGroup = new Container();
  const fstack = new FilterStack();
  let filterSig = "";
  const axisG = new Graphics();      // axes (outside bloom, crisp)
  const dotsG = new Graphics();
  bloomGroup.addChild(dotsG);
  const labels = new Container();
  container.addChild(axisG, bloomGroup, labels);

  // axis title + tick texts, created once
  const mkText = (size, anchor) => { const t = new Text({ text: "", style: { fill: 0xffffff, fontSize: size, fontFamily: "system-ui" } }); t.anchor.set(...anchor); container.addChild(t); return t; };
  const axT = { xTitle: mkText(12, [0.5, 1]), yTitle: mkText(12, [0.5, 0]), x0: mkText(10, [0, 1]), x1: mkText(10, [1, 1]), y0: mkText(10, [1, 1]), y1: mkText(10, [1, 0]) };

  const nodes = new Map(); // pid -> record

  function update(frame, dt) {
    const w = app.screen.width, h = app.screen.height;
    bloomGroup.filterArea = app.screen;
    app.renderer.background.color = hexNum(CFG.bg);
    const ML = 62, MR = 28, MT = 26, MB = 42;         // plot margins
    const px0 = ML, px1 = w - MR, py0 = h - MB, py1 = MT; // x range, y range (py0 bottom)
    const xk = VARS[CFG.xVar] ? CFG.xVar : "hr", yk = VARS[CFG.yVar] ? CFG.yVar : "hrv";

    const people = (frame?.people || []).filter((p) => p.enrollment === "active");
    const seen = new Set();
    const easeK = 1 - Math.exp(-dt * CFG.ease);
    for (const p of people) {
      seen.add(p.person_id);
      let n = nodes.get(p.person_id);
      if (!n) {
        const label = new Text({ text: p.display_name, style: { fill: 0xffffff, fontSize: 10, fontFamily: "system-ui", fontWeight: "600" } });
        label.anchor.set(0, 0.5); labels.addChild(label);
        n = { pid: p.person_id, x: null, y: null, phase: p.phase ?? 0, hr: p.hr ?? 60, colorNum: hexNum(p.color), label, alpha: 0 };
        nodes.set(p.person_id, n);
      }
      n.hr = p.hr ?? n.hr; n.colorNum = hexNum(p.color);
      if (n.label.text !== p.display_name) n.label.text = p.display_name;
      n.phase += (n.hr / 60) * 2 * Math.PI * dt;
      n.alpha += (1 - n.alpha) * Math.min(1, dt * 3);
      const tx = px0 + norm(VARS[xk].get(p), xk) * (px1 - px0);
      const ty = py0 + norm(VARS[yk].get(p), yk) * (py1 - py0);
      if (n.x == null) { n.x = tx; n.y = ty; } else { n.x += (tx - n.x) * easeK; n.y += (ty - n.y) * easeK; }
    }
    for (const [id, n] of nodes) {
      if (seen.has(id)) continue;
      n.alpha += (0 - n.alpha) * Math.min(1, dt * 3);
      if (n.alpha < 0.02) { n.label.destroy(); nodes.delete(id); }
    }

    // --- axes ---
    axisG.clear();
    axisG.moveTo(px0, py1).lineTo(px0, py0).lineTo(px1, py0).stroke({ width: 1.5, color: hexNum(CFG.axisColor), alpha: 0.9 });
    const lc = hexNum(CFG.labelColor), ac = hexNum(CFG.axisColor);
    axT.xTitle.text = VARS[xk].label; axT.xTitle.x = (px0 + px1) / 2; axT.xTitle.y = h - 6; axT.xTitle.style.fill = lc;
    axT.yTitle.text = VARS[yk].label; axT.yTitle.x = 12; axT.yTitle.y = MT - 18 < 4 ? 4 : (py1 + py0) / 2; axT.yTitle.rotation = -Math.PI / 2; axT.yTitle.style.fill = lc;
    axT.x0.text = "" + Math.round(VARS[xk].min); axT.x0.x = px0; axT.x0.y = py0 + 16; axT.x0.style.fill = ac;
    axT.x1.text = "" + Math.round(VARS[xk].max); axT.x1.x = px1; axT.x1.y = py0 + 16; axT.x1.style.fill = ac;
    axT.y0.text = "" + Math.round(VARS[yk].min); axT.y0.x = px0 - 6; axT.y0.y = py0; axT.y0.style.fill = ac;
    axT.y1.text = "" + Math.round(VARS[yk].max); axT.y1.x = px0 - 6; axT.y1.y = py1; axT.y1.style.fill = ac;

    // --- dots + labels ---
    dotsG.clear();
    for (const [, n] of nodes) {
      const beat = 0.5 + 0.5 * Math.cos(n.phase % (2 * Math.PI));
      const r = CFG.dotR + CFG.beatPulse * beat;
      dotsG.circle(n.x, n.y, r).fill({ color: n.colorNum, alpha: n.alpha });
      n.label.visible = CFG.showLabels;
      if (CFG.showLabels) { n.label.x = n.x + r + 4; n.label.y = n.y; n.label.alpha = n.alpha * 0.9; n.label.style.fill = hexNum(CFG.labelColor); }
    }

    fstack.refresh(CFG.filters, w, h);
    if (fstack.sig !== filterSig) { filterSig = fstack.sig; bloomGroup.filters = fstack.list.length ? fstack.list : null; }
  }

  function destroy() { for (const [, n] of nodes) n.label.destroy(); nodes.clear(); for (const k in axT) axT[k].destroy(); fstack.destroy(); container.destroy({ children: true }); }
  function getState() { return { version: 1, params: JSON.parse(JSON.stringify(CFG)) }; }
  function setState(state) { if (!state) return; if (state.params) Object.assign(CFG, JSON.parse(JSON.stringify(state.params))); fstack.sig = ""; filterSig = ""; }
  return { container, update, destroy, params: CFG, controls: CONTROLS, getState, setState };
}
