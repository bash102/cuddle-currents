// Events = the choreography layer. The renderer emits a fixed CATALOG of events; a preset
// binds REACTIONS to each (stored in CFG.events). A reaction targets a particle system, a
// filter, or a node property — and specifies WHERE (location) and HOW (trigger). The actual
// runtime dispatch (firing a reaction at the event's location, animating a filter outward
// from it) is the next step — this module is the schema + defaults the editor drives.

// Events the node-graph renderer knows how to emit.
export const EVENT_CATALOG = [
  { id: "activated", label: "Node Activated", tip: "A person becomes active on stage (enrolled). Hit reactions fire once when they appear; continuous reactions hold while they're present." },
  { id: "hr", label: "Node Reacts to HR", tip: "Continuous, every frame. Drive scale/opacity/halo from a data Source — the heartbeat (oscillate) or the person's current HR / HRV / phase value." },
  { id: "joined", label: "Node Joins Cohort", tip: "The moment a node first enters a cohort (hit), and continuously while it's in one (modulate ramps like fade-to-master / scale-up)." },
  { id: "left", label: "Node Leaves Cohort", tip: "The moment a node drops out of its cohort (after the grace period)." },
  { id: "disconnected", label: "Node Disconnected", tip: "The person's band lost signal (connection = disconnected). They STAY on stage, just dimmed — this is a dropout, NOT removal." },
  { id: "beat", label: "Beat (per heartbeat)", tip: "Fires once per heartbeat — a discrete tick. Use a hit reaction here to e.g. launch one particle per beat." },
  { id: "removed", label: "Node Removed", tip: "The person is no longer enrolled/active and their node is being destroyed (fades out). Different from Disconnected — this is leaving for good." },
];

export const REACTION_TYPES = ["particle", "filter", "property"];
export const LOCATIONS = ["node", "cohort centroid", "world"];
export const TRIGGERS = ["hit", "continuous", "modulate"];
// Programmatic waveforms for a continuous PROPERTY reaction (used when source = "beat").
export const CURVES = ["cosine", "bounce", "triangle", "pulse", "static"];
// What drives a continuous property reaction: "beat" = oscillate at the heartbeat (shaped by the
// curve); the others map the person's current VALUE (normalized) onto the property.
export const SOURCES = ["beat", "hr", "hrv", "phase"];

// A reaction: { active, type, ref, location, trigger, curve, params }. `ref` names a particle
// system, a filter, or a property depending on `type`. `curve` shapes a continuous property
// reaction (how scale/opacity follows the heartbeat).
export function makeReaction(type = "particle") {
  return { active: true, type, ref: "", location: "node", trigger: "hit", source: "beat", curve: "cosine" };
}

// Default choreography — mirrors what the renderer currently does hardcoded.
export function defaultEvents() {
  return EVENT_CATALOG.map((e) => ({
    id: e.id,
    label: e.label,
    reactions:
      e.id === "activated" ? [{ active: true, type: "particle", ref: "aura", location: "node", trigger: "continuous" }]
      : e.id === "hr" ? [
          { active: true, type: "property", ref: "scale", location: "node", trigger: "continuous", source: "beat", curve: "cosine", params: { amount: 0.28, rate: 1 } },
          { active: true, type: "property", ref: "halo", location: "node", trigger: "continuous", source: "beat", curve: "cosine", params: { amount: 0.5, rate: 1 } },
        ]
      : e.id === "joined" ? [
          { active: true, type: "property", ref: "color", location: "node", trigger: "modulate", params: { amount: 1.0, onset: 2.0, dur: 1.0 } }, // fade to master color
          { active: true, type: "property", ref: "scale", location: "node", trigger: "modulate", params: { amount: 0.2, onset: 3.0, dur: 1.0 } }, // grow to ~120%
          { active: true, type: "particle", ref: "joinBurst", location: "node", trigger: "hit" },   // celebratory spray
          { active: true, type: "particle", ref: "ringBurst", location: "node", trigger: "hit" },    // per-node ripple (scales)
          { active: true, type: "filter", ref: "shockwave", location: "cohort centroid", trigger: "hit" }, // one big cohort ripple
        ]
      : [],
  }));
}
