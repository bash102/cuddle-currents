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

Heart-rate synchrony is a measurable marker of real-world social engagement, strongest
in close proximity and stronger with affiliation (PNAS Nexus 2026; Frontiers in Human
Neuroscience 2022). We quantify it two ways:

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

## Hardware

Coospo **HW706** (BT 4.0) and **HW9** (BT 5.0) both expose the standard BLE Heart Rate
Service (`0x180D` / `0x2A37`) including RR intervals — no proprietary protocol. macOS
CoreBluetooth holds only ~7–10 peripherals at once, which is why the full system will
use gateways; Phase 1 stays within that limit.

## Quick start

```bash
pip install -e .              # or: pip install -e '.[dev]' for tests

# Run against the built-in simulator (no hardware needed) — the demo path:
cuddle --source sim --scenario drift_into_sync --people 6

# Then open, in two separate windows:
#   Show view (clean puddle):   http://127.0.0.1:8770/
#   Ops view  (technical):      http://127.0.0.1:8770/ops
```

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
show on every Ops card; on the Show view, press **L** to reveal initials or numbers.

**Reusing bands across people** (for when you have fewer bands than people): on an
Ops person card, the **band ▸** menu hands that person's band to someone else or
**releases** it. A released person is *parked* — kept in the roster with their name,
identity, and baseline, just without a band — and their freed band returns to the
**Unassigned devices** list, where an **assign to parked…** menu can hand it to any
parked person. Reassigning to someone who's already baselined reactivates them
instantly (no re-baseline). So a handful of bands can rotate through many people
while every person's baseline is retained.

### Simulator scenarios (`--scenario`, sim only, switchable live in Ops)

| Scenario | What it shows |
|---|---|
| `independent` | Uncoupled hearts — synchrony stays near zero (baseline/control). |
| `drift_into_sync` | Coupling ramps up; the group gradually locks into one pulsing puddle. |
| `dropout` | A band roams out and rejoins — exercises the connection lifecycle. |
| `cliques` | Two sub-groups lock internally at different rates — the puddle forms separate clumps. |
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

## Two independent frontends

The backend serves one WebSocket stream (`/ws`) to two decoupled pages, meant to run
in parallel on different monitors:

- **`/` Show** — the final visualization: a clean, full-screen "puddle." Each person
  is a glyph at a fixed seat on the ring; the beat is an in-place pulse, and as group
  cohesion rises everyone eases inward and gathers (spreading back out when out of
  sync). When someone becomes active (enrolled or handed a band) a brief cue announces
  their glyph + seat ("Wren · #1 — sapphire circle"). Press **L** to cycle on-dot
  labels (none → initials → seat number).
- **`/ops` Ops** — the technical status: per-band connection lifecycle, raw HR/RR
  trace + signal quality, the abstract per-person signal, and the synchrony heatmap.

## Architecture

```
sources/  →  hub/  →  processing/  →  transport/  →  frontend/
(BLE|sim|    (registry (resample,      (FastAPI      (show + ops
 replay)      enrollment  quality,       /ws + REST)   pages)
 behind a     ingest)     baseline,
 Protocol)                abstract,
                          synchrony)
```

The `SampleSource` Protocol (`src/cuddle/sources/base.py`) is the one swap point:
direct BLE now, a gateway/MQTT source later, both feeding the same normalized
per-person sample stream. Bands are expected to roam in and out of range — each device
auto-reconnects with backoff, and sessions are keyed by a stable `person_id` so a
drop-and-rejoin preserves history and matrix position.

## Development

```bash
pytest            # 34 tests: BLE parser, synchrony, baseline, reconnect, enrollment
```

Key modules:

- `sources/ble_parser.py` — pure `0x2A37` decoder (golden-tested).
- `sources/sim_source.py` — Kuramoto-coupled cardiac-oscillator simulator + replay.
- `hub/enrollment.py`, `processing/baseline.py` — enroll → baseline → active flow.
- `processing/synchrony.py` — concordance + PLV + group cohesion.

## Roadmap (Phase 2)

BLE→WiFi gateway / MQTT ingestion, ~30-person scale, and session persistence beyond
JSONL captures.

## License

MIT — see [LICENSE](LICENSE).
