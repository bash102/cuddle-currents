// Dev control panel — the "scene tools" for iterating on presets.
//
// Drives a SimModel. Synchrony is variable-driven: each person exposes the GENERATIVE
// variables behind their signal (3.2–3.6), and you set them per person to put people in
// or out of sync. Scenario buttons (2.x) are bulk presets of those variables.
//
// Every control carries a NUMBER (e.g. 3.3) shown in the UI and repeated in its hover
// tooltip, so we can reference items unambiguously while iterating ("bump 3.3 Rate").
// TIPS below is the single source of truth for both.

import { VARS } from "./model.js";
import { COHORT } from "./cohort.js";

const CONNECTIONS = ["connected", "stale", "reconnecting", "disconnected"];
const SCENARIOS = [
  ["2.1", "in_sync", "In sync"],
  ["2.2", "two_cliques", "Two cliques"],
  ["2.3", "anti_phase", "Anti-phase"],
  ["2.4", "independent", "Independent"],
];
// Per-person variable columns map to tip ids 3.2 … 3.6, in VARS order.
const VAR_TIP = Object.fromEntries(VARS.map((v, i) => [v.key, `3.${2 + i}`]));

const TIPS = {
  "1.1": "Order R — Kuramoto order parameter (0–1): how phase-locked the group's beats are. Rises when people share HR₀ (3.2) & BeatΦ (3.6). Sits near 1/√N at no-sync (~0.18 for 30), NOT 0.",
  "1.2": "Cohesion — mean pairwise HR concordance (−1..1): how much HR fluctuations co-move. Rises when people share Rate (3.3) & Phase (3.4). This is what the layout edges track.",
  "1.3": "Active — number of people currently on stage.",
  "1.4": "People — how many of the 30 are active. Independent of the scenario.",
  "1.5": "Noise — global sensor-noise floor (bpm) added to every HR. Higher = messier signal, lower / less reliable concordance.",
  "1.6": "Qualify time — seconds a pair's concordance must be SUSTAINED before it counts as a cohort (an edge is drawn). Stops momentary value overlaps from reading as sync. 0 = instant (raw).",
  "1.7": "Qualify level — the smoothed concordance threshold a pair must exceed for 'Qualify time' to cohort. Higher = only strong, confident sync qualifies.",
  "2.1": "In sync — everyone gets the same wave (HR₀/Rate/Phase): concordance & Order R → 1.",
  "2.2": "Two cliques — two internally-matched groups offset 90°, so they stay separate without merging or repelling.",
  "2.3": "Anti-phase — two groups on the same Rate but opposite Phase: cross-concordance → −1.",
  "2.4": "Independent — every person a different wave: nobody correlates (baseline / control).",
  "3.1": "Person — identity: color × shape glyph, name, seat #.",
  "3.2": "HR₀ — resting heart rate (bpm). Sets the HR baseline AND the beat frequency. Matching HR₀ (+ BeatΦ) locks two people's beats → raises Order R (1.1).",
  "3.3": "Rate — frequency (Hz) of this person's HR-fluctuation wave. Two people cohort (concordance high) ONLY when their Rate matches. Different Rate = they drift in and out.",
  "3.4": "Phase — phase offset (°) of the HR wave. With matched Rate: same Phase = synced, 180° = anti-synced, 90° = uncorrelated.",
  "3.5": "Depth — amplitude (bpm) of the HR wave. 0 = flat signal (nothing to correlate → concordance unreliable). Bigger = stronger, more trustworthy concordance.",
  "3.6": "BeatΦ — cardiac beat-phase offset (°). With matched HR₀: same BeatΦ raises Order R; spreading BeatΦ lowers it. Doesn't affect concordance.",
  "3.7": "Connection — link state: connected / stale / reconnecting / disconnected. Tests how a preset renders presence & roaming.",
  "3.8": "Var — hr_var: SD of HR over the ~8s window (bpm). Tracks Depth (3.5). Low (<~0.8) = flat/unreliable.",
  "3.9": "Trace — recent smoothed-HR history (sparkline): the signal whose windowed correlation becomes concordance.",
  "3.10": "Beat — beat phase: the dot pulses once per heartbeat (the `phase` field).",
};

const tipText = (id) => TIPS[id].split(" — ").slice(1).join(" — ");
const T = (id) => `title="${id} — ${tipText(id)}"`;
const N = (id) => `<span class="num">${id}</span>`;

// Draw a person's recent HR history into their row sparkline, auto-scaled.
function drawSpark(r, trace, color) {
  const c = r.spark, cx = r.sctx, w = c.width, h = c.height;
  cx.clearRect(0, 0, w, h);
  if (!trace || trace.length < 2) return;
  let mn = Infinity, mx = -Infinity;
  for (const v of trace) { if (v < mn) mn = v; if (v > mx) mx = v; }
  const rng = Math.max(1, mx - mn); // >=1 bpm so a flat line sits mid-height, not clipped
  cx.strokeStyle = color; cx.lineWidth = 2; cx.beginPath();
  trace.forEach((v, i) => {
    const x = (i / (trace.length - 1)) * (w - 2) + 1;
    const y = h - 3 - ((v - mn) / rng) * (h - 6);
    i ? cx.lineTo(x, y) : cx.moveTo(x, y);
  });
  cx.stroke();
}

const css = `
#simctl { position: fixed; top: 0; right: 0; width: 620px; height: 100vh; overflow-y: auto;
  background: rgba(20,10,16,0.94); color: #f2e4de; font: 12px/1.4 system-ui, sans-serif;
  padding: 14px 14px 40px; box-sizing: border-box; z-index: 10;
  border-left: 1px solid rgba(255,255,255,0.08); backdrop-filter: blur(6px); }
#simctl h2 { font-size: 12px; letter-spacing: .12em; text-transform: uppercase;
  color: #b89; margin: 18px 0 8px; font-weight: 600; }
#simctl h2:first-child { margin-top: 0; }
#simctl .doclink + h2 { margin-top: 2px; }
#simctl .doclink { display: block; margin-bottom: 10px; color: #e8663f;
  text-decoration: none; font-size: 11px; font-weight: 600; letter-spacing: .02em;
  border: 1px solid rgba(232,102,63,0.4); border-radius: 6px; padding: 5px 9px; }
#simctl .doclink:hover { background: rgba(232,102,63,0.14); border-color: #e8663f; }
#simctl .num { display: inline-block; color: #e8663f; font-weight: 600;
  font-variant-numeric: tabular-nums; margin-right: 4px; cursor: help; }
#simctl .row { display: flex; align-items: center; gap: 8px; margin: 6px 0; cursor: help; }
#simctl label { flex: 0 0 96px; color: #c9b; }
#simctl input[type=range] { flex: 1; }
#simctl .val { flex: 0 0 34px; text-align: right; font-variant-numeric: tabular-nums; color: #fff; }
#simctl .btns { display: flex; flex-wrap: wrap; gap: 6px; }
#simctl button { background: #2e1622; color: #f2e4de; border: 1px solid rgba(255,255,255,0.12);
  border-radius: 7px; padding: 6px 9px; cursor: pointer; font: inherit; }
#simctl button:hover { border-color: rgba(255,255,255,0.35); }
#simctl button.on { background: #e8663f; border-color: #e8663f; color: #150a10; font-weight: 600; }
#simctl button .num { color: inherit; opacity: .7; margin-right: 3px; }
#simctl .note { color: #a89; font-size: 11px; margin: 4px 0 0; line-height: 1.5; }
#simctl .metrics { display: flex; gap: 16px; font-variant-numeric: tabular-nums; }
#simctl .metrics > div { cursor: help; }
#simctl .metrics b { color: #fff; font-size: 16px; display: block; }
#simctl .metrics span { color: #b89; font-size: 10px; text-transform: uppercase; letter-spacing: .06em; }

/* per-person variable table */
#simctl .ptable { margin-top: 6px; }
#simctl .prow { display: grid;
  grid-template-columns: 14px minmax(40px,1fr) 46px 46px 46px 46px 46px 86px 30px 48px 14px;
  align-items: center; gap: 5px; padding: 3px 2px;
  border-bottom: 1px solid rgba(255,255,255,0.05); }
#simctl .phead { color: #b89; font-size: 10px; text-transform: uppercase; letter-spacing: .03em;
  border-bottom: 1px solid rgba(255,255,255,0.14); position: sticky; top: 0;
  background: #1a0d14; z-index: 1; }
#simctl .phead span { cursor: help; }
#simctl .prow.off { opacity: .34; }
#simctl .glyph { width: 12px; height: 12px; border-radius: 50%; }
#simctl .pname { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#simctl .d { text-align: right; font-variant-numeric: tabular-nums; color: #fff; font-size: 11px; }
#simctl .beat { width: 12px; height: 12px; border-radius: 50%; opacity: .2; }
#simctl .spark { width: 48px; height: 14px; display: block; }
#simctl .nin { width: 100%; box-sizing: border-box; background: #241019; color: #fff;
  border: 1px solid rgba(255,255,255,0.14); border-radius: 5px; padding: 2px 3px;
  font: 11px system-ui; text-align: right; -moz-appearance: textfield; }
#simctl .nin::-webkit-inner-spin-button, #simctl .nin::-webkit-outer-spin-button { -webkit-appearance: none; margin: 0; }
#simctl select { background: #241019; color: #f2e4de; border: 1px solid rgba(255,255,255,0.14);
  border-radius: 5px; padding: 2px 3px; font: 11px system-ui; width: 100%; box-sizing: border-box; }
`;

export function mountControls(model, { onFrame } = {}) {
  const style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);

  const el = document.createElement("div");
  el.id = "simctl";
  document.body.appendChild(el);

  const nActive = model.people.filter((p) => p.active).length;

  el.innerHTML = `
    <a class="doclink" href="/VIZ_DATA_REFERENCE.md" target="_blank" rel="noopener">📄 Viz Data Reference ↗</a>
    <h2>1 · Scene</h2>
    <div class="metrics">
      <div ${T("1.1")}>${N("1.1")}<b id="m-r">–</b><span>Order R</span></div>
      <div ${T("1.2")}>${N("1.2")}<b id="m-coh">–</b><span>Cohesion</span></div>
      <div ${T("1.3")}>${N("1.3")}<b id="m-n">–</b><span>Active</span></div>
    </div>
    <div class="row" ${T("1.4")}><label>${N("1.4")}People</label>
      <input type="range" id="s-active" min="0" max="30" step="1" value="${nActive}">
      <span class="val" id="v-active">${nActive}</span></div>
    <div class="row" ${T("1.5")}><label>${N("1.5")}Noise</label>
      <input type="range" id="s-noise" min="0" max="3" step="0.1" value="${model.params.noise}">
      <span class="val" id="v-noise"></span></div>
    <div class="row" ${T("1.6")}><label>${N("1.6")}Qualify t</label>
      <input type="range" id="s-qualt" min="0" max="10" step="0.5" value="${COHORT.qualTime}">
      <span class="val" id="v-qualt"></span></div>
    <div class="row" ${T("1.7")}><label>${N("1.7")}Qualify lvl</label>
      <input type="range" id="s-qualon" min="0.1" max="0.9" step="0.05" value="${COHORT.qualOn}">
      <span class="val" id="v-qualon"></span></div>

    <h2>2 · Scenario <span class="num" style="color:#a89;cursor:default">preset variable sets</span></h2>
    <div class="btns" id="scn"></div>

    <h2>3 · People <span class="num" style="color:#a89;cursor:default">generative variables per person</span></h2>
    <p class="note"><b>Cohort two people:</b> match their <b>3.3 Rate</b> + <b>3.4 Phase</b> (→ Cohesion / edges). <b>Lock their beats:</b> match <b>3.2 HR₀</b> + <b>3.6 BeatΦ</b> (→ Order R). 180° Phase = anti-sync; <b>3.5 Depth</b> 0 = flat.</p>
    <div class="ptable" id="ptable"></div>
  `;

  // ---- 2 · Scenario buttons -------------------------------------------------
  const scn = el.querySelector("#scn");
  let activePreset = model.scenario; // separate from any persistent sim mode
  for (const [id, key, label] of SCENARIOS) {
    const b = document.createElement("button");
    b.innerHTML = `${N(id)}${label}`;
    b.dataset.scn = key;
    b.setAttribute("title", `${id} — ${tipText(id)}`);
    b.onclick = () => {
      activePreset = key;
      model.applyScenario(key);
      syncScenarioButtons();
      buildPeople(); // variables changed
    };
    scn.appendChild(b);
  }
  function syncScenarioButtons() {
    scn.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.scn === activePreset));
  }
  // Any manual variable edit means the state no longer matches a canned scenario.
  function markCustom() {
    if (activePreset === null) return;
    activePreset = null;
    syncScenarioButtons();
  }

  // ---- 1.5 · Noise + 1.4 · People count -------------------------------------
  const sNoise = el.querySelector("#s-noise"), vNoise = el.querySelector("#v-noise");
  const updNoise = () => { vNoise.textContent = model.params.noise.toFixed(1); };
  sNoise.oninput = () => { model.setParam("noise", parseFloat(sNoise.value)); updNoise(); };
  updNoise();

  // Cohort qualification rule (shared COHORT config, read live by the tracker).
  const sQualt = el.querySelector("#s-qualt"), vQualt = el.querySelector("#v-qualt");
  const updQualt = () => { vQualt.textContent = COHORT.qualTime.toFixed(1) + "s"; };
  sQualt.oninput = () => { COHORT.qualTime = parseFloat(sQualt.value); updQualt(); };
  updQualt();
  const sQualon = el.querySelector("#s-qualon"), vQualon = el.querySelector("#v-qualon");
  const updQualon = () => { vQualon.textContent = COHORT.qualOn.toFixed(2); };
  sQualon.oninput = () => { COHORT.qualOn = parseFloat(sQualon.value); updQualon(); };
  updQualon();

  const sActive = el.querySelector("#s-active"), vActive = el.querySelector("#v-active");
  sActive.oninput = () => {
    vActive.textContent = sActive.value;
    model.setActiveCount(parseInt(sActive.value, 10));
    buildPeople();
  };

  // ---- 3 · Per-person generative-variable table -----------------------------
  const ptable = el.querySelector("#ptable");
  const rows = new Map(); // person_id -> { spark, sctx, var, beat }

  function header() {
    const h = document.createElement("div");
    h.className = "prow phead";
    const varHeads = VARS.map((v) => `<span class="d" ${T(VAR_TIP[v.key])}>${v.label}</span>`).join("");
    h.innerHTML = `
      <span></span>
      <span ${T("3.1")}>${N("3.1")}Person</span>
      ${varHeads}
      <span ${T("3.7")}>Conn</span>
      <span class="d" ${T("3.8")}>Var</span>
      <span ${T("3.9")}>Trace</span>
      <span ${T("3.10")}></span>`;
    return h;
  }

  function buildPeople() {
    ptable.innerHTML = "";
    rows.clear();
    ptable.appendChild(header());
    for (const p of model.people) {
      const row = document.createElement("div");
      row.className = "prow" + (p.active ? "" : " off");
      const inputs = VARS.map((v) =>
        `<input class="nin v-${v.key}" type="number" min="${v.min}" max="${v.max}" step="${v.step}" value="${p[v.key]}" ${T(VAR_TIP[v.key])}>`).join("");
      const cnOpts = CONNECTIONS.map((c) => `<option value="${c}" ${p.connection === c ? "selected" : ""}>${c}</option>`).join("");
      row.innerHTML = `
        <span class="glyph" style="background:${p.color}"></span>
        <span class="pname" title="${p.name} · #${p.seat}">${p.name}</span>
        ${inputs}
        <select class="cn">${cnOpts}</select>
        <span class="d var">–</span>
        <canvas class="spark" width="96" height="28"></canvas>
        <span class="beat" style="background:${p.color}"></span>`;
      for (const v of VARS) {
        row.querySelector(".v-" + v.key).oninput = (e) => {
          const val = parseFloat(e.target.value);
          if (!Number.isNaN(val)) { model.setPerson(p.id, { [v.key]: val }); markCustom(); }
        };
      }
      row.querySelector(".cn").onchange = (e) => { model.setPerson(p.id, { connection: e.target.value }); markCustom(); };
      ptable.appendChild(row);
      const spark = row.querySelector(".spark");
      rows.set(p.id, { var: row.querySelector(".var"), beat: row.querySelector(".beat"), spark, sctx: spark.getContext("2d") });
    }
  }

  syncScenarioButtons();
  buildPeople();

  // ---- live readout ---------------------------------------------------------
  const mR = el.querySelector("#m-r"), mCoh = el.querySelector("#m-coh"), mN = el.querySelector("#m-n");
  return {
    update(frame) {
      if (!frame) return;
      const s = frame.synchrony || {};
      mR.textContent = (s.order_param ?? 0).toFixed(2);
      mCoh.textContent = (s.cohesion ?? 0).toFixed(2);
      mN.textContent = (s.person_ids || []).length;
      for (const p of frame.people || []) {
        const r = rows.get(p.person_id);
        if (!r) continue;
        r.var.textContent = p.hr_var == null ? "–" : p.hr_var.toFixed(1);
        r.beat.style.opacity = p.phase == null ? 0.12 : (0.18 + 0.82 * (0.5 + 0.5 * Math.cos(p.phase))).toFixed(2);
        drawSpark(r, p.hr_trace_tail, p.color);
      }
      if (onFrame) onFrame(frame);
    },
  };
}
