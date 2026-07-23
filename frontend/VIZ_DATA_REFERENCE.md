# Visualization Data Reference

## Running the preset harness

No build step and no `npm install` — the Pixi / pixi-filters / particle-emitter libs are
vendored under `frontend/vendor/` and loaded via the import map in `dev.html`. Just serve
`frontend/` over HTTP (ES modules need `http://`, not `file://`):

```
cd frontend
python3 -m http.server 8081
# then open http://127.0.0.1:8081/dev.html
```

**The page has two halves:**
- **Left — the stage.** The live PixiJS render of the current preset.
- **Right — the dev controls.** *Data* controls (numbered): add/remove people, pick a sync
  **scenario** (in_sync / two_cliques / anti_phase / independent), tune noise, and edit each
  person's driver variables (rest HR, envelope rate/phase/depth). This drives the visuals; it
  is **not** the backend — to render off live bands instead, point the app at `/ws`.

**Designing presets (the panel under the title):**
- **Open Preset** (top-left button) — switch between presets / Import a saved `.json`.
- **Save · Save As… · Rename · Reset** (top of the control panel) — all operate on the **whole
  preset** (physics, colors, filters, particles, events — everything `getState()` captures),
  saved to `localStorage`.
- Grouped sliders (Physics, Motion, Cohort Lifecycle, Edges, Nodes, Colors) plus editors for the
  **Filter Stack**, **Particle Systems**, and **Events** (see §5). Edits apply live.

**Assets** referenced by path (particle/node PNGs, emitter JSON) go anywhere under `frontend/`;
the server serves them, and presets store only the path (e.g. `/assets/spark.png`).

---

Everything a preset can draw comes from one object — the **`StateFrame`**, broadcast
~10×/second over `/ws` (live bands) or produced identically by the dev harness
(`frontend/js/sim/`). This is the single contract; presets render off this and nothing
else. Canonical definition: `src/cuddle/core/models.py`.

```
StateFrame
├─ t            : float        server time (s)
├─ people[]     : PersonState  one per enrolled person (see §1)
├─ unassigned[] : DeviceInfo   bands not yet bound to a person (Ops only)
├─ synchrony    : SynchronyState  the relational / community data (see §2)
├─ scenario     : string|null the sim scenario, if any
└─ source       : "ble"|"sim"|"mqtt"
```

Access it in a preset via `getFrame()` from `js/store.js` (or `subscribe(fn)`).

---

## 1. Per-person data (`PersonState`)

One entry per person, in `frame.people[]`. Filter to the ones on stage with
`p.enrollment === "active"`.

### Identity — fixed for the session (assigned once at enrollment)

| Field | Type | Meaning | Viz use |
|---|---|---|---|
| `person_id` | string | Stable logical id (survives band drop/rejoin). **Key everything on this.** | keying, matrix lookup |
| `display_name` | string | Person's name (→ initials for labels) | label |
| `color` | hex string | 1 of 8 identity hues | primary color channel |
| `shape` | string | 1 of 8 glyphs: `disc ring triangle square diamond star hexagon plus` | secondary identity channel |
| `seat` | int | 1-based number ("you're #7") | label / find-yourself |
| `device_id` | string\|null | Currently bound sensor | Ops only |

`color × shape` = 64 unique combos (covers the 30-person target). Palette is fixed in
`hub/enrollment.py` and mirrored in `js/sim/model.js` / `js/shapes.js`.

### Live physiological signal — changes every frame

| Field | Type | Range | Meaning | Viz use |
|---|---|---|---|---|
| `hr` | float\|null | ~45–180 bpm | Smoothed instantaneous heart rate | size, speed, the number |
| `phase` | float\|null | 0–2π rad | Beat phase — **when** the heart beats | pulse timing, particle burst |
| `hr_var` | float\|null | ~0–8 bpm | SD of HR over the sync window. **Low = flat/calm signal** | trust / gating (see §3) |
| `rmssd` | float\|null | ~10–80 ms | Rolling HRV (autonomic tone) | texture, glow intensity |
| `rmssd_delta` | float\|null | signed | HRV vs the person's own resting baseline | relaxation vs stress |
| `hr_trace_tail` | float[] | recent HR | Short history of smoothed HR | sparkline / trails |
| `rr_tail` | float[] | seconds | Recent inter-beat intervals | beat-accurate effects |

`null` means "not known yet" (person just became active, pre-baseline). Guard for it.

### Status / lifecycle — mostly presence & Ops

| Field | Type | Values | Viz use |
|---|---|---|---|
| `enrollment` | string | `discovered assigned baselining calibrated active retired` | **show only `active`** on stage |
| `connection` | string | `connected stale reconnecting disconnected` | opacity / presence (roaming) |
| `quality` | float | 0–1 | signal confidence | fade weak signals |
| `quality_flags` | string[] | e.g. `["flat"]` | specific issues | debug / Ops |
| `baseline_progress` | float\|null | 0–1 | calibration progress | Ops meter |
| `uptime`,`last_seen` | float\|null | seconds / time | connection timing | Ops |

**Suggested Show-view mapping of `connection` → alpha:** connected `1.0`, stale `0.55`,
reconnecting `0.35`, disconnected `0.12` (fade out but keep position, since the person
may rejoin).

---

## 2. Community / relational data (`SynchronyState`)

This is the heart of the piece: not per-person, but **per-relationship** — who is in
sync with whom. Found at `frame.synchrony`.

| Field | Type | Meaning |
|---|---|---|
| `person_ids` | string[] | Row/column order for `matrix` & `plv`. **Index into these by position in this array**, not by `people[]` order. |
| `matrix` | float[N][N] | Pairwise **concordance** ∈ [−1, 1]. `+1` fully co-moving, `0` unrelated, `−1` anti-correlated. Diagonal = 1. Symmetric. |
| `plv` | float[N][N] | Pairwise **phase-locking value** ∈ [0, 1]. `1` = beats locked in step, `0` = drifting. |
| `cohesion` | float | Mean of the pairwise concordance (upper triangle). One "how aligned overall" scalar. |
| `order_param` | float | **Kuramoto R** ∈ [0, 1]. The single "how synced is the puddle" number, from beat phase. |
| `mode` | string | `zscore` (default) \| `raw` \| `baseline_delta`. Under `zscore`, concordance = windowed Pearson correlation (pure dynamics, offset-invariant). |

Two independent measures, complementary:
- **`matrix` / `cohesion`** read the *HR dynamics* — do two people's fluctuations
  co-move? (This is what drives clumping.)
- **`plv` / `order_param`** read *beat timing* — are the actual heartbeats phase-locked?

**Important — R is not 0 at "no sync."** For N independent people, R ≈ `1/√N` (~0.45 for
5, ~0.18 for 30). Judge synchrony by R rising *above* that baseline, not by R > 0.

### Correct indexing

```js
const { person_ids, matrix } = frame.synchrony;
const k = new Map(person_ids.map((id, i) => [id, i]));
const concordance = (a, b) => matrix[k.get(a)]?.[k.get(b)] ?? 0; // a,b are person_ids
```

---

## 3. What actually makes people cohort together

Physiological synchrony is an alignment of **dynamics, not level** — two people can share
a high heart rate and be completely unsynchronized. What matters is whether their HR
*fluctuations* co-move. Two mechanisms produce that, and the data exposes both:

1. **Shared drive → concordance.** People exposed to the same influence (proximity, a
   shared event, co-regulation) have their HR envelopes rise and fall together. Windowed
   correlation of those envelopes is the **`matrix`**. Co-moving → high positive; opposed
   → negative; unrelated → ~0.
2. **Beat coupling → phase-locking.** Their individual beats pull toward a common rhythm
   (Kuramoto coupling). Measured by **`plv`** per pair and **`order_param` (R)** overall.

### How the simulator models it (so you can drive tests)

The harness is **generative and variable-driven** — no imposed groups. Each person's
signal is built from five settable variables, and synchrony *emerges* when people's
variables match:

```
HR(t)    = HR₀ + Depth · sin(2π · Rate · t + Phase) + noise   (then smoothed)
phase(t) = 2π · (HR₀/60) · t + BeatΦ                          (cardiac beat phase)
```

| Variable | Drives |
|---|---|
| **HR₀** (bpm) | HR baseline **and** beat frequency |
| **Rate** (Hz) | speed of the HR-fluctuation wave |
| **Phase** (°) | phase offset of that wave |
| **Depth** (bpm) | amplitude of the wave (0 = flat) |
| **BeatΦ** (°) | cardiac beat-phase offset |

- **Cohort two people** (high `matrix`, they clump) → match their **Rate + Phase**.
- **Anti-cohort** (`matrix` → −1, flung apart) → same Rate, **Phase 180°** apart.
- **Decorrelate** (`matrix` ~0, sit apart) → give them **different Rate** (their waves
  beat in and out of step).
- **Phase-lock beats** (high `plv` / `order_param`) → match **HR₀ + BeatΦ**.
- **Flat person** → **Depth 0**: HR sits at the noise floor (low `hr_var`), so its
  `matrix` entries correlate only noise and are unreliable — the flat-signal-gate case.

The **Noise** slider is the global sensor-noise floor. Scenario buttons (In sync / Two
cliques / Anti-phase / Independent) are just bulk presets of these per-person variables.

### Two guards every preset should apply

The raw `matrix` is noisy frame-to-frame (the arousal envelope drifts slowly relative to
the correlation window, so any single frame jitters — even genuinely independent pairs
spike). Don't react to raw values. Do what the reference puddle does:

1. **Temporal EMA** over ~8 s (`s ← s + (raw − s)·(1 − e^(−dt/τ))`, τ≈8) so only
   *sustained* concordance moves the layout, not one-frame spikes.
2. **Flat-signal gate** — trust a pair's concordance only when **both** hearts actually
   vary: multiply by `clamp((hr_var − varLo)/(varHi − varLo), 0, 1)` per person
   (varLo≈0.8, varHi≈3.0 bpm). Calm/flat signals get damped toward 0 rather than drawn as
   confidently synced.

Apply both and the flickering cross-pair noise settles out, leaving only real cohorts.

---

## 4. Quick reference — attribute → visual channel

Not prescriptive, just the natural fits:

| Data | Encodes | Common channel |
|---|---|---|
| `color`, `shape`, `seat` | who this is | identity (mandatory — people find themselves) |
| `phase` | the beat | pulse / scale throb / emitter burst |
| `hr` | arousal level | size, motion speed, brightness |
| `hr_var`, `rmssd` | signal richness / HRV | glow, texture density, trust |
| `connection` | presence | opacity |
| `matrix[i][j]` (gated+smoothed) | pairwise sync | **distance**, edge weight/color, whether they share a blob |
| `cohesion` / `order_param` | whole-group sync | central bloom, background intensity, "one organism" cues |

The relational data (`matrix`, `order_param`) is the most important dimension — it's what
turns 30 dots into a *group* portrait. Every preset should answer: **how does each person
show their relationship to the other 29?**

---

## 5. Working on a preset — which files to open

The visual layer is a **PixiJS preset system**. All presets read the same `StateFrame`
(§1–2) and share the plumbing; each preset is a self-contained module.

### Shared harness (rarely edited)
| File | What it is |
|---|---|
| `frontend/dev.html` | Dev page: mock data source + control panel + Pixi stage. Import map maps `pixi.js` → vendored build. |
| `frontend/js/store.js`, `js/ws.js` | Frame bus + live `/ws` client (renderer-agnostic). |
| `frontend/js/sim/*` | The **data simulator** + control panel (model.js, controls.js, cohort.js, mockSource.js). Edit to change the *data*, not the visuals. |
| `frontend/js/show/pixiApp.js` | Pixi Application, the **preset switcher**, the per-preset controls UI, and the save/export/import system. |
| `frontend/js/presets/registry.js` | The list of presets. **Add a new preset here** (import its factory, append an entry). |
| `frontend/vendor/*` | Vendored libs: `pixi.min.mjs`, `pixi-filters.min.mjs`, `particle-emitter.es.js`. |

### Preset: **Node Graph** (`node-graph`)
| File | Edit it to change… |
|---|---|
| `frontend/js/presets/nodeGraph.js` | **Everything visual for this preset.** `CFG` (top of file) = all tunables; `CONTROLS` = which knobs appear as sliders (with `tip`); the physics, cohort lifecycle, metaball necks, and labels live here. |
| `frontend/js/presets/particles.js` | The **particle systems** engine: named friendly-param defs (`SYSTEM_PARAMS`) + `ParticleSystem` (continuous per-node emitters + one-shot bursts). Systems are edited in the harness and saved with the preset. |
| `frontend/js/presets/filters.js` | The **post-process filter stack** (`FILTERS` registry + `FilterStack`) and `EventFilters` — positioned one-shot animated filters (Shockwave, Twist, Bulge, Zoom-blur) fired by events. |
| `frontend/js/presets/events.js` | The **choreography schema**: `EVENT_CATALOG`, reaction types/locations/triggers, and `defaultEvents()`. |
| `frontend/js/presets/dispatch.js` | The **choreography runtime** (`Choreographer`) — turns `CFG.events` into live particle/filter/property reactions each frame. |

Every preset factory returns `{ container, update, destroy, params, controls, getState, setState, applyParticles }`.
`params`+`controls` drive the on-screen sliders; `getState`/`setState` drive save / switch.

### Particle systems (harness-authored)
Systems are **named friendly-param defs** stored in `CFG.particleSystems` and edited live in the
Particle Systems panel (Shape, Rate/Burst, Life, Speed, Scale, Spawn R). Three are built-in and
undeletable (the renderer drives them via events): **`aura`** (continuous, one per node),
**`joinBurst`** (one-shot spray on cohort-join), and **`ringBurst`** (per-node ring ripple on join —
a cheap, scalable stand-in for a per-node shockwave). Add your own with **＋** — a new system is
defined + selectable in the Events editor, and fires once you bind it to an event.

Per system:
- **`shape`** — `scatter` (spray in random directions) or `ring` (particles fly radially outward
  from a thin torus = an expanding ring ripple).
- **`texture`** — path/URL to a PNG served by the frontend (e.g. `/assets/spark.png`). Blank = the
  generated soft dot. Loads async with a soft-dot fallback; a bad path warns and falls back.
- **`config`** — path/URL to a **Pixi particle-editor JSON file** (e.g. `/assets/emitters/spark.json`).
  When set it **overrides the sliders** for that system (the PNG + node/cohort color are still
  injected). This is the *hybrid* workflow: edit the file in the editor
  (https://userland.pixijs.io/particle-emitter-editor/) and save it back down — no copy-paste. Old
  editor exports are auto-upgraded; a starter file lives at `frontend/assets/emitters/example.json`.

### Events → reactions (choreography)
The renderer emits a fixed `EVENT_CATALOG` — **activated, joined, left, disconnected, beat, removed**.
A preset binds **reactions** to each (in `CFG.events`, edited in the Events panel). A reaction is
`{ type, ref, location, trigger }`:
- **type** — `particle` (fire a system), `filter` (positioned animated filter), or `property`
  (node scale-pop / opacity-dip / color-flash).
- **ref** — the particle-system name, filter type, or property.
- **location** — where it resolves: `node`, `cohort centroid`, or `world` (screen center).
- **trigger** — `hit` (once, at the moment) or `continuous`/`modulate` (every frame the state holds).

Each reaction has an **on/off toggle** and exposes the referenced item's **settings inline**: a
particle reaction shows the shared system's params (Shape, Rate/Burst, Life…); a filter reaction
shows that instance's params (amplitude, wavelength… + Duration) — the center comes from `location`;
a property reaction shows Amount + Duration.

Defaults: `activated → aura` (continuous, node), `joined → joinBurst` (hit) **+ shockwave** (hit,
node — the ripple). Event filters are capped at 4 concurrent passes so a mass sync can't stack
dozens of full-screen shockwaves.

### Filters, counted
The static **Filter Stack** (bloom, glow, outline, color-grade, rgb-split, bulge/pinch, zoom-blur,
twist, shockwave) runs as an ordered post-process over the bloom group; order + params are UI-editable
and only bloom is on by default. **Shockwave** and **Twist** are also usable as event filters (they
need animating — inert in the static stack). Adding a filter = import its class + one `FILTERS` entry;
add an `fx` descriptor to make it fireable as a positioned event filter.

### Node graphic
Each node is two tinted sprites: an opaque **core** and a bigger, dimmer **halo** — by default a
generated soft disc, tinted to the node/cohort color. The **Nodes** control group exposes **Node
size**, **Core PNG** + **Halo PNG** (path/URL, blank = disc — tinted, so use white/grayscale art),
**Halo** on/off, **Halo size** (× core), and **Halo alpha**. PNGs load async with a disc fallback.

### Edges
Edges are **not** a full graph — each node draws one metaball neck to a single **rotating partner**,
held a few seconds then rewired. Partner selection is **biased toward nearby members** (within the
neck's draw range, `(r1+r2)·6`), weighted to the closest few, so the edge reliably renders while the
partner still rotates for a live "jostle." A node briefly shows no neck while a link fades in/out on
rewire, or before it passes `tMaster`.

**Edge style** (Edges group): **metaball** (default — the generated gooey neck, drawn edge-to-edge)
or **png** — a stretched sprite from node **center to node center** (so it tucks under the cores and
avoids the metaball's hourglass pinch when nodes stack close). Set **Edge PNG** (blank = a generated
soft beam, tinted to the cohort color) and scale thickness with **Edge width**.

### Cohort glow
The soft circle behind each cohort (additive, tinted to the cohort color) is generated by default.
The **Colors** group exposes **Cohort glow PNG** (path/URL, blank = the generated circles — use a
white/grayscale radial glow like `/assets/glow.png`), plus **Cohort glow size** and **α**.

### Assets
Drop referenced files anywhere under `frontend/` (the dev server serves it): PNGs (e.g.
`frontend/assets/*.png`) for particle/node textures, and emitter JSON (e.g. `frontend/assets/emitters/*.json`)
for the advanced particle configs. Presets store only the **path**, so the file must exist at that path.

Built-in systems (`aura`, `joinBurst`, `ringBurst`) and the node core/halo are **generated at
runtime** — the PNG fields are blank by default. A **starter pack** ships under `frontend/assets/`
so the placeholders resolve out of the box (all white/grayscale, so they tint per node):

| File | For | Shape |
|---|---|---|
| `assets/soft-dot.png` | particle aura | soft radial dot |
| `assets/spark.png` | celebratory bursts | 4-point star |
| `assets/ring.png` | ring-burst / ripple | thin bright ring |
| `assets/node.png` | node **Core PNG** | solid disc, soft edge |
| `assets/glow.png` | node **Halo PNG** | wide soft glow |
| `assets/beam.png` | edge **Edge PNG** (png style) | horizontal soft beam |
| `assets/emitters/example.json` | a system's **Emitter JSON** | starter editor config |

Paste any of these paths (e.g. `/assets/spark.png`) into a system's PNG field or the Core/Halo PNG
to try them; or click the **📁** on any path field to pick from the files actually served under
`/assets/` (no typing). The **✕** clears a field back to the generated default, and the badge
(`generated`/`soft dot`/`sliders` → `PNG`/`file`) shows the current source at a glance.
