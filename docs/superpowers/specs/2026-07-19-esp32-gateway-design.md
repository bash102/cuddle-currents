# ESP32 Gateway PoC — Design

**Status:** approved design, pre-implementation
**Date:** 2026-07-19
**Scope:** Phase 2, first slice — an end-to-end proof-of-concept for the BLE→WiFi gateway
ingestion path, proving one gateway before scaling toward ~30 people.

## 1. Context & goal

Phase 1 connects Coospo BLE armbands directly to a Mac. macOS CoreBluetooth holds only
~7–10 peripherals, so scaling requires **BLE→WiFi gateways**: bands connect to small
gateways that forward samples over the local network. The `SampleSource` Protocol
(`src/cuddle/sources/base.py`) is the designed swap point — only the ingestion source
changes; `hub/`, `processing/`, `transport/`, and both frontends consume `NormalizedSample`
and never learn the origin.

This PoC de-risks that path: define the MQTT wire contract, build the Python
`GatewayMqttSource`, validate the whole app end-to-end against a **mock gateway**, then
write ESP32 firmware to the frozen contract.

**Success criteria:** the running app renders live people from bands whose samples arrive
over MQTT — connection state, enrollment, HRV/synchrony — with no change to any layer
downstream of the source.

## 2. Decisions (from brainstorming)

| Decision | Choice | Rationale |
|---|---|---|
| Transport | **MQTT** (mosquitto broker) | matches the existing stub and the Phase-2 target; PoC exercises the real path |
| HR payload | **raw `0x2A37` bytes** | firmware stays a dumb bridge; `ble_parser` remains the one decoder; RR/contact/flags preserved |
| Presence | **explicit per-device status events** + LWT | gateway is the BLE central and knows the real link state; more accurate than inference |
| Realization | **mock gateway first, then firmware** | locks the contract + app side with zero hardware; firmware targets a frozen interface |
| Identity | **the band** (`device_id` = BLE address); gateway is routing only | preserves `person_id`-keyed continuity; roaming across gateways just works |
| Multi-gateway coordination | **opportunistic + per-gateway cap** now; orchestration-ready later | zero coordination for the PoC; nothing precludes app-orchestrated assignment |

## 3. Architecture

```
[bands] --BLE--> [ESP32 gateway] --WiFi/MQTT--> [mosquitto] <--sub-- [GatewayMqttSource] -> hub -> processing -> transport -> frontend
                    (real, later)                                     (new SampleSource)      (all unchanged)
[bands] --BLE--> [mock gateway (host script)] --^  (PoC: stands in for the ESP32 to freeze the contract)
```

One new class enters the running app — `GatewayMqttSource` implementing `SampleSource`.
Everything downstream is untouched.

Build artifacts, in order:
1. `GatewayMqttSource` (`src/cuddle/sources/mqtt_source.py`, replacing the stub) + a
   `0x2A37` **encoder** (inverse of `ble_parser`).
2. **Mock gateway** (`tools/mock_gateway.py`) — publishes the exact contract; two modes.
3. **ESP32 firmware** — spec'd here, built after the contract is frozen by the mock.

## 4. MQTT wire contract

Topic prefix `cuddle/`; `<gw>` = gateway id, `<dev>` = band BLE address.

| Topic | Payload | Meaning |
|---|---|---|
| `cuddle/<gw>/hr/<dev>` | **raw `0x2A37` bytes** (binary) | one HR notification; app decodes via `ble_parser`, stamps `t_recv` on receipt |
| `cuddle/<gw>/status/<dev>` | JSON `{event: "connected"\|"disconnected", rssi: int}` | explicit per-device link event; drives `ConnectionState` + the unassigned list |
| `cuddle/<gw>/online` | `"1"` / `"0"`, **retained**, `"0"` set as the MQTT **LWT** | gateway liveness; on `0`/LWT the app marks all that gateway's bands `disconnected` |

Contract rules:
- **Identity is `<dev>`** across gateways. The same band under a different `<gw>` is the
  same `device_id` → same `person_id` → continuous session. `GatewayMqttSource` dedupes by
  `device_id`; last-connected-wins for routing.
- **Presence is explicit**: `_states[dev]` is set from `status` events, not inferred from
  HR recency, with two backstops — the gateway **LWT** and a `drop_after` silence timeout.
- HR is raw bytes (binary MQTT payload); the app never re-implements `0x2A37` decoding.

## 5. Gateway↔band association & capacity

**Governing BLE fact:** a consumer HR band holds **exactly one connection at a time** and
**stops advertising once connected**. Two gateways therefore cannot both connect to the
same band — there is no duplicate data and no shared ownership to resolve.

- "Which gateway owns a band" = **whichever gateway currently holds the connection**,
  emergent (first to connect while it advertises), not assigned. Simultaneous attempts are
  arbitrated by the BLE link layer; the loser backs off. No app-level locking needed.
- **Handoff:** when the holding gateway loses the band (range/drop), it advertises again and
  another in-range gateway may grab it. Because identity is the band, this is seamless:
  `GatewayMqttSource` tracks `device_id → current_gateway` (last-connected-wins by
  timestamp); a stale `disconnected` from the old gateway for a band already re-homed is
  ignored; `person_id` continuity holds.

**Capacity:** ESP32 BLE-central connections — NimBLE supports up to ~9; **~4–6 is the
reliable planning number**. At `max_connections` a gateway stops initiating new connections,
leaving other bands advertising for gateways with headroom — **self-balancing given coverage
overlap**.

Coordination levels:

| Level | Mechanism | When |
|---|---|---|
| **A. Opportunistic + cap** | each gateway connects to any `0x180D` band up to `max_connections`; BLE arbitration + cap balance implicitly | **PoC / this spec** |
| **B. App-orchestrated** | app sees all gateways over MQTT, assigns bands via a `cuddle/<gw>/cmd` topic; a `discovery` topic reports seen-but-unconnected bands | growth path (30 people) |
| C. Static allowlist | config: gateway→bands | rejected — manual, breaks roaming |

**Known edge case (level A):** a band in range of *only* full gateways is unserved and
invisible to the app. Resolved by level B (or more gateways / better coverage). Level B is
additive — `cmd` and `discovery` topics, no rework of this contract.

**PoC:** level A, one gateway, a few bands. `max_connections` is enforced by the gateway;
its default (4) is a conservative starting point **validated empirically** during firmware
bring-up (build order step 6) — the measured reliable ceiling sets the shipped default and
informs how many gateways the 30-person deployment needs.

## 6. `GatewayMqttSource` (Python)

Replaces the stub; implements `SampleSource` (`start`/`stop`/`subscribe`/
`connection_states`/`unassigned_devices`/`bind`/`unbind`).

- **Client:** `aiomqtt` (asyncio-native), callbacks on the event loop — no thread
  marshaling. Subscribes `cuddle/+/hr/+`, `cuddle/+/status/+`, `cuddle/+/online`.
- **`hr/<dev>`:** decode raw bytes via `ble_parser.parse_hr_measurement`; emit
  `NormalizedSample(person_id=bindings.get(dev, dev), device_id=dev, source=mqtt,
  t_recv=clock.now(), rr_intervals, contact, flags, seq=++counter[dev])` onto the queue.
  `subscribe()` yields from the queue.
- **`status/<dev>`:** set `_states[dev]` from the event; record `rssi`, `last_seen`,
  `device→gateway`; unbound devices surface in `unassigned_devices()`.
- **`online`/LWT:** on gateway `0`, mark every device homed to that gateway `disconnected`.
- **Backstops:** reuse `reconnect.drop_after` / `reconnect.evict_after` — a device silent
  past `drop_after` → `disconnected`; unbound + absent past `evict_after` → evicted (same
  semantics as the Phase-1 BLE source fix).
- `bind`/`unbind` identical to BLE; the three-place binding invariant and the enrollment
  flow are unchanged. `source=mqtt` on emitted samples.

## 7. `0x2A37` encoder

Small inverse of `ble_parser.parse_hr_measurement` (build a Heart Rate Measurement frame
from `hr_bpm` + `rr_intervals` + flags), living beside the parser. Needed for the mock
gateway's replay mode and for tests. Golden-tested by round-tripping with the decoder.

## 8. Mock gateway (`tools/mock_gateway.py`)

Publishes the exact contract so the app path is validated with no ESP32. Two modes:
- `--mode bleak`: a real BLE central (reusing `DirectBleSource`'s connect logic) that
  republishes raw `0x2A37` notification bytes and emits `status`/`online`. Needs bands.
- `--mode replay --capture <jsonl>`: re-encodes a recorded capture's RR into `0x2A37`
  frames via the encoder and publishes on a synthetic schedule. Fully hardware-free.

## 9. ESP32 firmware (spec; built after the contract freezes)

- **Framework:** Arduino + **NimBLE-Arduino** (higher central-connection ceiling than
  Bluedroid).
- **Loop:** scan for `0x180D`; connect up to `max_connections` (default 4); per band,
  subscribe `0x2A37` and republish the **raw notification bytes** to `cuddle/<gw>/hr/<dev>`;
  publish `status` on connect/drop + periodic RSSI; set the retained `online` LWT.
- **Transport:** Wi-Fi + MQTT (`PubSubClient` or ESP-IDF mqtt). `<gw>` from compile-time/NVS
  id; `<dev>` = band MAC. BLE and MQTT reconnect/backoff mirror the Python semantics.

## 10. Testing

- **`GatewayMqttSource`:** feed synthetic messages straight into the handlers (no broker) —
  raw `0x2A37` bytes, `status` JSON, `online`/LWT, and a handoff sequence (A connects → A
  drops → B connects) — assert `NormalizedSample`, `connection_states`,
  `unassigned_devices`, and `person_id` continuity. Mirrors the pure-handler style of
  `tests/test_ble_source.py`.
- **Encoder:** golden round-trip with `ble_parser`.
- **Integration (optional):** mosquitto + mock gateway (replay) + app; assert frames flow.

## 11. Dependencies, CLI, config

- **Dependency:** add `aiomqtt`. Mosquitto is a documented dev/runtime prereq (not pip).
- **CLI:** `--source mqtt --broker localhost:1883`.
- **Config:** new `mqtt:` section — `broker` (host:port), `topic_prefix: cuddle`,
  `max_connections` (gateway hint / firmware default). Reuse `reconnect.drop_after` /
  `reconnect.evict_after` for the source backstops.

## 12. Build order

1. `0x2A37` encoder + golden tests.
2. `GatewayMqttSource` + handler unit tests (no broker).
3. Mock gateway (replay mode) + CLI `--source mqtt`; validate end-to-end against mosquitto.
4. Mock gateway (bleak mode) with real bands.
5. ESP32 firmware to the frozen contract.
6. **Validate `max_connections`** — stress one ESP32 with an increasing number of bands
   and measure the reliable ceiling: connection stability over time, `0x2A37` notification
   throughput, and dropped/missed beats per band under sustained streaming. NimBLE allows
   up to ~9 concurrent connections, but the reliable number under continuous HR
   notifications is expected to be lower; set the `max_connections` default from this
   measurement (and record it in the spec + config). This number gates how many gateways
   the 30-person deployment needs, so it must be measured, not assumed.

## 13. Out of scope (this slice)

- App-orchestrated assignment (level B): `cmd` / `discovery` topics, load balancing.
- Multi-gateway deployment, failover, and the unserved-band edge case.
- Security/auth on the broker (local trusted network assumed for the PoC).
- Gateway provisioning/OTA.
