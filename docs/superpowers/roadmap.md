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
