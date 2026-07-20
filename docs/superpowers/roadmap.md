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

### 3. Level B — app-orchestrated assignment  *(own spec; the big one for 30-person scale)*
The PoC ships level A (opportunistic + per-gateway cap). Level B gives the app a global view
and control:
- **`discovery` topic** — gateways report *seen-but-unconnected* bands so the app knows what
  is available network-wide (not just what's already connected).
- **`cmd` topic** — app assigns bands to specific gateways; firmware obeys.
- **assignment algorithm** — balance load across gateways, **solve the unserved-band edge
  case** (a band in range of only full gateways), prefer strongest RSSI, add stickiness to
  limit handoff churn.
- **Ops UI** — gateway roster and which band is on which gateway.

Additive to the PoC contract (new topics), not a rewrite.

### 4. Broker security
TLS, credentials, and ACLs. The PoC assumes a trusted local network; real deployments need
auth on the broker.

### 5. Gateway provisioning / OTA
Flashing and configuring many gateways; over-the-air firmware updates.

**Runtime Wi-Fi / config provisioning.** Today the firmware bakes Wi-Fi credentials and
the broker address in at compile time via a gitignored `secrets.h` — fine for one PoC
gateway, but painful at ~30 and impossible for a non-developer to change. Add an easy way
to set the wireless settings (and broker/gateway id) on a running device, without
re-flashing:
- **Serial config** — a small command interface over USB serial that writes SSID /
  password / broker / gateway id to NVS (persistent flash), read at boot.
- **Hosted captive portal** — on first boot (or when it can't join a network) the ESP32
  starts a SoftAP + web page (e.g. WiFiManager-style) where you pick the SSID and enter
  the password from a phone/laptop; it stores to NVS and reconnects.
Either replaces compile-time `secrets.h` as the source of config; keep `secrets.h` as an
optional dev override. Store secrets in NVS, never in the repo.

### 6. 30-person scale validation
The actual target. Validate processing/synchrony throughput, the Show puddle at ~30 dots,
and MQTT message volume at scale. May surface tuning work in `processing/` and the frontend.

### 7. Session persistence beyond JSONL captures
Existing README roadmap item — durable session storage/history beyond flat capture files.

## Known deferred edge cases

- **Unserved band** (level A): a band in range of only at-capacity gateways is invisible to
  the app. Fixed by level B (or more gateways / better coverage).
- **Handoff churn**: without stickiness, a band on the edge of two gateways could flap
  between them. Addressed by the level B assignment algorithm.
