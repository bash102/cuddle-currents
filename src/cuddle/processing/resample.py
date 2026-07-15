"""Resample irregular beat series onto a uniform grid.

Beats arrive whenever a heart beats, but cross-person correlation needs both people
sampled on the *same* clock at the *same* rate. Everything downstream that compares
people first passes through here.
"""

from __future__ import annotations

import numpy as np


def uniform_grid(t_from: float, t_to: float, hz: float) -> np.ndarray:
    if t_to <= t_from:
        return np.empty(0)
    return np.arange(t_from, t_to, 1.0 / hz)


def resample(t: np.ndarray, v: np.ndarray, grid: np.ndarray) -> np.ndarray:
    """Linear interpolation of (t, v) onto grid. Edges clamp to first/last value."""
    if t.size == 0 or grid.size == 0:
        return np.full(grid.shape, np.nan)
    return np.interp(grid, t, v)


def coverage(t: np.ndarray, grid: np.ndarray, max_gap: float) -> float:
    """Fraction of grid points within ``max_gap`` seconds of an actual sample."""
    if t.size == 0 or grid.size == 0:
        return 0.0
    idx = np.searchsorted(t, grid)
    idx = np.clip(idx, 1, len(t) - 1)
    left = np.abs(grid - t[idx - 1])
    right = np.abs(t[np.clip(idx, 0, len(t) - 1)] - grid)
    nearest = np.minimum(left, right)
    return float(np.mean(nearest <= max_gap))


def ema(values: np.ndarray, dt: float, tau: float) -> np.ndarray:
    """Exponential moving average over a uniform series (time constant tau seconds)."""
    if values.size == 0 or tau <= 0:
        return values
    alpha = 1.0 - np.exp(-dt / tau)
    out = np.empty_like(values)
    acc = values[0]
    for i, x in enumerate(values):
        acc += alpha * (x - acc)
        out[i] = acc
    return out
