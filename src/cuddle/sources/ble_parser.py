"""Pure decoder for the standard BLE Heart Rate Measurement characteristic (0x2A37).

Both the Coospo HW706 and HW9 use the standard GATT Heart Rate Service; there is no
proprietary framing. This module is deliberately dependency-free and side-effect-free
so it can be exhaustively golden-tested against hand-built byte vectors.

Payload layout (Bluetooth SIG Heart Rate Measurement):
  byte 0: flags
    bit 0  HR value format   0 = uint8, 1 = uint16
    bit 1  sensor contact status (meaningful only if bit 2 set)
    bit 2  sensor contact supported
    bit 3  energy expended present (uint16, kJ) — skipped
    bit 4  RR-interval(s) present
  HR value: uint8 or uint16 LE per bit 0
  [energy expended: uint16 LE] if bit 3
  RR intervals: zero or more uint16 LE, units of 1/1024 second
"""

from __future__ import annotations

from dataclasses import dataclass, field

RR_UNIT = 1.0 / 1024.0


@dataclass
class HeartRateMeasurement:
    hr_bpm: int
    rr_intervals: list[float] = field(default_factory=list)  # seconds
    contact: bool | None = None  # None if sensor-contact not supported
    energy_expended: int | None = None
    flags: int = 0


def parse_hr_measurement(data: bytes | bytearray) -> HeartRateMeasurement:
    """Decode a 0x2A37 notification payload. Raises ValueError on malformed input."""
    if len(data) < 2:
        raise ValueError(f"HR measurement too short: {len(data)} bytes")

    flags = data[0]
    hr16 = bool(flags & 0x01)
    contact_supported = bool(flags & 0x04)
    contact_status = bool(flags & 0x02)
    energy_present = bool(flags & 0x08)
    rr_present = bool(flags & 0x10)

    idx = 1
    if hr16:
        if len(data) < idx + 2:
            raise ValueError("HR measurement claims 16-bit HR but is truncated")
        hr = data[idx] | (data[idx + 1] << 8)
        idx += 2
    else:
        hr = data[idx]
        idx += 1

    energy: int | None = None
    if energy_present:
        if len(data) < idx + 2:
            raise ValueError("HR measurement claims energy expended but is truncated")
        energy = data[idx] | (data[idx + 1] << 8)
        idx += 2

    rr: list[float] = []
    if rr_present:
        remaining = len(data) - idx
        # Each RR is 2 bytes; ignore a stray trailing byte defensively.
        for j in range(idx, idx + (remaining // 2) * 2, 2):
            raw = data[j] | (data[j + 1] << 8)
            rr.append(raw * RR_UNIT)

    contact = contact_status if contact_supported else None

    return HeartRateMeasurement(
        hr_bpm=hr,
        rr_intervals=rr,
        contact=contact,
        energy_expended=energy,
        flags=flags,
    )
