# Cuddle Currents — Phase 2 Roadmap

A living document for follow-on work beyond Phase 1 (direct BLE + simulator + visualizer).
Each milestone below is a candidate for its own brainstorm → spec → plan cycle; this file
sequences them and records what's deferred so nothing gets lost between cycles.

Update this doc as milestones are specced, started, or completed.

## Status

| Item | State |
|---|---|
| Phase 1 — direct BLE, simulator, two frontends, synchrony viz | done (on `main`) |
| Connection-state hardening (reachable `disconnected`, eviction, unassigned staleness) | done — commit `08b305c` |
| **ESP32 gateway PoC — design** | approved — [`specs/2026-07-19-esp32-gateway-design.md`](specs/2026-07-19-esp32-gateway-design.md) |
| ESP32 gateway PoC — implementation | next (plan pending) |

## The PoC (current slice)

Scope and decisions are in the design spec above. In short: MQTT contract (raw `0x2A37` HR,
explicit per-device status, gateway LWT), band-keyed identity, opportunistic capacity with a
per-gateway cap, and a **mock-gateway-first** build order that freezes the contract before
firmware. Ends when a real ESP32 bridges a few bands to the running app.

## Follow-on milestones (post-PoC, in order)

### 1. Firmware hardening
Robust BLE + MQTT reconnect/backoff, NVS-stored gateway config, retained-state correctness,
watchdog. Turns the PoC firmware into something that survives a real session.

### 2. Multi-gateway deployment
Run 2+ gateways with overlapping coverage. Verify in practice: band **handoff/roaming**
across gateways (identity is the band, so `person_id` continuity should hold), and that
opportunistic capacity self-balances.

### 3. Level B — app-orchestrated assignment  *(done — app side + firmware code; on-hardware validation pending — [`specs/2026-07-20-level-b-orchestration-design.md`](specs/2026-07-20-level-b-orchestration-design.md))*
The PoC shipped level A (opportunistic + per-gateway cap). Level B gives the app a global view
and full authority (stability-first placement, auto-revert on orchestrator death). Shipped:
- **`hub/orchestration/`** (`world.py` world-model + coverage memory, `plan.py` pure
  stability-first planner, `orchestrator.py` async MQTT service) — enabled with
  `cuddle --source mqtt --orchestrate` (or `orchestrator.enabled` in `app.yaml`).
- **Additive MQTT contract**: `cuddle/<gw>/report` (retained: capacity/mode/connected/seen),
  `cuddle/<gw>/cmd` (connect/release), `cuddle/control/mode` (managed|opportunistic,
  retained), `cuddle/control/online` (retained, orchestrator LWT). Level A topics
  (`hr`/`status`/`online`) are unchanged.
- **Stability-first placement** — connected bands aren't moved except a bounded,
  cooldown-guarded unserved-band rebalance; enrolled bands (assigned/baselining/active) are
  pinned (force-connected immediately, protected from rebalance). Auto-reverts every gateway
  to level A (opportunistic) if the orchestrator dies.
- **Ops UI + REST** — `StateFrame` gained `gateways[]`/`unserved[]`; Ops gained a gateway
  roster, an unserved-band callout, a mode toggle, and manual override
  (`/api/orchestrator/mode`, `/connect`, `/release`, `/pin`).
- **Firmware** (`firmware/gateway-idf`) — managed mode (report + cmd + transition-based
  auto-revert), boot-default opportunistic. See that directory's README.

**Results (Tasks 10-11, mock multi-gateway e2e)**: all servable bands connect; the
unserved-band case (a band in range of only full gateways) gets served after a rebalance with
**no cmd thrash** (fixed via an eviction cooldown — see `plan.py`); enrollment force-connect
pins+connects a chosen band; killing the app flips every gateway back to opportunistic
(auto-revert), confirmed via the mock harness. **Firmware on-hardware validation is still
pending** — a separate hardware step (flash real ESP32 gateways, run against a live
orchestrator) has not yet been done; do not treat firmware managed mode as hardware-validated.

**Known limitation**: a band connected stably for longer than `coverage_ttl` (60s) won't be
auto-rebalanced — its cross-gateway coverage memory (RSSI at other gateways) ages out because
a connected BLE band stops re-advertising, so the orchestrator can't refresh it. Rather than
rebalance on stale RSSI, the orchestrator conservatively surfaces the *other* unserved band as
unserved instead of moving the long-connected one. Operator can manually place via Ops
(`/api/orchestrator/connect` or `pin`).

Additive to the PoC contract (new topics), not a rewrite.

#### Level B — real-hardware validation checklist (highest-value next step)

Everything above is **mock-validated**; the single-ESP32 hardware test only exercised the
*communication* (report / mode switch / cmd received / auto-revert) with **no bands**. One
2-gateway session with real bands flips most unknowns into knowns. Setup: 2 IDF gateways with
**distinct gateway ids** (each provisioned via the portal — they collide on MQTT topics if both
default to `esp32-01`), the broker, `cuddle --source mqtt --orchestrate`, the **Ops page open in
a browser**, and a handful of bands.

- [ ] Both gateways appear in the Ops roster with correct `capacity`/`mode`; boot opportunistic,
      flip to managed when the orchestrator starts.
- [ ] Bands get placed via `cmd` (real `connect`), HR flows, and the roster shows each band on
      the right gateway. Confirm the LED goes green→brighter as bands land (teal in managed).
- [ ] **Real RSSI noise**: watch initial placement for flapping / odd strongest-gateway picks
      (real RSSI swings ±10–20 dBm; the mock used fixed values).
- [ ] Manual override in the browser: force-connect a `seen` band to a chosen gateway;
      force-release; pin/unpin. Confirm Release actually sticks (the pin bug fixed in final review).
- [ ] Enrollment: assign a band → it's pinned → force-connected → baselines → active, without a
      rebalance ever interrupting it.
- [ ] **Roaming/handoff**: carry a band out of one gateway's range → it drops → re-placed on
      another gateway with `person_id` continuity (history/matrix position intact).
- [ ] **Unserved-band rebalance** with a real coverage-overlap topology (mirror the mock's
      band-only-reachable-via-a-full-gateway case): confirm it's served via one clean eviction,
      no `cmd` thrash.
- [ ] **60s coverage limitation**: let bands sit connected >1 min, then introduce an unserved
      band — observe whether it's rebalanced in or (expected) surfaced as unserved. Decide if the
      conservative behavior is acceptable or needs the aggressive-rebalance change.
- [ ] **App-death auto-revert**: kill the app → both gateways revert to opportunistic within
      ~15s (LED yellow/green per Level A) → bands stay served. Restart → they return to managed.
- [ ] Load/robustness: with many `seen` advertisers, confirm `report` still publishes (buffer
      sizing) and no MQTT churn; check the LED never sticks on a stale state.
- [ ] Record results here + in the design spec; promote confirmed items out of "pending".

### 4. Broker security
TLS, credentials, and ACLs. The PoC assumes a trusted local network; real deployments need
auth on the broker.

### 5. Gateway provisioning / OTA
Flashing and configuring many gateways; over-the-air firmware updates.

**Runtime Wi-Fi / config provisioning.**
- **Hosted captive portal — DONE** (firmware commit): on boot the gateway joins saved
  Wi-Fi, or (no creds / BOOT held) raises a SoftAP + web form (`Cuddle-Gateway-Setup` ->
  `192.168.4.1`) to set Wi-Fi + broker/port/gateway-id from a phone; persisted to NVS
  (WiFiManager + Preferences). `secrets.h` now only seeds compile-time defaults — no
  Wi-Fi password in the repo.
- **Serial config** (future, optional) — a USB-serial command interface to set the same
  NVS config headless, for bulk provisioning without a phone.
- **Remaining**: a way to re-open the portal without physical BOOT access (e.g. an MQTT
  command topic or a web endpoint on the running device) for fleet reconfiguration.

### 6. 30-person scale validation
The actual target. Validate processing/synchrony throughput, the Show puddle at ~30 dots,
and MQTT message volume at scale. May surface tuning work in `processing/` and the frontend.

**Measured (isolated 5-gateway mock fleet + 30-person sim capture, no hardware):** the
gateway/MQTT/orchestration path holds fine — 30/30 bands placed across 5 managed gateways,
~33 HR msg/s ingested. **But the app does NOT sustain the 10 Hz frame loop at 30 people:**
`synchrony.compute()` alone is ~149 ms/frame at N=30 (9 ms at N=6, 42 ms at N=15 — clean
O(N²)), so the fixed-cadence frame loop drops to ~3 Hz. Root cause: 435 pairs each running a
±8-sample lag sweep (`sync_max_lag`), recomputed EVERY frame. Fix directions (cheapest first):
(1) **decouple synchrony from the visual frame** — recompute it at ~1-2 Hz, not 10 Hz (it's
slow-changing; the puddle already smooths); (2) drop/adapt `sync_max_lag` for large N; (3)
vectorize the pairwise CCC (numpy batch vs. the Python 435-pair loop).

**DONE — synchrony vectorized** (masked-matmul CCC/PLV, max-over-lags): pairwise CCC 125.8 ms -> 0.93 ms at N=30 (~135x), numerically identical to the loop (max diff ~1e-15, regression-tested). End-to-end frame rate 3.0 -> 5.5 Hz, CPU 53% -> 40%. **Then** removed the remaining per-person duplication (the 30 s smoothed grid was computed for both `hr_var` and synchrony; RMSSD for both the readout and its delta) by deriving each person's grid + RMSSD once per frame and sharing them (build_frame -> synchrony via `hr_grids`; bit-identical output) — per-frame per-person+synchrony work 94 -> 55 ms. **And** made the frame loop rate-compensating (it slept a *full* period AFTER each build, so the real rate was build+period). **Result: sustained 10.0 Hz at 30 people, CPU 53% -> 28.6%** (measured, isolated 5-gateway mock fleet). M6 processing throughput: met.

**Gateway BLE ceiling: 3 on the Arduino toolchain → 6 validated on the ESP-IDF port.**
Arduino hardware test with 6 bands: the gateway saw all 6 but only 3 subscribed; the 4th+
got repeated `connect FAILED`. The limit was the precompiled BT controller's concurrent-ACL
cap (3) — `CONFIG_BT_NIMBLE_MAX_CONNECTIONS` raises only the NimBLE host table, not the
controller. **The ESP-IDF port is now done** (`firmware/gateway-idf/`): building the same
firmware under ESP-IDF v5.5 (arduino-esp32 3.3.10 as a component + esp-nimble-cpp 2.x)
recompiles both host and controller from source, so `sdkconfig` governs the ceiling
(`CONFIG_BT_NIMBLE_MAX_CONNECTIONS=6` + `CONFIG_BT_CTRL_BLE_MAX_ACT=7`). **Hardware-validated:
6 bands connected and subscribed concurrently, zero `connect FAILED`.**
Implications for 30-person scale:
- **Now:** plan `ceil(people / 6)` gateways (≈5 for 30) with the IDF firmware. Hard max is 9;
  ≤6 is the recommended planning number for RAM headroom on the S3.
- Fallback: the arduino-cli build (`firmware/gateway/`) still ships at 3/gateway if the IDF
  toolchain isn't set up.

### 7. Session persistence beyond JSONL captures
Existing README roadmap item — durable session storage/history beyond flat capture files.

## Known deferred edge cases

- **Unserved band** (level A): a band in range of only at-capacity gateways is invisible to
  the app. Fixed by level B (or more gateways / better coverage).
- **Handoff churn**: without stickiness, a band on the edge of two gateways could flap
  between them. Addressed by the level B assignment algorithm.

## Backlog — Ops UI & firmware polish

Small, independent polish items (no dependencies; pick up any time):

- **Gateway naming: assign a human-friendly name to each gateway — DONE** (client-side).
  An operator can click a gateway's name (or the ✎ button) on the Ops roster to set a friendly
  alias ("Living Room") edited in place; it's stored in `localStorage` keyed by gateway id, so
  it's Ops-UI-only — never sent to the backend or the gateway, and the canonical id stays
  authoritative (shown on hover). Chose the app-side path over NVS-in-`report` for zero
  wire/firmware cost. *Remaining (optional):* per-browser only — a shared/persisted mapping
  would need a StateFrame field + REST endpoint.
- **Seen-list name resolution: case-insensitive — DONE.** `SeenBand` already carried
  `person_id` (commit `4ae6df5`); `person_for_device` now matches addresses case-insensitively
  so an enrolled band resolves to its person in the seen list even when the MAC casing differs
  between sources (firmware NimBLE `toString()` is lowercase).
- **Ops enroll: confirm on Enter — DONE.** Pressing Enter in the enroll name field confirms
  the name→band enrollment (same as clicking Enroll).
- **Ops HR charts: labeled axes — DONE.** Per-person HR trace now has a y-axis (bpm min/max)
  and an x-axis (time →, oldest beat left → newest right).
- **Ops: remove the orbiting circle — DONE.** Dropped the per-person phase dial (the dot
  orbiting a circle) from the Ops cards.
- **Firmware: RGB status LED — DONE.** Onboard NeoPixel (GPIO48, `rgbLedWrite`, intentionally
  dim — channels capped at 24) shows link/mode/load: yellow (Wi-Fi connecting) · blue (portal) ·
  orange (MQTT down) · green (online/opportunistic, brighter with band count) · teal (managed).
  `updateLed()` runs at the top of `loop()`, main task only.
- **Firmware: fleet bring-up — DONE.** Gateway id auto-appends a per-chip MAC suffix (one image
  → unique per board, e.g. `esp32-01-a172e0`), and optional compile-time `WIFI_SSID`/`WIFI_PASS`
  in `secrets.h` let a freshly-flashed gateway auto-join with no captive portal.
- **Firmware: OTA updates.** — **DONE** (Tasks 5–7). Fleet-wide over-the-air updates via
  MQTT-triggered pull: the app posts a command on `cuddle/control/ota` with an image URL;
  gateways fetch and self-update in dual-slot OTA with auto-rollback if the new image fails
  to reach MQTT within ~60s. See `firmware/gateway-idf/README.md` for usage. Follow-up items
  (not blocking): **staggered/rolling rollout** (limit concurrent updates to avoid overload),
  **per-gateway targeting** (selective fleet updates), and **HTTPS + signed images** (trusted
  LAN only; TLS/signing deferred for Phase 2).
