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
