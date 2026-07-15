"""Monotonic host clock — the Phase-1 master clock for cross-person alignment.

The BLE Heart Rate Service gives no absolute device timestamp, so we stamp every
sample with a high-resolution monotonic host time on receipt. RR intervals
reconstruct beat timing *within* a person; this clock aligns *across* people.

Wrapped in one module so tests can monkeypatch a deterministic clock, and so the
gateway phase can later swap in a network-synchronized time source.
"""

from __future__ import annotations

import time


def now() -> float:
    """Seconds from an arbitrary epoch, monotonic (never goes backwards)."""
    return time.monotonic()
