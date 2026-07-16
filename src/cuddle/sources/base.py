"""The load-bearing interface: every ingestion source implements ``SampleSource``.

Direct BLE (now), the simulator (dev/demo), and the future gateway/MQTT source all
implement exactly this Protocol. The hub depends only on it, so swapping the
ingestion source is a one-line change and Layers 1-4 never move.
"""

from __future__ import annotations

from typing import AsyncIterator, Protocol, runtime_checkable

from cuddle.core.models import ConnectionState, DeviceInfo, NormalizedSample


@runtime_checkable
class SampleSource(Protocol):
    async def start(self) -> None:
        """Begin scanning/connecting and producing samples."""
        ...

    async def stop(self) -> None:
        """Tear down all links and stop producing."""
        ...

    def subscribe(self) -> AsyncIterator[NormalizedSample]:
        """Async-iterate normalized samples as they arrive."""
        ...

    @property
    def connection_states(self) -> dict[str, ConnectionState]:
        """Current link state per device_id."""
        ...

    def unassigned_devices(self) -> list[DeviceInfo]:
        """Devices seen but not yet bound to a person (drives Ops enrollment list)."""
        ...

    def bind(self, device_id: str, person_id: str) -> None:
        """Tell the source which person a device belongs to.

        After binding, samples from ``device_id`` carry ``person_id``. Before
        binding they carry a provisional id equal to the device_id.
        """
        ...

    def unbind(self, device_id: str) -> None:
        """Forget a device's person binding, returning it to the unassigned pool."""
        ...
