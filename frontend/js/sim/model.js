// Client-side data simulator — generative and variable-driven.
//
// Produces the EXACT StateFrame shape the real /ws stream emits (see
// src/cuddle/core/models.py) so every preset can be developed against 30 fully
// controllable people — no backend, no hardware, no baseline flow.
//
// Each person's live signal is built from a small set of GENERATIVE VARIABLES you set
// per person. Synchrony is NOT imposed by grouping — it EMERGES when people's variables
// match:
//
//   HR(t)    = restHr + envDepth * sin(2π·envRate·t + envPhase) + smoothed_noise
//   phase(t) = 2π·(restHr/60)·t + beatPhase                                (cardiac beat phase)
//
// Two people's HR fluctuations CO-MOVE — high concordance, they cohort — when their
// envRate AND envPhase match. They COUNTER-MOVE (concordance → −1) at opposite phase.
// They DECORRELATE (→ 0) when their rates differ (the two waves beat in and out of step)
// or when envDepth ≈ 0 (a flat signal has nothing to correlate). Their beats PHASE-LOCK
// (high PLV / Order R) when restHr matches and beatPhase aligns.
//
// So: match two people's Rate+Phase to sync them, offset the Phase to anti-sync, or
// change one's Rate to drift them apart — all per person, no clusters.

export const COLORS = [
  "#3b6fe0", "#e8663f", "#17a2a2", "#e0245e",
  "#b07914", "#9b5de5", "#1f9e6f", "#c14fa0",
];
export const SHAPES = ["disc", "ring", "triangle", "square", "diamond", "star", "hexagon", "plus"];

const NAMES = [
  "Wren", "Ada", "Kai", "Mira", "Theo", "Luca", "Noor", "Ivy", "Rex", "Sol",
  "Juno", "Otis", "Nia", "Ezra", "Cleo", "Remy", "Faye", "Milo", "Zara", "Hugo",
  "Lena", "Arlo", "Suki", "Beau", "Iris", "Dax", "Vera", "Cody", "Esme", "Finn",
];

const W = 80;         // rolling window (~8s at 10 Hz) for corr / hr_var / PLV
const DT = 0.1;       // fixed sim step (10 Hz) — matches the real broadcast cadence
const DEG = Math.PI / 180;
const HR_TAU = 1.2;   // HR smoothing time (s) — the broadcast hr is "smoothed instantaneous HR"

let _spare = null;
function randn() {
  if (_spare !== null) { const s = _spare; _spare = null; return s; }
  let u = 0, v = 0;
  while (u === 0) u = Math.random();
  while (v === 0) v = Math.random();
  const r = Math.sqrt(-2 * Math.log(u));
  _spare = r * Math.sin(2 * Math.PI * v);
  return r * Math.cos(2 * Math.PI * v);
}

function std(buf) {
  let m = 0;
  for (let i = 0; i < buf.length; i++) m += buf[i];
  m /= buf.length;
  let s = 0;
  for (let i = 0; i < buf.length; i++) { const d = buf[i] - m; s += d * d; }
  return Math.sqrt(s / buf.length);
}

// Windowed Pearson correlation of two equal-length HR buffers. Near-flat signals
// (SD below the noise floor) return 0 rather than a spurious correlation.
function pearson(a, b) {
  const n = a.length;
  let ma = 0, mb = 0;
  for (let i = 0; i < n; i++) { ma += a[i]; mb += b[i]; }
  ma /= n; mb /= n;
  let cov = 0, va = 0, vb = 0;
  for (let i = 0; i < n; i++) {
    const da = a[i] - ma, db = b[i] - mb;
    cov += da * db; va += da * da; vb += db * db;
  }
  if (va < 1e-6 || vb < 1e-6) return 0;
  return cov / Math.sqrt(va * vb);
}

// Phase-locking value over the window: magnitude of the mean unit vector of the
// pairwise phase difference. 1 = perfectly locked, ~0 = drifting apart.
function plvOf(pa, pb) {
  const n = pa.length;
  let sx = 0, sy = 0;
  for (let i = 0; i < n; i++) { const d = pa[i] - pb[i]; sx += Math.cos(d); sy += Math.sin(d); }
  return Math.hypot(sx, sy) / n;
}

const mod2pi = (x) => ((x % (2 * Math.PI)) + 2 * Math.PI) % (2 * Math.PI);
const r1 = (x) => Math.round(x * 10) / 10;
const r2 = (x) => Math.round(x * 100) / 100;
const rand = (lo, hi) => lo + Math.random() * (hi - lo);

// The generative variables — the knobs the dev panel exposes per person.
export const GEN_VARS = ["restHr", "envRate", "envPhase", "envDepth", "beatPhase"];
export const VARS = [
  { key: "restHr", label: "HR₀", min: 40, max: 120, step: 1, unit: "bpm" },
  { key: "envRate", label: "Rate", min: 0.03, max: 0.50, step: 0.01, unit: "Hz" },
  { key: "envPhase", label: "Phase", min: 0, max: 360, step: 5, unit: "°" },
  { key: "envDepth", label: "Depth", min: 0, max: 14, step: 0.5, unit: "bpm" },
  { key: "beatPhase", label: "BeatΦ", min: 0, max: 360, step: 5, unit: "°" },
];

export class SimModel {
  constructor(nMax = 30) {
    this.t = 0;
    this.nMax = nMax;
    this.params = { noise: 0.6 }; // global sensor-noise floor (bpm)
    this.people = [];
    for (let i = 0; i < nMax; i++) this.people.push(this._makePerson(i));
    this.applyScenario("in_sync");
    this.setActiveCount(6);
  }

  _makePerson(i) {
    return {
      id: `p${String(i + 1).padStart(2, "0")}`,
      seat: i + 1,
      name: NAMES[i % NAMES.length],
      color: COLORS[i % COLORS.length],
      shape: SHAPES[Math.floor(i / COLORS.length) % SHAPES.length],
      // ---- generative variables (settable via setPerson) ----
      restHr: 64,      // bpm — baseline HR + beat frequency
      envRate: 0.10,   // Hz — HR-fluctuation (arousal) frequency
      envPhase: 0,     // deg — HR-fluctuation phase offset
      envDepth: 6,     // bpm — HR-fluctuation amplitude (0 = flat)
      beatPhase: 0,    // deg — cardiac beat phase offset
      connection: "connected",
      active: false,
      // ---- derived / running state ----
      noiseSm: 0,      // EMA-smoothed sensor noise only (the envelope is NOT smoothed)
      phase: 0,
      hrBuf: new Float64Array(W).fill(64),
      phBuf: new Float64Array(W).fill(0),
      hr: 64, hrVar: 0.1, rmssd: 40,
    };
  }

  // ---- control surface (driven by the dev panel) ----------------------------

  setActiveCount(n) {
    n = Math.max(0, Math.min(this.nMax, Math.round(n)));
    this.people.forEach((p, i) => { const was = p.active; p.active = i < n; if (p.active && !was) this._prime(p); });
  }

  setParam(k, v) { this.params[k] = v; }

  setPerson(id, patch) {
    const p = this.people.find((x) => x.id === id);
    if (!p) return;
    Object.assign(p, patch);
    // If a generative variable changed, re-prime so the signal (and thus concordance
    // and hr_var) reflects the new value immediately — no window-fill lag. This makes
    // the cohort dwell timer the ONLY latency, so it's a clean, honest control.
    if (p.active && GEN_VARS.some((k) => k in patch)) this._prime(p);
  }

  // Fill a person's rolling buffers with their CURRENT signal over the past window, so
  // hr_var / concordance start at steady state instead of ramping up from a cold start.
  _prime(p) {
    const noise = this.params.noise;
    for (let k = 0; k < W; k++) {
      const tk = this.t - (W - 1 - k) * DT;
      const env = p.envDepth * Math.sin(2 * Math.PI * p.envRate * tk + p.envPhase * DEG);
      p.hrBuf[k] = p.restHr + env + noise * randn();
      p.phBuf[k] = mod2pi(2 * Math.PI * (p.restHr / 60) * tk + p.beatPhase * DEG);
    }
    p.noiseSm = 0;
    p.hr = p.hrBuf[W - 1];
    p.phase = p.phBuf[W - 1];
    p.hrVar = std(p.hrBuf);
  }

  // Scenario presets are just bulk assignments of the per-person generative variables —
  // a fast way to reach a canned state you can then hand-edit.
  applyScenario(name) {
    const P = this.people;
    switch (name) {
      case "in_sync": // one shared wave: match Rate + Phase + HR → concordance & R → 1
        P.forEach((p) => Object.assign(p, { restHr: 64, envRate: 0.10, envPhase: 0, envDepth: 6, beatPhase: 0 }));
        break;
      case "two_cliques": // two groups, each internally matched; a 90° envelope offset
        // makes cross-concordance a STABLE ~0 (orthogonal), so the clumps stay separate
        // without beating; different HR keeps their beats from locking across groups.
        P.forEach((p, i) => Object.assign(p, i % 2
          ? { restHr: 68, envRate: 0.10, envPhase: 90, envDepth: 6, beatPhase: 90 }
          : { restHr: 62, envRate: 0.10, envPhase: 0, envDepth: 6, beatPhase: 0 }));
        break;
      case "anti_phase": // same rate, opposite envelope phase → cross-concordance → −1
        P.forEach((p, i) => Object.assign(p, i % 2
          ? { restHr: 64, envRate: 0.10, envPhase: 180, envDepth: 6, beatPhase: 180 }
          : { restHr: 64, envRate: 0.10, envPhase: 0, envDepth: 6, beatPhase: 0 }));
        break;
      case "independent": // every person a different wave → nobody correlates
        // Rates are CONTINUOUS and spread WIDE (not rounded to 0.01, which caused many
        // people to share an exact rate → stable high concordance → spurious cohorts that
        // percolated everyone into one clump). Wide spread keeps windowed concordance ~0.
        P.forEach((p) => Object.assign(p, {
          restHr: Math.round(rand(52, 84)),
          envRate: rand(0.05, 0.45),
          envPhase: Math.round(rand(0, 360)),
          envDepth: r1(rand(5, 8)),
          beatPhase: Math.round(rand(0, 360)),
        }));
        break;
      default:
        return;
    }
    this.scenario = name;
    for (const p of this.people) if (p.active) this._prime(p); // steady state immediately
  }

  // ---- simulation -----------------------------------------------------------

  step() {
    const noise = this.params.noise;
    const aHr = 1 - Math.exp(-DT / HR_TAU);
    this.t += DT;
    const act = this.people.filter((p) => p.active);

    for (const p of act) {
      if (p.connection === "disconnected") {
        // Frozen: hold last HR/phase; buffers keep filling with the frozen value so the
        // person naturally decorrelates from everyone still moving.
        p.hrBuf.copyWithin(0, 1); p.hrBuf[W - 1] = p.hr;
        p.phBuf.copyWithin(0, 1); p.phBuf[W - 1] = p.phase;
        p.hrVar = std(p.hrBuf);
        continue;
      }
      const env = p.envDepth * Math.sin(2 * Math.PI * p.envRate * this.t + p.envPhase * DEG);
      // Smooth only the NOISE (sensor jitter); the envelope is the true signal and passes
      // through at full amplitude. Otherwise the HR EMA low-passes fast Rate values into
      // looking flat (low hr_var), and the flat-gate wrongly blocks them from cohorting.
      p.noiseSm += aHr * (noise * randn() - p.noiseSm);
      const hr = p.restHr + env + p.noiseSm;
      p.phase = mod2pi(2 * Math.PI * (p.restHr / 60) * this.t + p.beatPhase * DEG);
      p.hrBuf.copyWithin(0, 1); p.hrBuf[W - 1] = hr;
      p.phBuf.copyWithin(0, 1); p.phBuf[W - 1] = p.phase;
      p.hr = hr;
      p.hrVar = std(p.hrBuf);
      p.rmssd = 28 + 24 / (1 + p.restHr / 60);
    }

    return this._frame(act);
  }

  _frame(act) {
    const ids = act.map((p) => p.id);
    const n = ids.length;
    const matrix = Array.from({ length: n }, () => new Array(n).fill(0));
    const plv = Array.from({ length: n }, () => new Array(n).fill(0));
    for (let i = 0; i < n; i++) {
      matrix[i][i] = 1; plv[i][i] = 1;
      for (let j = i + 1; j < n; j++) {
        const c = pearson(act[i].hrBuf, act[j].hrBuf);
        const v = plvOf(act[i].phBuf, act[j].phBuf);
        matrix[i][j] = matrix[j][i] = c;
        plv[i][j] = plv[j][i] = v;
      }
    }

    let sx = 0, sy = 0;
    for (const p of act) { sx += Math.cos(p.phase); sy += Math.sin(p.phase); }
    const R = n ? Math.hypot(sx, sy) / n : 0;

    let cs = 0, cc = 0;
    for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) { cs += matrix[i][j]; cc++; }
    const cohesion = cc ? cs / cc : 0;

    const people = this.people.map((p) => {
      const on = p.active;
      return {
        person_id: p.id,
        display_name: p.name,
        color: p.color,
        shape: p.shape,
        seat: p.seat,
        device_id: `sim-${String(p.seat).padStart(2, "0")}`,
        connection: on ? p.connection : "disconnected",
        enrollment: on ? "active" : "assigned",
        quality: on ? (p.connection === "connected" ? 0.95 : 0.5) : 0,
        quality_flags: on && p.envDepth < 1 ? ["flat"] : [],
        hr: on ? r1(p.hr) : null,
        hr_var: on ? r2(p.hrVar) : null,
        rmssd: on ? r1(p.rmssd) : null,
        rmssd_delta: null,
        phase: on ? p.phase : null,
        last_seen: this.t,
        uptime: null,
        baseline_progress: null,
        rr_tail: [],
        hr_trace_tail: on ? Array.from(p.hrBuf.slice(-40), r1) : [],
      };
    });

    return {
      t: this.t,
      people,
      unassigned: [],
      synchrony: { person_ids: ids, matrix, plv, cohesion, order_param: R, mode: "zscore" },
      scenario: this.scenario,
      source: "sim",
    };
  }
}
