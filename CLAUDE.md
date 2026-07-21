# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Phase-1 proof-of-concept visualizer for **interpersonal heart-rate synchrony**: a handful
of Coospo BLE armbands connect directly to a Mac, and the app shows per-person signal
quality plus cross-person alignment. The whole architecture is built so the direct-BLE
ingestion can later be swapped for BLE→WiFi gateways without touching processing or
visualization. See `README.md` for the science and metrics.

## Commands

```bash
pip install -e '.[dev]'                 # editable install with test deps

cuddle --source sim --scenario drift_into_sync --people 6   # no hardware; the demo path
cuddle --source ble --record captures/session.jsonl         # real bands, record raw samples
cuddle --source replay --capture captures/session.jsonl     # replay a recording, no hardware

pytest                                  # full suite
pytest tests/test_synchrony.py -k lag   # a single file / test by name
```

Server binds `127.0.0.1:8770` (override `--port`/`--host` or `config/app.yaml`). Open the
**Show** view at `/` and the **Ops** view at `/ops` (typically on two monitors). Sim-only
flags: `--scenario` (switchable live in Ops), `--people`, `--baseline-scale` (shortens the
rest calibration for demos — it scales both duration *and* `min_beats`).

## Architecture

Pipeline, left to right — each stage depends only on the one before it:

```
sources/ → hub/ → processing/ → transport/ → frontend/
```

- **`sources/`** — `SampleSource` Protocol (`base.py`): `bind`/`unbind`/`subscribe`/
  `unassigned_devices`/`connection_states`. This is the **one swap point** — `ble_source`,
  `sim_source` (Kuramoto-coupled oscillators + `ReplaySource`) are interchangeable. A source
  emits `NormalizedSample`s already tagged with `person_id` via its internal device→person
  bindings.
- **`hub/`** — `registry.SessionStore` owns a `PersonSession` per person; `enrollment` runs
  the DISCOVERED→ASSIGNED→BASELINING→CALIBRATED→ACTIVE lifecycle and band reuse; `ingest`
  pumps samples from the source into sessions + baseline collectors.
- **`processing/`** — pure functions over sessions: `resample`, `artifact` (beat spike
  correction), `baseline`, `abstract` (smoothed HR / RMSSD / phase), `synchrony`
  (concordance matrix + PLV + cohesion), and `frame.build_frame` which assembles everything.
- **`transport/ws_server.py`** — FastAPI: one WebSocket `/ws` broadcast + REST control
  (`/api/enroll`, `/api/reassign`, `/api/release`, `/api/retire`, `/api/baseline/start`,
  `/api/sync-mode`, `/api/scenario`, `/api/orchestrator/{mode,connect,release,pin}`).
  `app.Engine` ticks enrollment + builds a frame on a fixed cadence and fans it out.
- **`hub/orchestration/`** (Level B, opt-in via `cuddle --source mqtt --orchestrate` or
  `orchestrator.enabled` in `app.yaml`) — app-orchestrated gateway assignment: `world.py`
  (world-model + coverage memory), `plan.py` (pure stability-first planner — connected bands
  aren't moved except a bounded unserved-band rebalance), `orchestrator.py` (the async MQTT
  service). Additive to the Level A contract: `cuddle/<gw>/report` (retained
  capacity/mode/connected/seen), `cuddle/<gw>/cmd` (connect/release), `cuddle/control/mode`
  (managed|opportunistic, retained), `cuddle/control/online` (retained, orchestrator LWT —
  every gateway auto-reverts to opportunistic if this goes stale). `hr`/`status`/`online`
  topics are unchanged.
- **`frontend/`** — two decoupled pages rendering the same stream: `/` **Show** (the clean
  force-directed "puddle") and `/ops` **Ops** (technical status + reassignment UI).

### Invariants worth knowing before editing

- **`StateFrame` (`core/models.py`) is the only contract** between backend and both
  frontends. Both pages render off it and nothing else — add a field there to surface data.
- **Sessions are keyed by stable `person_id`, never by device address.** A band that drops
  and rejoins, or is swapped to another band, resumes the same session (history, RMSSD
  window, matrix position). Enrollment persists to `config/enrollment.yaml`.
- **A device→person binding lives in three places that must stay in sync:**
  `profile.device_id`, `registry._device_to_person`, and the source's `_bindings`.
  `EnrollmentManager` is the only coordinator; reassign/park/retire must update all three or
  a band keeps routing samples to its old owner (a person "getting data" while shown
  disconnected). Don't touch one without the others.
- **Concordance/metrics are backend; the puddle *layout* is frontend.** `synchrony.py`
  produces the raw concordance matrix (`zscore` mode by default = offset-invariant windowed
  Pearson). The concordance→distance mapping, the HR-variability (flat-signal) gate, and the
  temporal smoothing all live in `frontend/js/show/puddle.js` (tunables at the top of
  `FORCE`). The Ops heatmap shows the raw matrix; the Show puddle shows the gated/smoothed
  version.
- **Flat-signal caveat:** `zscore` concordance reads *dynamics*, so when HR is near-flat
  (windowed SD ~<1–2 bpm, calm rest) it correlates noise. Expect the puddle to *not* clump
  such signals — that is intentional; trust level agreement (`raw` mode / HR readouts) there.
- **`artifact.correct_rr` runs before resampling/smoothing** and is deliberately surgical
  (Hampel + Malik floor + missed/extra-beat repair) so it removes spikes without flattening
  the real dynamics the coherence metric needs. Config under `artifact:` in `app.yaml`.

## Runtime state & the frontend build

- **No frontend build step.** `frontend/js/**` is served live via FastAPI `StaticFiles`, so
  a **hard-refresh** picks up JS/HTML/CSS edits with no restart. **Python changes need the
  `cuddle` process restarted** (no hot reload).
- **`config/enrollment.yaml` and `captures/*.jsonl` are runtime state, not tracked in git.**
  The running server *owns* `enrollment.yaml` and rewrites it on any roster change — don't
  hand-edit it while the server is running (it will be clobbered); use the REST API / Ops UI
  (`/api/retire` etc.) instead.

## Conventions

- macOS CoreBluetooth caps concurrent peripherals at ~7–10 — Phase 1 stays within that; the
  gateway path is Phase 2.
- Scenarios (`sources/scenarios.py`) are pure/deterministic given their construction args, so
  tests assert on their shape (`tests/test_sim_scenarios.py`); add new scenarios to
  `make_scenario`, `SCENARIO_NAMES`, and the Ops dropdown in `frontend/ops.html`.
