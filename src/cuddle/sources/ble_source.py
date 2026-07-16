"""Direct-to-Mac BLE ingestion via ``bleak`` (CoreBluetooth on macOS).

Continuously scans for peripherals advertising the standard Heart Rate Service
(0x180D), auto-connects to each, subscribes to Heart Rate Measurement (0x2A37),
and emits ``NormalizedSample``. Dropouts are expected: each device gets a supervised
task that reconnects with exponential backoff + jitter, indefinitely, so a band that
roams out of range rejoins on its own.

Note: macOS CoreBluetooth holds only ~7-10 peripherals at once, which is why the
30-person system uses BLE->WiFi gateways instead. This source is Phase 1 only.
"""

from __future__ import annotations

import asyncio
import contextlib
import random

from cuddle.core import clock
from cuddle.core.models import ConnectionState, DeviceInfo, NormalizedSample, Source
from cuddle.sources.ble_parser import parse_hr_measurement

HR_SERVICE = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"


class DirectBleSource:
    """A ``SampleSource`` backed by real Coospo/standard BLE HR armbands."""

    def __init__(
        self,
        *,
        backoff_start: float = 1.0,
        backoff_max: float = 16.0,
        jitter: float = 0.3,
        scan_interval: float = 5.0,
    ) -> None:
        self._queue: asyncio.Queue[NormalizedSample] = asyncio.Queue()
        self._backoff_start = backoff_start
        self._backoff_max = backoff_max
        self._jitter = jitter
        self._scan_interval = scan_interval

        self._states: dict[str, ConnectionState] = {}
        self._bindings: dict[str, str] = {}  # device_id -> person_id
        self._seq: dict[str, int] = {}
        self._last_hr: dict[str, int] = {}
        self._rssi: dict[str, int | None] = {}
        self._device_tasks: dict[str, asyncio.Task] = {}
        self._scan_task: asyncio.Task | None = None
        self._running = False

    # ---- SampleSource protocol ------------------------------------------

    async def start(self) -> None:
        self._running = True
        self._scan_task = asyncio.create_task(self._scan_loop(), name="ble-scan")

    async def stop(self) -> None:
        self._running = False
        tasks = list(self._device_tasks.values())
        if self._scan_task:
            tasks.append(self._scan_task)
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._device_tasks.clear()

    async def subscribe(self):
        while True:
            yield await self._queue.get()

    @property
    def connection_states(self) -> dict[str, ConnectionState]:
        return dict(self._states)

    def unassigned_devices(self) -> list[DeviceInfo]:
        out = []
        for dev, state in self._states.items():
            if dev not in self._bindings:
                out.append(
                    DeviceInfo(
                        device_id=dev,
                        source=Source.ble,
                        hr_bpm=self._last_hr.get(dev),
                        connection=state,
                        rssi=self._rssi.get(dev),
                    )
                )
        return out

    def bind(self, device_id: str, person_id: str) -> None:
        self._bindings[device_id] = person_id

    def unbind(self, device_id: str) -> None:
        self._bindings.pop(device_id, None)

    # ---- internals ------------------------------------------------------

    async def _scan_loop(self) -> None:
        from bleak import BleakScanner

        while self._running:
            try:
                devices = await BleakScanner.discover(
                    timeout=self._scan_interval, service_uuids=[HR_SERVICE]
                )
                for d in devices:
                    dev_id = d.address
                    rssi = getattr(d, "rssi", None)
                    self._rssi[dev_id] = rssi
                    if dev_id not in self._device_tasks:
                        self._states.setdefault(dev_id, ConnectionState.connecting)
                        self._device_tasks[dev_id] = asyncio.create_task(
                            self._device_loop(dev_id), name=f"ble-dev-{dev_id}"
                        )
            except Exception:
                await asyncio.sleep(self._scan_interval)

    async def _device_loop(self, device_id: str) -> None:
        from bleak import BleakClient

        backoff = self._backoff_start
        while self._running:
            self._states[device_id] = ConnectionState.connecting
            try:
                disconnected = asyncio.Event()

                def _on_disconnect(_client) -> None:
                    disconnected.set()

                async with BleakClient(
                    device_id, disconnected_callback=_on_disconnect
                ) as client:
                    self._states[device_id] = ConnectionState.connected
                    backoff = self._backoff_start  # reset on a clean connect

                    def _on_notify(_char, data: bytearray) -> None:
                        self._handle_notification(device_id, bytes(data))

                    await client.start_notify(HR_MEASUREMENT, _on_notify)
                    await disconnected.wait()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

            if not self._running:
                break
            self._states[device_id] = ConnectionState.reconnecting
            sleep = backoff * (1.0 + random.uniform(-self._jitter, self._jitter))
            await asyncio.sleep(max(0.1, sleep))
            backoff = min(self._backoff_max, backoff * 2.0)

        self._states[device_id] = ConnectionState.disconnected

    def _handle_notification(self, device_id: str, data: bytes) -> None:
        try:
            m = parse_hr_measurement(data)
        except ValueError:
            return
        self._last_hr[device_id] = m.hr_bpm
        self._seq[device_id] = self._seq.get(device_id, 0) + 1
        person_id = self._bindings.get(device_id, device_id)
        sample = NormalizedSample(
            person_id=person_id,
            device_id=device_id,
            source=Source.ble,
            t_recv=clock.now(),
            hr_bpm=m.hr_bpm,
            rr_intervals=m.rr_intervals,
            contact=m.contact,
            raw_flags=m.flags,
            seq=self._seq[device_id],
        )
        self._queue.put_nowait(sample)
