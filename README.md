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

## Two independent frontends

The backend serves one WebSocket stream (`/ws`) to two decoupled pages, meant to run
in parallel on different monitors:

- **`/` Show** — the final visualization: a clean, full-screen "puddle." Each person is
  a glyph in a gentle **force-directed layout where distance encodes correlation over
  time**: strongly concordant hearts **clump** (and sub-groups that sync separately
  settle into **separate clusters**, each with its own soft glow), uncorrelated people
  sit **far apart**, and anti-correlated pairs are pushed **farthest of all**. Two
  guards keep it honest — an **EMA** so only *sustained* concordance gathers a cluster
  (not a one-frame spike), and the **flat-signal gate** above (a correlation counts only
  when both hearts actually vary). Motion is heavily damped and speed-capped, so dots
  ease into place rather than darting, and the constellation is sized to use the screen.
  The beat is an in-place pulse. When someone becomes active (enrolled or handed a band)
  a brief cue announces their glyph + seat ("Wren · #1 — sapphire circle"). Press **L**
  to cycle on-dot labels (none → initials → seat number).
- **`/ops` Ops** — the technical status: per-band connection lifecycle, raw HR/RR trace
  + signal quality, the abstract per-person signal, and the synchrony heatmap. Cards
  sort **active sessions above disconnected ones**, and a person who (re)connects jumps
  to the top; each card has a **Remove** control (see band reuse above).

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
pytest            # 56 tests: BLE parser, synchrony, baseline, reconnect, enrollment,
                  #           sim scenarios, artifact correction
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
- `frontend/js/show/puddle.js` — the force-directed puddle: concordance→distance
  mapping, flat-signal gate, and temporal smoothing (tunables at the top of `FORCE`).

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
