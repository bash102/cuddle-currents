# Level B — App-Orchestrated Gateway Assignment — Design

**Status:** approved design, pre-implementation
**Date:** 2026-07-20
**Scope:** Phase 2, the scale layer — give the app a global view of all gateways and full
authority over which gateway connects which band, so a multi-gateway deployment serves ~30
people reliably. Additive to the frozen Level A contract; nothing downstream of the source
changes shape.

## 1. Context & goal

The PoC (see `2026-07-19-esp32-gateway-design.md`) shipped **Level A**: each gateway
opportunistically connects to any advertising `0x180D` band up to `max_connections`, with no
coordination. The ESP-IDF port raised the per-gateway ceiling from 3 to 6, so ~5 gateways
cover 30 people — but Level A has a hole: a band in range of *only* full gateways is
**unserved and invisible** to the app, and there is no way to balance load or place a band on
its strongest gateway.

**Level B** makes the app the sole authority. Gateways report what they see and obey
connect/release commands; the app decides placement. Goal: every enrollable band gets served
(or is explicitly surfaced as unservable), placement is deterministic, and — critically —
enrollment/baselining can connect a chosen band *immediately*.

**Success criteria:** with N gateways and overlapping coverage, the app connects every
in-range band up to total capacity; an operator can force-connect a band for enrollment and
watch it baseline without interruption; when all in-range gateways are full, the app frees a
slot when it safely can, else surfaces the band as unserved; and if the orchestrator dies,
gateways keep people connected by reverting to Level A.

## 2. Decisions (from brainstorming)

| Decision | Choice | Rationale |
|---|---|---|
| Slice ambition | **Run — full orchestration** | discovery + cmd + assignment algorithm + Ops roster + manual override, in one layer |
| Authority | **Full app authority**, with **auto-revert** safety | gateways only report + obey `cmd` when managed; if the orchestrator dies they fall back to Level A so data keeps flowing |
| Assignment policy | **Stability-first** | each band is a continuous HR stream; moving it drops beats. Coverage is a hard constraint, continuity beats perfect balance |
| Orchestrator structure | **Pure `plan()` core + event-trigger + reconcile** (approach C) | testable pure function; responsive on events, self-correcting on a slow tick |
| Realization | mock-gateways-first | validate the whole loop with zero hardware before the firmware pass |

**Governing physical fact (shapes everything):** a BLE band **stops advertising once
connected**. So a connected band is invisible to every *other* gateway — the app cannot
measure whether a connected band would get better RSSI elsewhere without disconnecting it.
Therefore RSSI/load optimization is a **connect-time decision** (the only moment of full
cross-gateway information); after connection, placement is effectively committed. This is why
stability-first is not merely preferred but essentially forced.

## 3. Architecture

```
[bands] --BLE--> [gateway (managed)] --report--> [broker] --> [Orchestrator (app)] --cmd--> [gateway]
                    reports seen+connected                       world model + plan()          connect/release
                                                                 pinned<-enrollment
                                                                 StateFrame.gateways -> Ops
```

One new app module — `hub/orchestrator.py` — sharing the app's aiomqtt client. It consumes
per-gateway `report`s, maintains a world model, runs a pure `plan()`, and publishes `cmd`s.
`GatewayMqttSource` is unchanged (it still turns `hr`/`status` into `NormalizedSample`s); the
orchestrator is purely additive alongside it.

## 4. MQTT contract additions

All additive; Level A topics (`hr/`, `status/`, `online/`) are unchanged. Prefix `cuddle/`.

| Topic | Payload | Retained | Meaning |
|---|---|---|---|
| `cuddle/<gw>/report` | JSON (below) | **yes** | gateway's complete view; the orchestrator's world-model feed. On-change + ~2 s heartbeat |
| `cuddle/<gw>/cmd` | `{"action":"connect"\|"release","dev":"<addr>"}` | no (QoS 1) | app → gateway. `connect` valid only for a band in that gateway's current `seen` |
| `cuddle/control/mode` | `"managed"` \| `"opportunistic"` | **yes** | app → all gateways; global authority switch |
| `cuddle/control/online` | `"1"` / `"0"` | **yes** | orchestrator liveness; `"0"` is its MQTT LWT |

`report` payload:
```json
{
  "capacity": 6,
  "mode": "managed",
  "connected": [{"dev": "c0:8c:43:ea:94:6e", "rssi": -60}],
  "seen":      [{"dev": "d0:f8:5c:28:16:4c", "rssi": -72}],
  "ts": 1737412345678
}
```
`seen` = bands advertising in range that this gateway is **not** connected to (a connected
band, per §2, is not advertising and appears in no gateway's `seen`). The gateway does not
send the address *type* on the wire — it keeps `{dev, addr_type, rssi, last_seen}` in its own
scan cache and resolves the type locally when it receives a `connect`.

## 5. World model & coverage memory

The orchestrator maintains, from `report`s:

- **gateways**: `{gw: {capacity, mode, online, connected:set[dev], last_report_ts}}`
- **holder index**: `dev -> gw` for connected bands
- **advertising set**: bands currently in some gateway's `seen` (unconnected)
- **coverage memory**: `{dev: {gw: (rssi, ts)}}` — every time a band appears in a gateway's
  `seen`, record the RSSI and timestamp. This accumulates cross-gateway reachability *while a
  band is advertising* and is the only (necessarily stale) hint about a connected band's
  alternatives later. Entries age out (`coverage_ttl`, default 60 s).

## 6. The `plan()` core

Pure function: `plan(world, current_assignment, pinned) -> list[Cmd]`. No I/O, no clock reads
passed in as args (timestamps come from the world snapshot). Priority order:

1. **Pinned placement (highest).** For each pinned band (enrollment: `ASSIGNED`/
   `BASELINING`/`ACTIVE`) not yet connected: place immediately on the best gateway that sees
   it (RSSI, then least-loaded). Fires without debounce. If no gateway sees it → mark
   `waiting-to-advertise` (surfaced, not silently stalled). If all seeing gateways are full →
   trigger the §step-3 rebalance on its behalf.
2. **Connect-time placement.** For each advertising, unconnected, unpinned band: candidates =
   gateways seeing it now with a free slot (managed + online). Pick strongest RSSI, tie-break
   least-loaded. Emit **one** `connect` to the winner; mark the band **pending** (`gw`,
   deadline `pending_ttl` ~8 s) so it is not re-issued until the next `report` confirms or the
   deadline passes — prevents two gateways racing for one band.
3. **Stability.** Connected bands are never moved under normal operation. Mobility is handled
   by natural drop → re-advertise → step 2/1 re-place. No active RSSI monitoring of connected
   bands (impossible per §2).
4. **Unserved-band resolution (reconcile tick only; disruptive).** A band advertising while
   **all** gateways that see it are full. Using coverage memory, find a connected,
   **unpinned** band `Y` on one of those full gateways that is *also* reachable (per coverage
   memory, non-stale) by another online gateway with a free slot. If found: `release Y` (it
   re-advertises → step 2 hands it to the other gateway) and the freed slot takes the unserved
   band. Bounded to **one** such move per tick per gateway, gated by hysteresis
   (`rebalance_cooldown`) so it cannot thrash. If no safe `Y` exists → band stays unserved,
   surfaced in Ops and logged (never a silent drop).

**Pinned invariants:** a pinned band is never chosen as the `Y` to release; and a pinned band
is the *reason* a rebalance may free a slot. So a baselining calibration is never interrupted
by orchestration.

## 7. Enrollment integration

No new wire topic — enrollment and the orchestrator are both in-process:

- When a person is `ASSIGNED` a band, that band enters the **pinned set** → orchestrator
  force-connects it (step 1) → `BASELINING` runs on the resulting HR stream → `CALIBRATED` →
  `ACTIVE`. Bands in `ASSIGNED`/`BASELINING`/`ACTIVE` are all pinned.
- On retire/release/park, unpin (and `release` if the intent is to disconnect).
- The pinned set is read from `SessionStore` each `plan()` call; the orchestrator does not own
  enrollment state, it observes it.

## 8. Gateway firmware changes

`firmware/gateway-idf/main/main.cpp` gains a **mode** (the arduino build can follow later):

- Subscribe `cuddle/<gw>/cmd`, `cuddle/control/mode`, `cuddle/control/online`.
- **Scan cache**: keep `{dev, addr_type, rssi, last_seen}` for advertising bands, expiring
  entries not seen in `seen_ttl` (~10 s). This cache is the `seen` list.
- **Publish `report`** (connected + seen + capacity + mode) on change + ~2 s heartbeat, in
  both modes.
- **`managed`**: scan (to populate `seen`) but **do not auto-connect**; connect/release only
  on `cmd`. `connect dev` resolves addr+type from the scan cache (preserves the
  random-address fix) and reuses `connectTo()`; `release dev` disconnects that client.
- **`opportunistic`**: today's Level A behavior (auto-connect up to `MAX_CONNECTIONS`), still
  publishing `report`.
- **Boot default**: `opportunistic` (safe = current behavior) until a retained `control/mode`
  says otherwise.
- **Auto-revert**: if `mode=managed` and `control/online` is `0`/absent for
  `orchestrator_grace` (~15 s), revert locally to opportunistic; snap back to managed when the
  orchestrator returns.

## 9. Orchestrator lifecycle (app)

`hub/orchestrator.py`, sharing the app's aiomqtt client:

- Subscribe `cuddle/+/report`; update world model + coverage memory on each.
- Run `plan()` on **debounced report events (~500 ms)** and a **~5 s reconcile tick**; diff
  results against outstanding assignments + pending; publish `connect`/`release` `cmd`s.
- Publish retained `control/mode=managed` and `control/online=1` (LWT `0`) while alive.
- Read the **pinned set** from `SessionStore` per plan.
- Surface state to frontends via **new `StateFrame` fields** (the only backend↔frontend
  contract): `gateways: [{id, online, mode, capacity, connected:[{dev, person_id?, rssi}],
  seen:[{dev, rssi}]}]` and `unserved: [{dev, rssi, reason}]`.
- Config under a new `orchestrator:` section in `app.yaml` (all tunables above:
  `report_debounce`, `reconcile_interval`, `pending_ttl`, `coverage_ttl`,
  `rebalance_cooldown`, `orchestrator_grace`, `seen_ttl`) with the defaults noted here.
- A CLI/config switch enables Level B (`--orchestrate` / `orchestrator.enabled`); default off
  keeps Level A behavior so existing single-gateway runs are unaffected.

## 10. Ops UI (additions to the existing Ops page)

Driven by the new `StateFrame` fields + new REST endpoints for manual actions:

- **Gateway roster** — one card per gateway: id, online dot, mode, capacity used (`4/6`),
  connected bands (dev → person name if enrolled, + RSSI), and `seen`-but-unconnected bands.
- **Unserved bands** — a prominent callout (these are people not getting data).
- **Manual override** — force-connect a `seen` band to a chosen gateway, force-release,
  pin/unpin. REST → orchestrator (in-process) → `cmd`.
- **Mode toggle** — global managed ⇄ opportunistic (sets `control/mode`).
- Ties into enrollment: assigning a band shows where it landed + baselining progress.

REST additions on `ws_server.py`: `/api/orchestrator/mode` (POST managed|opportunistic),
`/api/orchestrator/connect` (POST `{dev, gw}`), `/api/orchestrator/release` (POST `{dev}`),
`/api/orchestrator/pin` (POST `{dev, pinned}`).

## 11. Testing

- **Pure `plan()` unit tests** (the core net): hand-built worlds → assert exact `Cmd` lists.
  Cover connect-time RSSI/load placement, the pending-race guard (no double-assign),
  stability (assert *no* moves for a settled world), unserved-band 1-step rebalance via
  coverage memory (incl. the no-safe-`Y` → surfaced case), pinned protection + priority, and
  coverage-memory staleness expiry.
- **Multi-mock-gateway harness**: extend `tools/mock_gateway.py` to run N mock gateways, each
  with configurable *coverage* (which bands it sees + RSSI), obeying `cmd`, publishing
  `report`, honoring `control/mode` + auto-revert. Validates the whole loop (app +
  orchestrator + N gateways + broker) with **zero hardware** — roaming, unserved, enrollment
  force-connect, orchestrator-death auto-revert.
- **Firmware**: one on-hardware pass — managed mode, `cmd` connect/release, `report` contents,
  and auto-revert with the real gateway + bands.

## 12. Build order

1. **`plan()` core + world model + coverage memory** (pure, fully unit-tested) — no I/O.
2. **Orchestrator module**: MQTT wiring (subscribe `report`, publish `cmd`/`control`),
   debounce + reconcile loop, pending tracking, pinned from `SessionStore`. Unit-test the
   glue with injected messages (handler style, no broker).
3. **`StateFrame` fields + `frame.build_frame`** additions; **Ops UI** roster/unserved/
   override/mode; REST endpoints.
4. **Multi-mock-gateway harness**; end-to-end validation against mosquitto (roaming,
   unserved, enrollment, auto-revert).
5. **Firmware managed mode**: scan cache, `report`, `cmd` handling, `control/mode` +
   `control/online` subscription, auto-revert. Build + on-hardware validation.
6. **Config + CLI** (`orchestrator:` section, `--orchestrate`); docs + roadmap update.

## 13. Out of scope (this slice)

- Multi-orchestrator / HA (single app instance owns orchestration).
- Predictive placement or learned coverage maps (coverage memory is a simple aged cache).
- Cross-gateway time-sync of HR samples (identity is the band; `t_recv` stamped on receipt as
  today).
- Broker security (TLS/auth) — tracked separately in the roadmap.
