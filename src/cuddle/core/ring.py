"""Fixed-size ring buffers for per-person time series.

Beats arrive irregularly and forever, so every per-person signal is held in a
bounded ring keyed by host time. Two flavors:

- ``TimeSeries`` — (timestamp, value) pairs, used for HR, quality, phase.
- ``BeatBuffer`` — a thin wrapper that also exposes recent RR intervals.

Backed by numpy for cheap windowed slices during correlation.
"""

from __future__ import annotations

import numpy as np


class TimeSeries:
    """A bounded time-stamped scalar series."""

    def __init__(self, capacity: int = 4096) -> None:
        self._cap = capacity
        self._t = np.empty(capacity, dtype=np.float64)
        self._v = np.empty(capacity, dtype=np.float64)
        self._n = 0  # total pushes
        self._start = 0  # index of oldest element in the circular store

    def push(self, t: float, v: float) -> None:
        idx = (self._start + self._size) % self._cap if self._size < self._cap else self._start
        self._t[idx] = t
        self._v[idx] = v
        if self._size < self._cap:
            self._n += 1
        else:
            self._start = (self._start + 1) % self._cap
            self._n += 1

    @property
    def _size(self) -> int:
        return min(self._n, self._cap)

    def __len__(self) -> int:
        return self._size

    def arrays(self) -> tuple[np.ndarray, np.ndarray]:
        """Return (times, values) in chronological order (copies)."""
        n = self._size
        if n == 0:
            return np.empty(0), np.empty(0)
        if n < self._cap:
            return self._t[:n].copy(), self._v[:n].copy()
        idx = (self._start + np.arange(n)) % self._cap
        return self._t[idx].copy(), self._v[idx].copy()

    def window(self, t_from: float, t_to: float) -> tuple[np.ndarray, np.ndarray]:
        """Values with t in [t_from, t_to]."""
        t, v = self.arrays()
        if len(t) == 0:
            return t, v
        mask = (t >= t_from) & (t <= t_to)
        return t[mask], v[mask]

    def latest(self) -> tuple[float, float] | None:
        if self._size == 0:
            return None
        t, v = self.arrays()
        return float(t[-1]), float(v[-1])

    def tail_values(self, k: int) -> list[float]:
        _, v = self.arrays()
        return [float(x) for x in v[-k:]]
