# cuddle-currents

A visualizer for the **interpersonal physiological synchrony** that emerges when
people are physically close in a "cuddle puddle" — the way heart rate and HRV align
between people during proximity, touch, and shared attention. Each person wears a
Coospo armband heart-rate monitor; the app shows that we're connected, that the raw
signal is good, the abstract per-person signal, and the alignment across people.

This repository is **Phase 1**: a proof of concept where a handful of bands connect
directly to a Mac over BLE. The architecture isolates the ingestion source so the
direct-BLE path can later be swapped for BLE→WiFi gateways to scale toward ~30 people
without rewriting the processing or the visualization.

## The science, briefly

Heart-rate synchrony is a measurable marker of real-world social engagement: in the wild
it rises with **physical proximity** (dyads within ~20 m), is stronger between **socially
familiar** people, and collapses in loud environments (He et al., *Heart rate synchrony
as a marker of real-world social engagement*, PNAS Nexus 2026,
[10.1093/pnasnexus/pgag181](https://doi.org/10.1093/pnasnexus/pgag181)). At the group
level, continuous inter-beat coupling **predicts group cohesion** (Tomashin, Gordon &
Wallot, *Interpersonal Physiological Synchrony Predicts Group Cohesion*, Front. Hum.
Neurosci. 2022, [10.3389/fnhum.2022.903407](https://doi.org/10.3389/fnhum.2022.903407)).

**The mechanism.** The interval between heartbeats is continuously regulated by the
autonomic nervous system, so each person's heart rate is a *fluctuating envelope*, not a
fixed number. Two people's envelopes come into alignment through two pathways: a
**stimulus-driven** one — both nervous systems modulated at once by a shared event,
amplified by co-presence and joint attention — and an **interaction-driven** one —
reciprocal co-regulation via the exchange of social signals (speech prosody, facial
expression, gesture). Crucially, synchrony is an alignment of *dynamics*, not of arousal
*level*: two people can share an elevated heart rate and be completely unsynchronized
(the PNAS Nexus study found no link between mean HR and synchrony). What matters is
whether their fluctuations **co-move** — which is why proximity, familiarity, and a quiet
setting matter (they enable the shared input and social exchange), and why the metrics
below are deliberately offset-invariant.

We quantify it two ways:

- **Concordance (matrix):** windowed Lin's concordance of smoothed HR between each
  pair. Under the default `zscore` normalization this equals windowed Pearson
  correlation (pure dynamics, offset-invariant).
- **Phase-locking / Kuramoto order parameter (R):** the length of the average of
  everyone's beat-phase unit vectors — offset-robust, and the single "how synced is
  the puddle" scalar (0–1). Note R is *not* 0 at no-sync: for N independent people it
  sits around 1/√N (~0.45 for 5), so judge synchrony by R rising above that baseline.
  It's complementary to cohesion — R measures beat *timing*, cohesion the HR *dynamics*.

Because individual physiology differs (resting HR, HRV, respiration), each person is
**baselined** at rest and their signal normalized before comparison.

One caveat the visualization respects: `zscore` concordance reads *dynamics*, so when a
person's HR is essentially **flat** (windowed SD near the sensor noise floor — ~1 bpm at
calm rest) it correlates noise and is unreliable. The Show puddle therefore **gates
concordance on HR variability** — a correlation only counts when both hearts actually
vary — so calm, uninformative signals are not drawn as confidently synced. For flat
traces, level agreement (`raw` mode, or simply the per-person HR readouts) is what to
trust.

## Hardware

**Bands.** Coospo **HW706** (BT 4.0) and **HW9** (BT 5.0) both expose the standard BLE Heart
Rate Service (`0x180D` / `0x2A37`) including RR intervals — no proprietary protocol. Any
standard BLE HR strap should work.

**Gateways (ESP32-S3).** macOS CoreBluetooth holds only ~7–10 peripherals at once, so scaling
past a handful of people uses **BLE→WiFi gateways**: an ESP32-S3 connects to the bands and
forwards the raw `0x2A37` notifications to the app over MQTT — the app still does all decoding,
so a gateway stays a dumb bridge. Firmware lives in `firmware/`:

- [`firmware/gateway-idf/`](firmware/gateway-idf/README.md) — the **ESP-IDF** build,
  **6 concurrent bands** (hardware-validated), plus Level B "managed mode". The recommended build.
- [`firmware/gateway/`](firmware/gateway/README.md) — the simpler **arduino-cli** build, capped
  at **3** concurrent bands by the precompiled BT controller. Fallback / no-IDF-toolchain path.

Both provision Wi-Fi + broker at runtime via a captive portal (no Wi-Fi credentials in the
repo). Rough sizing: ~`ceil(people / 6)` IDF gateways (≈5 for 30 people). See each firmware
README for the toolchain, build, flash, and provisioning steps, and
[Running with BLE→WiFi gateways](#running-with-blewifi-gateways-mqtt) for the broker setup and
the run-time wiring on the app side.

**Over-the-air updates.** After the initial USB flash, the whole IDF-gateway fleet updates
over Wi-Fi — bump `version.txt`, `idf.py build`, then push via the Ops "Update fleet" button
(or `POST /api/ota`); each gateway pulls, flashes, and auto-rolls-back if the new image can't
get healthy. Full steps in the [gateway OTA section](firmware/gateway-idf/README.md#ota-updates).

## Quick start

```bash
pip install -e .              # or: pip install -e '.[dev]' for tests

# Run against the built-in simulator (no hardware needed) — the demo path:
cuddle --source sim --scenario drift_into_sync --people 6

# Then open, in separate windows (typically on two monitors):
#   Show     — the visualization:      http://127.0.0.1:8770/
#   Ops      — technical + control:    http://127.0.0.1:8770/ops
#   Viz      — author the look:        http://127.0.0.1:8770/viz-settings
#   Puddle   — legacy force layout:    http://127.0.0.1:8770/puddle
```

The simulator is just the demo path. `--source` selects where samples come from:

```bash
cuddle --source ble                                   # real bands direct to the Mac (≤ ~7)
cuddle --source mqtt --broker 192.168.1.50:1883       # bands via an ESP32 gateway, over MQTT
cuddle --source mqtt --broker 192.168.1.50:1883 --orchestrate   # + Level B multi-gateway
cuddle --source replay --capture captures/session.jsonl         # replay a recording, no hardware
```

`ble` needs bands in range of the Mac; `mqtt` needs a running broker and at least one flashed
gateway — see [Running with BLE→WiFi gateways](#running-with-blewifi-gateways-mqtt) for the
broker config, provisioning, and a hardware-free smoke test; `--orchestrate` adds
app-authoritative placement across gateways (Level B — see the roadmap). Any real-band run can
add `--record captures/x.jsonl` to log raw samples for later hardware-free replay.

The server binds `127.0.0.1:8770` by default (an uncommon port, to avoid colliding
with other local services). Override the port and host per-run with
`cuddle --port 9001 --host 0.0.0.0`, or persistently in `config/app.yaml`
(`transport.port` / `transport.host`). The frontends discover the port automatically,
so no other change is needed.

On the **Ops** page: enroll each device (identify it by its live HR), press
**Baseline**, and once calibrated the person goes active and joins the puddle on the
**Show** page. Flip the **sync mode** and **scenario** selectors to see the effect.
(For quick demos the simulator shortens the baseline via `--baseline-scale`.)

Each person gets a unique visual identity — a **color × shape** glyph (8 colors ×
8 shapes = 64 combos, covering the 30-person target) plus a **seat number** — so
anyone can find their own dot ("you're #7, the teal triangle"). The glyph and seat
show on every Ops card; the Show renderers label each dot with its display name (label
colors are tunable in [Viz Settings](#the-four-views)), and `/puddle` cycles its own
label modes with **L**.

**Reusing bands across people** (for when you have fewer bands than people): on an
Ops person card, the **band ▸** menu hands that person's band to someone else or
**releases** it. A released person is *parked* — kept in the roster with their name,
identity, and baseline, just without a band — and their freed band returns to the
**Unassigned devices** list, where an **assign to parked…** menu can hand it to any
parked person. Reassigning to someone who's already baselined reactivates them
instantly (no re-baseline). So a handful of bands can rotate through many people
while every person's baseline is retained. To drop someone from the roster entirely,
the **Remove** button on their card (two-click confirm) retires them and returns their
band to the pool.

### Simulator scenarios (`--scenario`, sim only, switchable live in Ops)

| Scenario | What it shows |
|---|---|
| `independent` | Uncoupled hearts — synchrony stays near zero (baseline/control). |
| `drift_into_sync` | Coupling ramps up; the group gradually locks into one pulsing puddle. |
| `dropout` | A band roams out and rejoins — exercises the connection lifecycle. |
| `cliques` | Two sub-groups lock internally at different rates — the puddle forms separate clumps. |
| `anti_phase` | Two groups whose HR envelopes run in anti-phase — cross-group concordance goes strongly negative, so the two clumps are flung to opposite ends (the max-distance case). |
| `sync_then_break` | The group locks together, holds, then coupling releases and they drift apart. |
| `contagion` | Sync spreads from a seed outward — members join the locked group one at a time. |
| `pacer` | An external rhythm (guided co-breathing) everyone couples toward, pulling mixed rates to a common ~63 bpm. |

With real bands:

```bash
cuddle --source ble --record captures/session.jsonl
```

Recorded sessions replay without hardware:

```bash
cuddle --source replay --capture captures/session.jsonl
```

## Running with BLE→WiFi gateways (MQTT)

Three pieces: an **MQTT broker** on the Mac, one or more **flashed gateways** pointed at it, and
the app run with `--source mqtt`. The app does all `0x2A37` decoding — a gateway is a dumb bridge.

### 1. Broker (mosquitto)

```bash
brew install mosquitto
```

**Mosquitto 2.x will not accept gateway connections out of the box.** Started bare it logs
*"Starting in local only mode"* and binds `127.0.0.1` + `[::1]` only — fine for an all-on-one-Mac
test, invisible to an ESP32 on your LAN. Remote clients need a config file with an explicit
listener:

```conf
# broker.conf — POC on a trusted LAN only (no auth; see the trust model below)
listener 1883 0.0.0.0
allow_anonymous true
```

```bash
mosquitto -c broker.conf -v          # -v logs connects + every topic, worth it while debugging
```

Verify it's reachable off-box — `lsof -nP -i :1883` must show `*:1883`, not `127.0.0.1:1883`.

### 2. Gateways

Build and flash per the firmware README — [`firmware/gateway-idf/`](firmware/gateway-idf/README.md)
(recommended, 6 bands) or [`firmware/gateway/`](firmware/gateway/README.md) (3 bands). Wi-Fi and
broker are set at **runtime** via the captive portal, so no credentials live in the repo:

1. Join the open Wi-Fi **`Cuddle-Gateway-Setup`** (hold **BOOT**/GPIO0 at reset to reopen it on an
   already-provisioned gateway).
2. The config page opens automatically, or visit `http://192.168.4.1`.
3. Set your Wi-Fi network + password, and the **MQTT broker IP / port / gateway id**.

Two things to get right: the broker IP must be the Mac's **LAN** address (not `127.0.0.1` — that
would mean the ESP32 itself), and each **gateway id must be unique** since it's the `<gw>` in
every topic. `secrets.h` only seeds compile-time *defaults* for these; the portal overrides them
into NVS. Rough sizing: ~`ceil(people / 6)` IDF gateways.

### 3. App

```bash
cuddle --source mqtt --broker 192.168.1.50:1883                   # the broker's LAN address
cuddle --source mqtt --broker 192.168.1.50:1883 --orchestrate     # + Level B placement
```

Or persistently via `mqtt.broker` / `mqtt.port` in `config/app.yaml`. Add `--host 0.0.0.0` if
gateways must reach the app for [OTA](firmware/gateway-idf/README.md#ota-updates).

### Smoke test with no hardware

Because the gateway is a pure bridge, anything that can publish those bytes is indistinguishable
from real hardware — so with the broker and the app running (steps 1 and 3), you can exercise the
whole ingestion path with no ESP32 and no bands:

```bash
mosquitto_sub -t 'cuddle/#' -v                    # watch traffic (real or faked)

# One fake band beat: flags=0x10 (RR present), HR=0x48 (72 bpm), RR=0x0348 (840/1024 ≈ 0.82 s)
mosquitto_pub -t 'cuddle/gw-test/hr/AA:BB:CC:DD:EE:FF' -m "$(printf '\x10\x48\x48\x03')"

curl -s localhost:8770/api/state    # -> unassigned[0] = {device_id: AA:BB..., hr_bpm: 72}
```

The device then appears in the Ops **Unassigned devices** list, ready to enroll like any real band.

Topics are `cuddle/<gw>/hr/<dev>` (raw `0x2A37`), `cuddle/<gw>/status/<dev>`, and
`cuddle/<gw>/online` (retained LWT). `--orchestrate` adds `cuddle/<gw>/report`,
`cuddle/<gw>/cmd`, and `cuddle/control/{mode,online}` — see the
[managed mode section](firmware/gateway-idf/README.md#managed-mode-level-b--app-orchestrated-assignment).

**Trust model:** no broker auth, plain TCP, and an unauthenticated `/api/ota` that can flash the
whole fleet. Trusted LAN only — read the
[OTA trust model](firmware/gateway-idf/README.md#ota-updates) before exposing any of this.

## The four views

The backend serves one WebSocket stream (`/ws`) to four decoupled pages, meant to run
in parallel on different monitors:

| URL | View | What it is |
|---|---|---|
| `/` | **Show** | The live visualization — the projector output. No UI chrome. |
| `/viz-settings` | **Viz Settings** | The same renderer plus the full authoring panel. |
| `/ops` | **Ops** | Per-band technical status, enrollment, band reuse, scenario control. |
| `/puddle` | **Puddle** | The original force-directed layout, kept as a fallback. |

- **`/` Show** — one WebGL (PixiJS) canvas rendering the active **preset**. A preset is a
  *style* (a bundle of settings) bound to a *renderer* (an engine): five ship in
  `frontend/js/presets/registry.js` — **Node Chart 1** / **2**, **Chord Graph**,
  **Scatter Plot**, **Distribution** — plus any `frontend/presets/*.preset.json`. Dots are
  labelled with display names and pulse on each beat. Deliberately **chrome-free**: the
  editor UI is never appended to this document, so a stray keystroke on the show laptop
  can't summon a settings panel mid-event.
- **`/viz-settings` Viz Settings** — the control panel. Identical code to `/`, mounted with
  `chrome: true`, which adds an **Open Preset** button (top-left), the preset picker dialog
  behind it, and a live parameter panel down the left edge. Hotkeys: **`1`–`9`** jump to that
  position in the preset library (ignored while typing in a field, so a numeric parameter entry
  doesn't switch presets), **`Esc`** closes the dialog. Edits **auto-push to the server**, so
  this page drives the Show output live:

  ```
  /viz-settings ──POST /api/viz/active (every 300ms, diffed)──> server
                                                                 ├── config/viz_active.json
                                                                 └── broadcast /ws/viz ──> /
  ```

  The active config is **server-authoritative**. On load the panel adopts whatever `/` is
  currently showing (`GET /api/viz/active`) rather than imposing this browser's last-used
  preset, so a second laptop joins *in sync*; and a Show page that reconnects picks up the
  live look instead of its own stale state. The preset **library** is per-browser
  (`localStorage`); the **active config** is shared. Run the app with `--host 0.0.0.0` to
  author from another machine.
- **`/ops` Ops** — the technical status: per-band connection lifecycle, raw HR/RR trace
  + signal quality, the abstract per-person signal, and the synchrony heatmap. Cards
  sort **active sessions above disconnected ones**, and a person who (re)connects jumps
  to the top; each card has a **Remove** control (see band reuse above).
- **`/puddle` Puddle** — the pre-Pixi visualization, still served: a clean, full-screen
  "puddle." Each person is a glyph in a gentle **force-directed layout where distance
  encodes correlation over time**: strongly concordant hearts **clump** (and sub-groups
  that sync separately settle into **separate clusters**, each with its own soft glow),
  uncorrelated people sit **far apart**, and anti-correlated pairs are pushed **farthest
  of all**. Two guards keep it honest — an **EMA** so only *sustained* concordance gathers
  a cluster (not a one-frame spike), and the **flat-signal gate** above (a correlation
  counts only when both hearts actually vary). Motion is heavily damped and speed-capped,
  so dots ease into place rather than darting, and the constellation is sized to use the
  screen. The beat is an in-place pulse. When someone becomes active (enrolled or handed a
  band) a brief cue announces their glyph + seat ("Wren · #1 — sapphire circle"). Press
  **L** to cycle on-dot labels (none → initials → seat number).

**Authoring presets without the backend.** `/viz-settings` needs a running `cuddle` (it renders
live band data). To iterate on the *look* alone, `frontend/` also serves a standalone harness
against a frontend-side simulator — `cd frontend && python3 serve.py`, then
`http://127.0.0.1:8081/dev.html`. Both paths share the same `POST /api/preset` contract, so the
panel's **Save** writes committable `frontend/presets/*.preset.json` either way. See
[`frontend/VIZ_DATA_REFERENCE.md`](frontend/VIZ_DATA_REFERENCE.md) for the panel's controls,
the per-person fields each renderer reads, and the particle/event system.

## Architecture

```
sources/  →  hub/  →  processing/  →  transport/  →  frontend/
(BLE|sim|    (registry (resample,      (FastAPI      (show + ops
 replay|      enrollment  quality,       /ws + REST)   pages)
 mqtt gw      ingest)     baseline,
 behind a                 abstract,
 Protocol)                synchrony)
```

The `SampleSource` Protocol (`src/cuddle/sources/base.py`) is the one swap point:
direct BLE, the simulator/replay, and the BLE→WiFi gateway (MQTT) source all feed the same
normalized per-person sample stream — nothing downstream knows the origin. Bands are expected
to roam in and out of range — each device
auto-reconnects with backoff, and sessions are keyed by a stable `person_id` so a
drop-and-rejoin preserves history and matrix position.

## Development

```bash
pytest            # BLE parser, synchrony, baseline, reconnect, enrollment, sim scenarios,
                  # artifact correction, MQTT gateway source, and Level B orchestration
```

Key modules:

- `sources/ble_parser.py` — pure `0x2A37` decoder (golden-tested).
- `sources/sim_source.py` — Kuramoto-coupled cardiac-oscillator simulator + replay,
  with a shared-arousal envelope so coupled HR *levels* co-move (and, in `anti_phase`,
  counter-move) — the signal cross-person concordance actually reads.
- `hub/enrollment.py` — enroll → baseline → active, plus band reuse (assign / park /
  reassign / remove); binding stays consistent across the registry and the source so a
  reassigned or removed band never keeps routing to its old owner.
- `processing/baseline.py` — the rest-capture calibration that gates a person to active.
- `processing/artifact.py` — beat-level spike correction (Hampel + Malik floor +
  missed/extra-beat repair) feeding HRV/synchrony; surgical so it doesn't flatten the
  real dynamics the coherence metric reads. Config under `artifact:` in `app.yaml`.
- `processing/synchrony.py` — concordance + PLV + group cohesion.
- `frontend/js/show/puddle.js` — the `/puddle` force-directed layout: concordance→distance
  mapping, flat-signal gate, and temporal smoothing (tunables at the top of `FORCE`).
- `frontend/js/show/pixiApp.js` — the `/` and `/viz-settings` Pixi bootstrap: preset library,
  the `chrome` flag that gates the whole authoring UI, and `applyConfig`/`getState`.
- `frontend/js/presets/registry.js` — the style-vs-renderer split; add a style to `PRESETS`
  or register a whole new engine in `RENDERERS`.
- `transport/viz_config.py` — the server-held active viz config broadcast on `/ws/viz`.

## Roadmap (Phase 2)

BLE→WiFi gateway / MQTT ingestion, ~30-person scale, and session persistence beyond
JSONL captures.

**Level B orchestration** (app-orchestrated gateway assignment) has landed on the Phase-2
branch: `cuddle --source mqtt --orchestrate` gives the app full authority over which gateway
holds which band (stability-first — connected bands aren't moved except a bounded
unserved-band rebalance — with auto-revert to opportunistic per-gateway assignment if the
orchestrator dies). Validated end-to-end against a mock multi-gateway harness; on-hardware
validation of the firmware's managed mode is still pending. See
[`docs/superpowers/roadmap.md`](docs/superpowers/roadmap.md) for details.

## License

MIT — see [LICENSE](LICENSE).
