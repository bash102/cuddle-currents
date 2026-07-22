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
        drop_after: float = 20.0,
        evict_after: float = 120.0,
        stale_after_rr_factor: float = 2.5,
    ) -> None:
        self._queue: asyncio.Queue[NormalizedSample] = asyncio.Queue()
        self._backoff_start = backoff_start
        self._backoff_max = backoff_max
        self._jitter = jitter
        self._scan_interval = scan_interval
        self._drop_after = drop_after
        self._evict_after = evict_after
        self._stale_after_rr_factor = stale_after_rr_factor

        self._states: dict[str, ConnectionState] = {}
        self._bindings: dict[str, str] = {}  # device_id -> person_id
        self._seq: dict[str, int] = {}
        self._last_hr: dict[str, int] = {}
        self._rssi: dict[str, int | None] = {}
        self._last_seen: dict[str, float] = {}  # device_id -> time of last notification
        self._last_connect: dict[str, float] = {}  # device_id -> time of last connect
        self._discovered_at: dict[str, float] = {}  # device_id -> first-seen time
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
        # Report the *effective* link state (raw lifecycle overlaid with
        # silence/staleness), not the raw retry-loop state, so a roamed-out band
        # surfaces as ``disconnected`` even while its task keeps retrying.
        now = clock.now()
        return {dev: self._link_state(dev, now) for dev in self._states}

    def unassigned_devices(self) -> list[DeviceInfo]:
        now = clock.now()
        out = []
        for dev in self._states:
            if dev not in self._bindings:
                out.append(
                    DeviceInfo(
                        device_id=dev,
                        source=Source.ble,
                        hr_bpm=self._last_hr.get(dev),
                        connection=self._link_state(dev, now),
                        rssi=self._rssi.get(dev),
                    )
                )
        return out

    def bind(self, device_id: str, person_id: str) -> None:
        self._bindings[device_id] = person_id

    def unbind(self, device_id: str) -> None:
        self._bindings.pop(device_id, None)

    # ---- link-state / eviction helpers (pure, testable) -----------------

    def _last_healthy(self, device_id: str) -> float | None:
        """Most recent evidence of a working link: last beat or last connect."""
        times = [
            t
            for t in (self._last_seen.get(device_id), self._last_connect.get(device_id))
            if t is not None
        ]
        return max(times) if times else None

    def _link_state(self, device_id: str, now: float) -> ConnectionState:
        """Effective link state for a device at ``now``.

        Overlays silence/staleness on the raw retry-loop lifecycle:
        - a *connected* link whose beats lag reads ``stale``; one silent past
          ``drop_after`` reads ``disconnected``;
        - a *reconnecting/connecting* link that has been failing/silent past
          ``drop_after`` reads ``disconnected`` (issue 1) — the supervising task
          keeps retrying at ``backoff_max`` in the background so it can rejoin.
        """
        raw = self._states.get(device_id, ConnectionState.disconnected)

        if raw == ConnectionState.connected:
            last = self._last_seen.get(device_id)
            if last is None:
                return raw  # freshly connected, no beat yet
            silent = now - last
            if silent > self._drop_after:
                return ConnectionState.disconnected
            hr = self._last_hr.get(device_id)
            expected = 60.0 / hr if hr else 1.0
            if silent > self._stale_after_rr_factor * expected:
                return ConnectionState.stale
            return ConnectionState.connected

        # Not currently connected. Keep showing the transient retry state until
        # we've been without a healthy link past drop_after, then call it dropped.
        healthy = self._last_healthy(device_id)
        if healthy is None:
            disc = self._discovered_at.get(device_id)
            if disc is None or (now - disc) > self._drop_after:
                return ConnectionState.disconnected
            return raw  # still within the initial connect grace
        if (now - healthy) > self._drop_after:
            return ConnectionState.disconnected
        return raw

    def _evictable(self, now: float) -> list[str]:
        """Unbound devices absent (no beat) longer than ``evict_after``.

        A bound device is never evictable — an enrolled person's band may return.
        """
        out: list[str] = []
        for dev in self._states:
            if dev in self._bindings:
                continue
            ref = self._last_seen.get(dev)
            if ref is None:
                ref = self._discovered_at.get(dev)
            if ref is None:
                continue
            if (now - ref) > self._evict_after:
                out.append(dev)
        return out

    def _evict(self, device_id: str) -> None:
        """Cancel a device's task and purge all per-device bookkeeping."""
        task = self._device_tasks.pop(device_id, None)
        if task is not None:
            task.cancel()
        for d in (
            self._states,
            self._seq,
            self._last_hr,
            self._rssi,
            self._last_seen,
            self._last_connect,
            self._discovered_at,
        ):
            d.pop(device_id, None)

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
                        self._discovered_at[dev_id] = clock.now()
                        self._states.setdefault(dev_id, ConnectionState.connecting)
                        self._device_tasks[dev_id] = asyncio.create_task(
                            self._device_loop(dev_id), name=f"ble-dev-{dev_id}"
                        )
            except Exception:
                await asyncio.sleep(self._scan_interval)

            # Reap unbound, long-absent devices so state doesn't grow unbounded.
            for dev_id in self._evictable(clock.now()):
                self._evict(dev_id)

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
                    self._last_connect[device_id] = clock.now()
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
        self._last_seen[device_id] = clock.now()
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
