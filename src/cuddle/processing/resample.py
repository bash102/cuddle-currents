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


def nearest_distance(t: np.ndarray, grid: np.ndarray) -> np.ndarray:
    """Seconds from each grid point to the closest actual sample in ``t``."""
    if t.size == 0 or grid.size == 0:
        return np.full(grid.shape, np.inf)
    idx = np.searchsorted(t, grid)
    lo = np.clip(idx - 1, 0, t.size - 1)
    hi = np.clip(idx, 0, t.size - 1)
    return np.minimum(np.abs(grid - t[lo]), np.abs(t[hi] - grid))


def resample(
    t: np.ndarray, v: np.ndarray, grid: np.ndarray, max_gap: float | None = None
) -> np.ndarray:
    """Linear interpolation of (t, v) onto grid. Edges clamp to first/last value.

    ``max_gap`` (seconds) refuses to bridge a dropout: grid points further than that
    from any real sample come back NaN instead of a straight line drawn across the
    hole. That line is not missing data, it is *invented* data — and it is invented in
    exactly the shape (a smooth ramp) that the correlation metric reads as signal. A
    25 s hole inside the 30 s sync window took a genuine +0.92 concordance down to
    +0.07, i.e. the visualization confidently reported "these two are not in sync"
    about a stretch where one of them simply wasn't there. Everything downstream is
    NaN-aware (see ``synchrony._ccc_matrix``/``_plv_matrix``), so a gap is *excluded*
    from the estimate rather than poisoning it.
    """
    if t.size == 0 or grid.size == 0:
        return np.full(grid.shape, np.nan)
    out = np.interp(grid, t, v)
    if max_gap is not None and max_gap > 0:
        out = np.where(nearest_distance(t, grid) <= max_gap, out, np.nan)
    return out


def coverage(t: np.ndarray, grid: np.ndarray, max_gap: float) -> float:
    """Fraction of grid points within ``max_gap`` seconds of an actual sample."""
    if t.size == 0 or grid.size == 0:
        return 0.0
    return float(np.mean(nearest_distance(t, grid) <= max_gap))


def ema(values: np.ndarray, dt: float, tau: float) -> np.ndarray:
    """Exponential moving average over a uniform series (time constant tau seconds).

    NaN-aware: gaps stay gaps (a plain recursion would smear one NaN over the whole
    remaining series), and the accumulator restarts on the far side rather than
    carrying pre-gap state across a stretch where nothing was measured.
    """
    if values.size == 0 or tau <= 0:
        return values
    alpha = 1.0 - np.exp(-dt / tau)
    out = np.empty_like(values)
    acc = np.nan
    for i, x in enumerate(values):
        if not np.isfinite(x):
            out[i] = np.nan
            acc = np.nan
            continue
        acc = x if not np.isfinite(acc) else acc + alpha * (x - acc)
        out[i] = acc
    return out
