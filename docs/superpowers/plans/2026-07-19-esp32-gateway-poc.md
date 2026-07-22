# ESP32 Gateway PoC (software slice) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ingest heart-rate samples over MQTT from BLE→WiFi gateways into the existing app, validated end-to-end by a hardware-free mock gateway.

**Architecture:** One new `SampleSource` — `GatewayMqttSource` — subscribes to a mosquitto broker and turns MQTT messages (raw `0x2A37` HR bytes, JSON status events, gateway LWT) into `NormalizedSample`s and per-device `ConnectionState`. Nothing downstream of the source changes. A `0x2A37` encoder (inverse of the existing parser) and a `tools/mock_gateway.py` publisher let us exercise the whole path without an ESP32.

**Tech Stack:** Python 3.11+, `aiomqtt` (asyncio MQTT client), mosquitto (dev/runtime broker), existing `numpy`/`fastapi`/`pydantic` stack, `pytest`.

## Global Constraints

- Python `>=3.11`; `src/` layout; tests under `tests/`, run with `pytest` (config in `pyproject.toml`).
- The source of truth for `0x2A37` decoding is `src/cuddle/sources/ble_parser.py` — never re-implement decoding elsewhere.
- Emitted samples use `source=Source.mqtt` (enum already exists in `core/models.py`).
- Device identity is the band's address (`device_id`); the gateway id is routing metadata only. A band under a new gateway is the same `device_id` → same `person_id`.
- Time comes from `cuddle.core.clock.now()`; testable helpers take an explicit `now` argument.
- Reuse the existing `reconnect.drop_after` (20.0) and `reconnect.evict_after` (120.0) config for source backstops; do not invent new silence/eviction knobs.
- Firmware (spec build-order steps 5–6) is OUT OF SCOPE for this plan — separate embedded plan.

---

### Task 1: `0x2A37` encoder

**Files:**
- Modify: `src/cuddle/sources/ble_parser.py` (add `encode_hr_measurement`)
- Test: `tests/test_ble_parser.py` (append)

**Interfaces:**
- Consumes: existing `parse_hr_measurement(data: bytes) -> HeartRateMeasurement`.
- Produces: `encode_hr_measurement(hr_bpm: int, rr_intervals: list[float] | None = None, contact: bool | None = None, energy_expended: int | None = None) -> bytes` — builds a spec-compliant Heart Rate Measurement (`0x2A37`) frame that `parse_hr_measurement` round-trips.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ble_parser.py`:

```python
from cuddle.sources.ble_parser import encode_hr_measurement, parse_hr_measurement


def test_encode_roundtrips_uint8_hr_no_rr():
    frame = encode_hr_measurement(72)
    m = parse_hr_measurement(frame)
    assert m.hr_bpm == 72
    assert m.rr_intervals == []


def test_encode_sets_rr_present_flag_and_quantizes():
    frame = encode_hr_measurement(60, rr_intervals=[1.0, 0.5])
    assert frame[0] & 0x10  # RR-present flag
    m = parse_hr_measurement(frame)
    assert len(m.rr_intervals) == 2
    assert m.rr_intervals[0] == pytest.approx(1.0, abs=1 / 1024)
    assert m.rr_intervals[1] == pytest.approx(0.5, abs=1 / 1024)


def test_encode_uint16_hr_when_over_255():
    frame = encode_hr_measurement(300)
    assert frame[0] & 0x01  # 16-bit HR format flag
    assert parse_hr_measurement(frame).hr_bpm == 300


def test_encode_contact_bits_roundtrip():
    assert parse_hr_measurement(encode_hr_measurement(65, contact=True)).contact is True
    assert parse_hr_measurement(encode_hr_measurement(65, contact=False)).contact is False
    assert parse_hr_measurement(encode_hr_measurement(65)).contact is None
```

(Ensure `import pytest` is present at the top of the file.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ble_parser.py -k encode -v`
Expected: FAIL with `ImportError: cannot import name 'encode_hr_measurement'`.

- [ ] **Step 3: Implement the encoder**

Append to `src/cuddle/sources/ble_parser.py`:

```python
def encode_hr_measurement(
    hr_bpm: int,
    rr_intervals: list[float] | None = None,
    contact: bool | None = None,
    energy_expended: int | None = None,
) -> bytes:
    """Build a 0x2A37 Heart Rate Measurement frame (inverse of parse_hr_measurement).

    RR intervals are quantized to the 1/1024 s wire resolution. Used by the mock
    gateway's replay mode and by tests; the round-trip with parse is golden-tested.
    """
    rr_intervals = rr_intervals or []
    flags = 0
    hr16 = hr_bpm > 0xFF
    if hr16:
        flags |= 0x01
    if contact is not None:
        flags |= 0x04  # contact supported
        if contact:
            flags |= 0x02  # contact detected
    if energy_expended is not None:
        flags |= 0x08
    if rr_intervals:
        flags |= 0x10

    out = bytearray([flags])
    if hr16:
        out += int(hr_bpm).to_bytes(2, "little")
    else:
        out.append(hr_bpm & 0xFF)
    if energy_expended is not None:
        out += int(energy_expended).to_bytes(2, "little")
    for rr in rr_intervals:
        raw = max(0, min(0xFFFF, round(rr * 1024.0)))
        out += int(raw).to_bytes(2, "little")
    return bytes(out)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_ble_parser.py -k encode -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/cuddle/sources/ble_parser.py tests/test_ble_parser.py
git commit -m "feat: add 0x2A37 encoder (inverse of the HR parser)"
```

---

### Task 2: `GatewayMqttSource` — HR ingestion path

**Files:**
- Modify: `src/cuddle/sources/mqtt_source.py` (replace the `NotImplementedError` stub)
- Test: `tests/test_mqtt_source.py` (create)

**Interfaces:**
- Consumes: `parse_hr_measurement` and `encode_hr_measurement` (Task 1); `NormalizedSample`, `Source`, `ConnectionState`, `DeviceInfo` from `core/models`; `clock.now`.
- Produces: `GatewayMqttSource(*, broker="127.0.0.1", port=1883, topic_prefix="cuddle", drop_after=20.0, evict_after=120.0, stale_after_rr_factor=2.5)` with a sync `_handle_message(topic: str, payload: bytes) -> None` dispatcher, `bind(device_id, person_id)`, `unbind(device_id)`, and an async `subscribe()` yielding `NormalizedSample`. Later tasks add presence handlers and the MQTT client loop.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mqtt_source.py`:

```python
import pytest

from cuddle.core.models import Source
from cuddle.sources.ble_parser import encode_hr_measurement
from cuddle.sources.mqtt_source import GatewayMqttSource


def _src():
    return GatewayMqttSource(broker="127.0.0.1", port=1883, topic_prefix="cuddle")


def test_hr_message_emits_sample_with_device_id_when_unbound():
    s = _src()
    frame = encode_hr_measurement(66, rr_intervals=[0.9])
    s._handle_message("cuddle/gw1/hr/AA:BB", frame)
    sample = s._queue.get_nowait()
    assert sample.device_id == "AA:BB"
    assert sample.person_id == "AA:BB"  # unbound -> provisional id == device id
    assert sample.source == Source.mqtt
    assert sample.hr_bpm == 66
    assert sample.rr_intervals[0] == pytest.approx(0.9, abs=1 / 1024)
    assert sample.seq == 1


def test_hr_message_uses_bound_person_id():
    s = _src()
    s.bind("AA:BB", "wren")
    s._handle_message("cuddle/gw1/hr/AA:BB", encode_hr_measurement(70))
    assert s._queue.get_nowait().person_id == "wren"


def test_seq_increments_per_device():
    s = _src()
    for _ in range(3):
        s._handle_message("cuddle/gw1/hr/AA:BB", encode_hr_measurement(70))
    seqs = [s._queue.get_nowait().seq for _ in range(3)]
    assert seqs == [1, 2, 3]


def test_malformed_hr_payload_is_ignored():
    s = _src()
    s._handle_message("cuddle/gw1/hr/AA:BB", b"\x00")  # too short for a valid frame
    assert s._queue.empty()


def test_wrong_prefix_ignored():
    s = _src()
    s._handle_message("other/gw1/hr/AA:BB", encode_hr_measurement(70))
    assert s._queue.empty()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mqtt_source.py -v`
Expected: FAIL — importing `GatewayMqttSource` raises `NotImplementedError` on construction (the stub).

- [ ] **Step 3: Replace the stub with the HR path**

Replace the entire body of `src/cuddle/sources/mqtt_source.py` (keep the module docstring's first paragraph, drop the "Not implemented" sketch) with:

```python
"""Ingest from BLE->WiFi gateways over MQTT (Phase 2).

Gateways publish raw 0x2A37 HR bytes, per-device status events, and an `online`
LWT. This source decodes with the shared ble_parser, keys everything by the band's
device_id (gateway is routing only), and synthesizes ConnectionState from the
explicit status events plus silence/LWT backstops. Everything downstream consumes
NormalizedSample and never learns the origin.
"""

from __future__ import annotations

import asyncio
import json

from cuddle.core import clock
from cuddle.core.models import ConnectionState, DeviceInfo, NormalizedSample, Source
from cuddle.sources.ble_parser import parse_hr_measurement


class GatewayMqttSource:
    def __init__(
        self,
        *,
        broker: str = "127.0.0.1",
        port: int = 1883,
        topic_prefix: str = "cuddle",
        drop_after: float = 20.0,
        evict_after: float = 120.0,
        stale_after_rr_factor: float = 2.5,
    ) -> None:
        self._broker = broker
        self._port = port
        self._prefix = topic_prefix
        self._drop_after = drop_after
        self._evict_after = evict_after
        self._stale_after_rr_factor = stale_after_rr_factor

        self._queue: asyncio.Queue[NormalizedSample] = asyncio.Queue()
        self._states: dict[str, ConnectionState] = {}
        self._bindings: dict[str, str] = {}  # device_id -> person_id
        self._seq: dict[str, int] = {}
        self._last_hr: dict[str, int] = {}
        self._rssi: dict[str, int | None] = {}
        self._last_seen: dict[str, float] = {}  # device_id -> time of last message
        self._device_gw: dict[str, str] = {}  # device_id -> current gateway id
        self._gw_devices: dict[str, set[str]] = {}  # gateway id -> device ids
        self._task: asyncio.Task | None = None
        self._running = False

    # ---- SampleSource protocol ------------------------------------------

    async def subscribe(self):
        while True:
            yield await self._queue.get()

    def bind(self, device_id: str, person_id: str) -> None:
        self._bindings[device_id] = person_id

    def unbind(self, device_id: str) -> None:
        self._bindings.pop(device_id, None)

    # ---- message handling (sync, testable) ------------------------------

    def _handle_message(self, topic: str, payload: bytes) -> None:
        parts = topic.split("/")
        if len(parts) < 3 or parts[0] != self._prefix:
            return
        _, gw, kind = parts[0], parts[1], parts[2]
        if kind == "hr" and len(parts) == 4:
            self._handle_hr(gw, parts[3], payload)
        elif kind == "status" and len(parts) == 4:
            self._handle_status(gw, parts[3], payload)
        elif kind == "online":
            self._handle_online(gw, payload)

    def _route_device(self, dev: str, gw: str) -> None:
        """Home a device to the gateway that last reported it (last-connected-wins)."""
        prev = self._device_gw.get(dev)
        if prev is not None and prev != gw:
            self._gw_devices.get(prev, set()).discard(dev)
        self._device_gw[dev] = gw
        self._gw_devices.setdefault(gw, set()).add(dev)

    def _handle_hr(self, gw: str, dev: str, payload: bytes) -> None:
        try:
            m = parse_hr_measurement(payload)
        except ValueError:
            return
        now = clock.now()
        self._last_hr[dev] = m.hr_bpm
        self._last_seen[dev] = now
        self._states[dev] = ConnectionState.connected  # beats imply a live link
        self._route_device(dev, gw)
        self._seq[dev] = self._seq.get(dev, 0) + 1
        person_id = self._bindings.get(dev, dev)
        self._queue.put_nowait(
            NormalizedSample(
                person_id=person_id,
                device_id=dev,
                source=Source.mqtt,
                t_recv=now,
                hr_bpm=m.hr_bpm,
                rr_intervals=m.rr_intervals,
                contact=m.contact,
                raw_flags=m.flags,
                seq=self._seq[dev],
            )
        )

    def _handle_status(self, gw: str, dev: str, payload: bytes) -> None:
        pass  # implemented in Task 3

    def _handle_online(self, gw: str, payload: bytes) -> None:
        pass  # implemented in Task 3
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_mqtt_source.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/cuddle/sources/mqtt_source.py tests/test_mqtt_source.py
git commit -m "feat: GatewayMqttSource HR ingestion (raw 0x2A37 over MQTT)"
```

---

### Task 3: `GatewayMqttSource` — presence, handoff, and eviction

**Files:**
- Modify: `src/cuddle/sources/mqtt_source.py` (fill in `_handle_status`/`_handle_online`, add link-state/eviction helpers + `connection_states`/`unassigned_devices`)
- Test: `tests/test_mqtt_source.py` (append)

**Interfaces:**
- Consumes: everything from Task 2.
- Produces: `connection_states -> dict[str, ConnectionState]`, `unassigned_devices() -> list[DeviceInfo]`, `_link_state(dev, now) -> ConnectionState`, `_evictable(now) -> list[str]`, `_evict(dev)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mqtt_source.py`:

```python
import json
from cuddle.core.models import ConnectionState
import cuddle.sources.mqtt_source as mqtt_mod


def _status(event, rssi=-60):
    return json.dumps({"event": event, "rssi": rssi}).encode()


def test_status_connected_then_disconnected(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(mqtt_mod.clock, "now", lambda: t[0])
    s = _src()
    s._handle_message("cuddle/gw1/status/AA:BB", _status("connected"))
    assert s.connection_states["AA:BB"] == ConnectionState.connected
    s._handle_message("cuddle/gw1/status/AA:BB", _status("disconnected"))
    assert s.connection_states["AA:BB"] == ConnectionState.disconnected


def test_silence_past_drop_after_reads_disconnected(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(mqtt_mod.clock, "now", lambda: t[0])
    s = _src()
    s._handle_message("cuddle/gw1/status/AA:BB", _status("connected"))
    t[0] += 25.0  # > drop_after (20)
    assert s.connection_states["AA:BB"] == ConnectionState.disconnected


def test_handoff_ignores_stale_disconnect_from_old_gateway(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(mqtt_mod.clock, "now", lambda: t[0])
    s = _src()
    s._handle_message("cuddle/gw1/status/AA:BB", _status("connected"))
    s._handle_message("cuddle/gw2/status/AA:BB", _status("connected"))  # handoff to gw2
    s._handle_message("cuddle/gw1/status/AA:BB", _status("disconnected"))  # stale from gw1
    assert s.connection_states["AA:BB"] == ConnectionState.connected


def test_gateway_lwt_marks_its_devices_disconnected(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(mqtt_mod.clock, "now", lambda: t[0])
    s = _src()
    s._handle_message("cuddle/gw1/status/AA:BB", _status("connected"))
    s._handle_message("cuddle/gw1/online", b"0")
    assert s.connection_states["AA:BB"] == ConnectionState.disconnected


def test_unassigned_lists_unbound_only():
    s = _src()
    s._handle_message("cuddle/gw1/status/AA:BB", _status("connected"))
    s._handle_message("cuddle/gw1/status/CC:DD", _status("connected"))
    s.bind("AA:BB", "wren")
    devs = {d.device_id for d in s.unassigned_devices()}
    assert devs == {"CC:DD"}


def test_evictable_only_unbound_and_absent(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(mqtt_mod.clock, "now", lambda: t[0])
    s = _src()
    s._handle_message("cuddle/gw1/status/AA:BB", _status("connected"))
    s._handle_message("cuddle/gw1/status/CC:DD", _status("connected"))
    s.bind("CC:DD", "wren")
    now = t[0] + 130.0  # > evict_after (120)
    assert s._evictable(now) == ["AA:BB"]  # bound CC:DD never evictable
    s._evict("AA:BB")
    assert "AA:BB" not in s._states
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mqtt_source.py -k "status or silence or handoff or lwt or unassigned or evict" -v`
Expected: FAIL — `connection_states`/`unassigned_devices`/`_evictable` not defined, and presence handlers are no-ops.

- [ ] **Step 3: Implement presence, link-state, and eviction**

In `src/cuddle/sources/mqtt_source.py`, replace the two placeholder handlers and add the helpers/Protocol methods:

```python
    def _handle_status(self, gw: str, dev: str, payload: bytes) -> None:
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            return
        now = clock.now()
        self._last_seen[dev] = now
        if "rssi" in data:
            self._rssi[dev] = data["rssi"]
        event = data.get("event")
        if event == "connected":
            self._route_device(dev, gw)
            self._states[dev] = ConnectionState.connected
        elif event == "disconnected":
            # honor only from the gateway that currently owns the band (ignore a
            # stale drop from the old gateway after a handoff)
            if self._device_gw.get(dev) == gw:
                self._states[dev] = ConnectionState.disconnected

    def _handle_online(self, gw: str, payload: bytes) -> None:
        if payload in (b"0", b"", b"false"):
            for dev in list(self._gw_devices.get(gw, ())):
                if self._device_gw.get(dev) == gw:
                    self._states[dev] = ConnectionState.disconnected

    # ---- link-state / eviction helpers (pure, testable) -----------------

    def _link_state(self, dev: str, now: float) -> ConnectionState:
        raw = self._states.get(dev, ConnectionState.disconnected)
        last = self._last_seen.get(dev)
        if last is None:
            return raw
        silent = now - last
        if silent > self._drop_after:
            return ConnectionState.disconnected
        if raw == ConnectionState.connected:
            hr = self._last_hr.get(dev)
            expected = 60.0 / hr if hr else 1.0
            if silent > self._stale_after_rr_factor * expected:
                return ConnectionState.stale
        return raw

    def _evictable(self, now: float) -> list[str]:
        return [
            dev
            for dev in self._states
            if dev not in self._bindings
            and self._last_seen.get(dev) is not None
            and (now - self._last_seen[dev]) > self._evict_after
        ]

    def _evict(self, dev: str) -> None:
        for d in (self._states, self._seq, self._last_hr, self._rssi, self._last_seen, self._device_gw):
            d.pop(dev, None)
        for s in self._gw_devices.values():
            s.discard(dev)

    # ---- SampleSource protocol (presence views) -------------------------

    @property
    def connection_states(self) -> dict[str, ConnectionState]:
        now = clock.now()
        return {dev: self._link_state(dev, now) for dev in self._states}

    def unassigned_devices(self) -> list[DeviceInfo]:
        now = clock.now()
        return [
            DeviceInfo(
                device_id=dev,
                source=Source.mqtt,
                hr_bpm=self._last_hr.get(dev),
                connection=self._link_state(dev, now),
                rssi=self._rssi.get(dev),
            )
            for dev in self._states
            if dev not in self._bindings
        ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_mqtt_source.py -v`
Expected: PASS (all — Task 2 + Task 3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/cuddle/sources/mqtt_source.py tests/test_mqtt_source.py
git commit -m "feat: GatewayMqttSource presence, handoff, LWT, eviction"
```

---

### Task 4: MQTT client loop + config + CLI wiring

**Files:**
- Modify: `pyproject.toml` (add `aiomqtt` dependency)
- Modify: `src/cuddle/sources/mqtt_source.py` (add `start`/`stop`/`_run` + reaper)
- Modify: `src/cuddle/core/config.py` (`mqtt` defaults)
- Modify: `config/app.yaml` (`mqtt:` section)
- Modify: `src/cuddle/cli.py` (`--source mqtt`, `--broker`)
- Test: `tests/test_mqtt_source.py` (append a reaper test), `tests/test_config.py` if present (else skip)

**Interfaces:**
- Consumes: everything from Tasks 2–3.
- Produces: `async start()`, `async stop()`, an internal `_run()` subscribe loop, and `_reap(now)`; CLI `--source mqtt --broker host:port` constructing `GatewayMqttSource` with `source_type=Source.mqtt`.

- [ ] **Step 1: Write the failing test (reaper)**

Append to `tests/test_mqtt_source.py`:

```python
def test_reap_removes_evictable_devices(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(mqtt_mod.clock, "now", lambda: t[0])
    s = _src()
    s._handle_message("cuddle/gw1/status/AA:BB", _status("connected"))
    s._reap(t[0] + 130.0)
    assert "AA:BB" not in s._states and "AA:BB" not in s._last_seen
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_mqtt_source.py -k reap -v`
Expected: FAIL with `AttributeError: 'GatewayMqttSource' object has no attribute '_reap'`.

- [ ] **Step 3: Add the client loop, reaper, start/stop**

Add `import contextlib` to the top of `src/cuddle/sources/mqtt_source.py`, then append these methods to `GatewayMqttSource`:

```python
    def _reap(self, now: float) -> None:
        for dev in self._evictable(now):
            self._evict(dev)

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run(), name="mqtt-ingest")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        import aiomqtt

        sub = f"{self._prefix}/#"
        while self._running:
            try:
                async with aiomqtt.Client(self._broker, self._port) as client:
                    await client.subscribe(sub)
                    async for message in client.messages:
                        if not self._running:
                            break
                        self._handle_message(str(message.topic), bytes(message.payload))
                        self._reap(clock.now())
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1.0)  # broker unreachable; retry
```

- [ ] **Step 4: Run the reaper test**

Run: `pytest tests/test_mqtt_source.py -v`
Expected: PASS (all).

- [ ] **Step 5: Add the dependency**

In `pyproject.toml`, add to `dependencies`:

```toml
    "aiomqtt>=2.0",
```

Then install: `pip install -e '.[dev]'`
Expected: `aiomqtt` installs without error.

- [ ] **Step 6: Add config defaults**

In `src/cuddle/core/config.py`, add a new top-level entry to `_DEFAULTS` (after the `reconnect` block):

```python
    "mqtt": {
        "broker": "127.0.0.1",
        "port": 1883,
        "topic_prefix": "cuddle",
        "max_connections": 4,  # per-gateway cap (gateway/firmware hint; validated on hardware)
    },
```

In `config/app.yaml`, append:

```yaml
mqtt:
  broker: 127.0.0.1           # mosquitto host
  port: 1883
  topic_prefix: cuddle        # topics: cuddle/<gateway>/hr|status/<device>, cuddle/<gateway>/online
  max_connections: 4          # per-gateway BLE cap; conservative default, validate on hardware
```

- [ ] **Step 7: Extract a testable parser in `cli.py`**

`cli.py` currently builds its `argparse` parser inline in `main()`. Extract it so the
CLI can be tested. Move the parser setup into a new function and have `main()` call it:

```python
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="cuddle", description="Cuddle Currents POC")
    # ... move every existing ap.add_argument(...) line here unchanged ...
    return ap


def main() -> None:
    args = build_parser().parse_args()
    engine = build_engine(args)
    # ... rest of main() unchanged ...
```

- [ ] **Step 8: Wire the `mqtt` source**

In `build_parser()`, add `"mqtt"` to the `--source` choices and add a `--broker` option:

```python
    ap.add_argument("--source", choices=["sim", "ble", "replay", "mqtt"], default="sim")
    ap.add_argument("--broker", help="MQTT broker host:port (with --source mqtt)")
```

In `build_engine`, add a branch before the `else`:

```python
    elif args.source == "mqtt":
        from cuddle.sources.mqtt_source import GatewayMqttSource

        mq = cfg["mqtt"]
        rc = cfg["reconnect"]
        broker = args.broker or f"{mq['broker']}:{mq['port']}"
        host, _, port = broker.partition(":")
        source = GatewayMqttSource(
            broker=host,
            port=int(port or mq["port"]),
            topic_prefix=mq["topic_prefix"],
            drop_after=rc["drop_after"],
            evict_after=rc["evict_after"],
            stale_after_rr_factor=cfg["processing"]["stale_after_rr_factor"],
        )
        source_type = Source.mqtt
```

- [ ] **Step 9: Write and run the CLI test**

Create `tests/test_cli_mqtt.py`:

```python
from cuddle.cli import build_engine, build_parser
from cuddle.core.models import Source
from cuddle.sources.mqtt_source import GatewayMqttSource


def test_mqtt_source_builds_from_cli():
    args = build_parser().parse_args(["--source", "mqtt", "--broker", "localhost:1884"])
    engine = build_engine(args)
    assert isinstance(engine.source, GatewayMqttSource)
    assert engine.source_type == Source.mqtt
    assert engine.source._broker == "localhost" and engine.source._port == 1884
```

Run: `pytest tests/test_cli_mqtt.py -v`
Expected: PASS.

- [ ] **Step 10: Run the full suite**

Run: `pytest -q`
Expected: PASS (all existing + new).

- [ ] **Step 11: Commit**

```bash
git add pyproject.toml src/cuddle/sources/mqtt_source.py src/cuddle/core/config.py config/app.yaml src/cuddle/cli.py tests/test_mqtt_source.py tests/test_cli_mqtt.py
git commit -m "feat: MQTT client loop + config + --source mqtt CLI wiring"
```

---

### Task 5: Mock gateway — replay mode + end-to-end validation

**Files:**
- Create: `tools/mock_gateway.py`
- Test: `tests/test_mock_gateway.py` (create)

**Interfaces:**
- Consumes: `encode_hr_measurement` (Task 1); `aiomqtt`; a capture JSONL of `NormalizedSample` rows (see `captures/*.jsonl`).
- Produces: `build_hr_topic(prefix, gw, dev) -> str`, `build_status_topic(prefix, gw, dev) -> str`, `online_topic(prefix, gw) -> str`, and `frames_from_capture(path) -> list[tuple[float, str, bytes]]` (relative-time, topic, payload) — the pure, testable core. Plus an async `run_replay(...)` that publishes them.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mock_gateway.py`:

```python
import json

from cuddle.sources.ble_parser import parse_hr_measurement
from tools.mock_gateway import build_hr_topic, frames_from_capture


def test_topic_builder():
    assert build_hr_topic("cuddle", "gwA", "AA:BB") == "cuddle/gwA/hr/AA:BB"


def test_frames_from_capture_encode_roundtrip(tmp_path):
    cap = tmp_path / "cap.jsonl"
    rows = [
        {"person_id": "AA:BB", "device_id": "AA:BB", "source": "ble", "t_recv": 100.0,
         "hr_bpm": 60, "rr_intervals": [1.0], "contact": True, "raw_flags": 16, "seq": 1},
        {"person_id": "AA:BB", "device_id": "AA:BB", "source": "ble", "t_recv": 101.0,
         "hr_bpm": 61, "rr_intervals": [0.98], "contact": True, "raw_flags": 16, "seq": 2},
    ]
    cap.write_text("\n".join(json.dumps(r) for r in rows))
    frames = frames_from_capture(str(cap))
    assert [round(t, 2) for t, _, _ in frames] == [0.0, 1.0]  # relative timing
    assert frames[0][1] == "cuddle/mock/hr/AA:BB"  # default gw id "mock"
    m = parse_hr_measurement(frames[0][2])
    assert m.hr_bpm == 60 and len(m.rr_intervals) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mock_gateway.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tools'` (or `tools.mock_gateway`).

- [ ] **Step 3: Create the mock gateway**

Create `tools/__init__.py` (empty) and `tools/mock_gateway.py`:

```python
"""Mock BLE->WiFi gateway: publishes the ESP32 gateway MQTT contract with no ESP32.

Modes:
  replay  — re-encode a recorded capture's RR into 0x2A37 frames and publish them
            on the capture's own timing (fully hardware-free).
  bleak   — a real BLE central that republishes raw notification bytes (Task 6).

Contract: cuddle/<gw>/hr/<dev> (raw 0x2A37 bytes), cuddle/<gw>/status/<dev> (JSON
{event, rssi}), cuddle/<gw>/online (retained "1"/"0", "0" as LWT).
"""

from __future__ import annotations

import argparse
import asyncio
import json

from cuddle.sources.ble_parser import encode_hr_measurement


def build_hr_topic(prefix: str, gw: str, dev: str) -> str:
    return f"{prefix}/{gw}/hr/{dev}"


def build_status_topic(prefix: str, gw: str, dev: str) -> str:
    return f"{prefix}/{gw}/status/{dev}"


def online_topic(prefix: str, gw: str) -> str:
    return f"{prefix}/{gw}/online"


def frames_from_capture(
    path: str, *, prefix: str = "cuddle", gw: str = "mock"
) -> list[tuple[float, str, bytes]]:
    """(relative_seconds, topic, raw 0x2A37 payload) for each RR-bearing capture row."""
    frames: list[tuple[float, str, bytes]] = []
    t0: float | None = None
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if not row.get("rr_intervals"):
                continue
            if t0 is None:
                t0 = row["t_recv"]
            payload = encode_hr_measurement(
                row["hr_bpm"],
                rr_intervals=row.get("rr_intervals"),
                contact=row.get("contact"),
            )
            frames.append(
                (row["t_recv"] - t0, build_hr_topic(prefix, gw, row["device_id"]), payload)
            )
    return frames


async def run_replay(path: str, broker: str, port: int, prefix: str, gw: str) -> None:
    import aiomqtt

    frames = frames_from_capture(path, prefix=prefix, gw=gw)
    devices = sorted({topic.split("/")[-1] for _, topic, _ in frames})
    will = aiomqtt.Will(online_topic(prefix, gw), b"0", qos=1, retain=True)
    async with aiomqtt.Client(broker, port, will=will) as client:
        await client.publish(online_topic(prefix, gw), b"1", qos=1, retain=True)
        for dev in devices:
            await client.publish(
                build_status_topic(prefix, gw, dev),
                json.dumps({"event": "connected", "rssi": -55}).encode(),
            )
        prev = 0.0
        for rel, topic, payload in frames:
            await asyncio.sleep(max(0.0, rel - prev))
            prev = rel
            await client.publish(topic, payload)
        await client.publish(online_topic(prefix, gw), b"0", qos=1, retain=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Mock BLE->WiFi gateway (MQTT publisher)")
    ap.add_argument("--mode", choices=["replay", "bleak"], default="replay")
    ap.add_argument("--capture", help="capture JSONL for --mode replay")
    ap.add_argument("--broker", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--prefix", default="cuddle")
    ap.add_argument("--gateway", default="mock")
    args = ap.parse_args()
    if args.mode == "replay":
        if not args.capture:
            raise SystemExit("--capture is required for --mode replay")
        asyncio.run(run_replay(args.capture, args.broker, args.port, args.prefix, args.gateway))
    else:
        raise SystemExit("--mode bleak is implemented in Task 6")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_mock_gateway.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Manual end-to-end validation (documented)**

Run these in three terminals (requires mosquitto: `brew install mosquitto`):

```bash
# 1. broker
mosquitto -p 1883

# 2. app against MQTT
cuddle --source mqtt --broker 127.0.0.1:1883

# 3. mock gateway replaying a real capture
python -m tools.mock_gateway --mode replay --capture captures/two-people.jsonl
```

Expected: open `http://127.0.0.1:8770/ops` — the capture's devices appear in the unassigned list with live HR; enrolling + baselining one makes it go active and appear on the Show puddle. This confirms the whole MQTT path end-to-end with no hardware.

- [ ] **Step 6: Commit**

```bash
git add tools/__init__.py tools/mock_gateway.py tests/test_mock_gateway.py
git commit -m "feat: mock gateway replay mode + end-to-end MQTT validation"
```

---

### Task 6: Mock gateway — bleak mode (real bands, no ESP32)

**Files:**
- Modify: `tools/mock_gateway.py` (add `run_bleak`)
- Test: `tests/test_mock_gateway.py` (append a topic/status-payload builder test)

**Interfaces:**
- Consumes: `DirectBleSource` internals for scanning/connecting (reuse, do not duplicate the BLE loop); `aiomqtt`; the topic builders from Task 5.
- Produces: `run_bleak(broker, port, prefix, gw)` — a real BLE central that republishes raw `0x2A37` notification bytes and status/online per the contract. `status_payload(event, rssi) -> bytes` helper.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mock_gateway.py`:

```python
from tools.mock_gateway import status_payload


def test_status_payload():
    import json as _json
    p = _json.loads(status_payload("connected", -50))
    assert p == {"event": "connected", "rssi": -50}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_mock_gateway.py -k status_payload -v`
Expected: FAIL with `ImportError: cannot import name 'status_payload'`.

- [ ] **Step 3: Implement bleak mode**

In `tools/mock_gateway.py`, add:

```python
def status_payload(event: str, rssi: int | None = None) -> bytes:
    return json.dumps({"event": event, "rssi": rssi}).encode()


async def run_bleak(broker: str, port: int, prefix: str, gw: str) -> None:
    """Real BLE central: forward raw 0x2A37 notifications to MQTT per the contract.

    Reuses DirectBleSource for scan/connect/backoff; we tap its normalized samples
    but re-emit the *raw* frame bytes so the app decodes with its own parser. Since
    DirectBleSource yields decoded NormalizedSample (not raw bytes), we re-encode
    with encode_hr_measurement — lossless for HR/RR/contact, the fields the app uses.
    """
    import aiomqtt

    from cuddle.sources.ble_source import DirectBleSource

    ble = DirectBleSource()
    will = aiomqtt.Will(online_topic(prefix, gw), b"0", qos=1, retain=True)
    async with aiomqtt.Client(broker, port, will=will) as client:
        await client.publish(online_topic(prefix, gw), b"1", qos=1, retain=True)
        await ble.start()
        seen: set[str] = set()
        try:
            async for s in ble.subscribe():
                dev = s.device_id
                if dev not in seen:
                    seen.add(dev)
                    await client.publish(
                        build_status_topic(prefix, gw, dev), status_payload("connected")
                    )
                payload = encode_hr_measurement(
                    s.hr_bpm, rr_intervals=s.rr_intervals, contact=s.contact
                )
                await client.publish(build_hr_topic(prefix, gw, dev), payload)
        finally:
            await ble.stop()
            await client.publish(online_topic(prefix, gw), b"0", qos=1, retain=True)
```

Then update `main()`'s `else` branch:

```python
    else:
        asyncio.run(run_bleak(args.broker, args.port, args.prefix, args.gateway))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_mock_gateway.py -v`
Expected: PASS (all).

- [ ] **Step 5: Manual validation with real bands (documented)**

```bash
mosquitto -p 1883
cuddle --source mqtt --broker 127.0.0.1:1883
python -m tools.mock_gateway --mode bleak
```

Expected: real armbands appear in Ops unassigned with live HR over MQTT; enroll → baseline → active → Show puddle. This proves the full contract with real BLE but no ESP32 — the interface the firmware will target.

- [ ] **Step 6: Commit**

```bash
git add tools/mock_gateway.py tests/test_mock_gateway.py
git commit -m "feat: mock gateway bleak mode (real bands over MQTT)"
```

---

## Notes for the implementer

- `aiomqtt` API: `aiomqtt.Client(host, port, will=...)` is an async context manager; `await client.subscribe("cuddle/#")`; iterate `async for message in client.messages` with `message.topic` (stringify with `str(...)`) and `message.payload` (bytes). `aiomqtt.Will(topic, payload, qos, retain)` sets the last-will. If the installed `aiomqtt` major version differs, adjust the client/iterator idiom but keep the handler layer (`_handle_message`) untouched — that's where all logic and tests live.
- The `_handle_*` handlers are deliberately synchronous and side-effect-contained so the whole source is unit-testable with no broker and no event loop. Preserve that boundary.
- Do NOT touch `hub/`, `processing/`, `transport/`, or the frontends — the whole point is that they don't change.

## Out of scope (separate plans)

- ESP32 firmware and the `max_connections` hardware validation (spec build-order steps 5–6).
- Level B orchestration (`cmd`/`discovery` topics), broker security, provisioning — see `docs/superpowers/roadmap.md`.
