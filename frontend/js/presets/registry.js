// Preset registry — the style-vs-renderer split.
//
// RENDERERS are the code engines (each has its own logic — physics, metaballs, etc.).
// PRESETS are STYLES: a named bundle of settings applied to a renderer. Many styles can
// share one renderer. The switcher (show/pixiApp.js) mounts a style by creating its
// renderer, then applying its state (localStorage tuning first, else the built-in `state`,
// else the renderer's own defaults).
//
// Add a style by appending to PRESETS; add a whole new engine by registering it in
// RENDERERS. Each renderer factory returns { container, update, destroy, params, controls,
// getState, setState }.

import { createNodeGraph } from "./nodeGraph.js";
import { defaultFilters } from "./filters.js";

export const RENDERERS = {
  "node-graph": createNodeGraph,
};

// Node Chart 2's filter stack: default filters, but bloom softer/wider for a calmer look.
function chart2Filters() {
  const f = defaultFilters();
  const bloom = f.find((x) => x.type === "bloom");
  if (bloom) bloom.params = { bloomScale: 0.6, threshold: 0.5, brightness: 0.8, blur: 10 };
  return f;
}

export const PRESETS = [
  // Node Chart 1 — the tight, glowing look (renderer's own defaults).
  { id: "node-graph", label: "Node Chart 1", renderer: "node-graph", state: null },

  // Node Chart 2 — same engine, a calmer/looser variation: gathers loosely, more damped
  // (less wobble), cohorts sit farther apart, softer + subtler bloom, fewer particles.
  {
    id: "node-graph-2", label: "Node Chart 2", renderer: "node-graph",
    state: {
      version: 1,
      params: {
        gravityK: 1.4, linkPull: 0.15, drag: 5.5, collideK: 45,
        minDist: 0.09, crossDist: 0.28, emitRate: 3,
        filters: chart2Filters(),
      },
    },
  },
];
