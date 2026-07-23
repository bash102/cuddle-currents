// Cohort qualification — the rule that a pair counts as "in sync" only after their
// concordance is SUSTAINED, not merely momentarily overlapping. Without this, transient
// value coincidences draw false connections and everything looks synced.
//
// Every preset needs this exact rule, so it lives here as a shared, tunable module.
// Three stages, applied per pair:
//
//   1. Flat-signal gate — scale concordance by how much BOTH hearts actually vary
//      (hr_var). A flat signal correlates only noise; it must not qualify.
//   2. EMA smoothing over `concTau` — removes frame-to-frame jitter.
//   3. Dwell timer — the smoothed magnitude must stay above `qualOn` for `qualTime`
//      seconds continuously to QUALIFY. It stays qualified until it falls below
//      `qualOff` (hysteresis), so it doesn't flicker at the boundary. `vis` is a
//      0..1 fade so a qualified edge eases in/out rather than popping.

export const COHORT = {
  concTau: 1.0,   // s — LIGHT EMA (just de-spikes); the dwell timer is the real guard,
                  //     so this stays small and doesn't hide the qualTime control.
  qualOn: 0.6,    // smoothed |concordance| must exceed this... (high enough that coincidental
                  //   beat-matches between independent people don't percolate into one clump)
  qualTime: 3.0,  // ...continuously for this many seconds to qualify (the main knob)
  qualOff: 0.4,   // ...and stays qualified until it drops below this (hysteresis). Not too
                  //   low, so a cohort unsticks promptly once concordance genuinely fades.
  fadeTau: 0.5,   // s — opacity ease-in/out once (dis)qualified
  varLo: 0.8,     // hr_var (bpm) at/below which a signal is treated as flat...
  varHi: 3.0,     // ...and at/above which it's fully trusted
};

// 0 when a person's HR is flat (SD <= varLo), ramping to 1 by varHi. null (unknown) is
// trusted, so we never damp on missing data.
export function varTrust(hrVar) {
  if (hrVar == null) return 1;
  return Math.max(0, Math.min(1, (hrVar - COHORT.varLo) / (COHORT.varHi - COHORT.varLo)));
}

export class CohortTracker {
  constructor(cfg = COHORT) { this.cfg = cfg; this.m = new Map(); }

  // Advance one pair. `raw` is the raw concordance [-1,1]; `hrVarA/B` are the two
  // people's hr_var. Returns { s, qual, vis }: s = smoothed signed concordance,
  // qual = has it sustained long enough, vis = 0..1 eased visibility.
  update(key, raw, hrVarA, hrVarB, dt) {
    const c = this.cfg;
    const gated = raw * Math.min(varTrust(hrVarA), varTrust(hrVarB));
    let e = this.m.get(key);
    // Start at 0, not the first raw value: a pair must genuinely BUILD concordance to
    // qualify — it can't inherit a stale coincidental correlation and qualify instantly.
    if (!e) { e = { s: 0, held: 0, qual: false, vis: 0 }; this.m.set(key, e); }
    e.s += (gated - e.s) * (1 - Math.exp(-dt / c.concTau));
    const mag = Math.abs(e.s);
    if (mag >= c.qualOn) { e.held += dt; if (e.held >= c.qualTime) e.qual = true; }
    else if (mag < c.qualOff) { e.held = 0; e.qual = false; }
    // between qualOff and qualOn: hold current state (hysteresis)
    e.vis += ((e.qual ? 1 : 0) - e.vis) * (1 - Math.exp(-dt / c.fadeTau));
    return e;
  }

  // Drop trackers for pairs whose people are no longer present.
  prune(liveKeys) { for (const k of this.m.keys()) if (!liveKeys.has(k)) this.m.delete(k); }
}

export const pairKey = (a, b) => (a < b ? a + "|" + b : b + "|" + a);
