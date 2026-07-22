"""Ingest from BLE->WiFi gateways over MQTT (Phase 2).

Gateways publish raw 0x2A37 HR bytes, per-device status events, and an `online`
LWT. This source decodes with the shared ble_parser, keys everything by the band's
device_id (gateway is routing only), and synthesizes ConnectionState from the
explicit status events plus silence/LWT backstops. Everything downstream consumes
NormalizedSample and never learns the origin.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from cuddle.core import clock
from cuddle.core.models import ConnectionState, DeviceInfo, NormalizedSample, Source
from cuddle.sources.ble_parser import parse_hr_measurement

logger = logging.getLogger(__name__)


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
        hr_holder_ttl: float = 3.0,
    ) -> None:
        self._broker = broker
        self._port = port
        self._prefix = topic_prefix
        self._drop_after = drop_after
        self._evict_after = evict_after
        self._stale_after_rr_factor = stale_after_rr_factor
        # A multi-connect band (e.g. Scosche Rhythm+) can be connected to two gateways at
        # once, so both publish its HR. We accept beats from only ONE gateway per device (the
        # "holder"); a different gateway's beats are dropped as duplicates. If the holder goes
        # silent for hr_holder_ttl, the next gateway to send HR takes over (failover).
        self._hr_holder_ttl = hr_holder_ttl

        self._queue: asyncio.Queue[NormalizedSample] = asyncio.Queue()
        self._states: dict[str, ConnectionState] = {}
        self._bindings: dict[str, str] = {}  # device_id -> person_id
        self._hr_holder: dict[str, tuple[str, float]] = {}  # device_id -> (gateway, last hr time)
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
        # Multi-connect dedup: accept beats from only one gateway per device. A different
        # gateway's beats are dropped while the holder is fresh (else the person double-counts
        # every beat, corrupting HR/RMSSD). Connection state above stays fresh either way.
        holder = self._hr_holder.get(dev)
        if holder is not None and holder[0] != gw and (now - holder[1]) <= self._hr_holder_ttl:
            return
        self._hr_holder[dev] = (gw, now)
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
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            return
        if not isinstance(data, dict):
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
        for d in (self._states, self._seq, self._last_hr, self._rssi, self._last_seen,
                  self._device_gw, self._hr_holder):
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

    # ---- reaper / client loop --------------------------------------------

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
                logger.debug("mqtt client loop error; retrying", exc_info=True)
                await asyncio.sleep(1.0)  # broker unreachable; retry
