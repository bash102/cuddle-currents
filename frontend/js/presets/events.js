// Events = the choreography layer. The renderer emits a fixed CATALOG of events; a preset
// binds REACTIONS to each (stored in CFG.events). A reaction targets a particle system, a
// filter, or a node property — and specifies WHERE (location) and HOW (trigger). The actual
// runtime dispatch (firing a reaction at the event's location, animating a filter outward
// from it) is the next step — this module is the schema + defaults the editor drives.

// Events the node-graph renderer knows how to emit.
export const EVENT_CATALOG = [
  { id: "activated", label: "Node Activated" },
  { id: "joined", label: "Node Joins Cohort" },
  { id: "left", label: "Node Leaves Cohort" },
  { id: "disconnected", label: "Node Disconnected" },
  { id: "beat", label: "Beat (per heartbeat)" },
  { id: "removed", label: "Node Removed" },
];

export const REACTION_TYPES = ["particle", "filter", "property"];
export const LOCATIONS = ["node", "cohort centroid", "world"];
export const TRIGGERS = ["hit", "continuous", "modulate"];

// A reaction: { type, ref, location, trigger }. `ref` names a particle system (from the
// particle-systems list) or a filter (from the filter stack) depending on `type`.
export function makeReaction(type = "particle") {
  return { active: true, type, ref: "", location: "node", trigger: "hit" };
}

// Default choreography — mirrors what the renderer currently does hardcoded.
export function defaultEvents() {
  return EVENT_CATALOG.map((e) => ({
    id: e.id,
    label: e.label,
    reactions:
      e.id === "activated" ? [{ active: true, type: "particle", ref: "aura", location: "node", trigger: "continuous" }]
      : e.id === "joined" ? [
          { active: true, type: "particle", ref: "joinBurst", location: "node", trigger: "hit" },   // celebratory spray
          { active: true, type: "particle", ref: "ringBurst", location: "node", trigger: "hit" },    // per-node ripple (scales)
          { active: true, type: "filter", ref: "shockwave", location: "cohort centroid", trigger: "hit" }, // one big cohort ripple
        ]
      : [],
  }));
}
