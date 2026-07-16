"""Per-person 'abstract' signal: smoothed HR, rolling RMSSD, and oscillator phase.

These are the derived representations the Abstract panel shows and that synchrony is
computed from. Kept as small pure functions over a ``PersonSession`` so both the
frame builder and the synchrony stage can reuse them.
"""

from __future__ import annotations

import numpy as np

from cuddle.hub.registry import PersonSession
from cuddle.processing.artifact import correct_rr
from cuddle.processing.baseline import rmssd
from cuddle.processing.resample import ema, resample, uniform_grid

# Extra look-back so the Hampel window has context at the window's left edge.
_ART_PREROLL = 5.0


def corrected_beats(
    session: PersonSession, t_from: float, t_to: float, art: dict | None
) -> tuple[np.ndarray, np.ndarray]:
    """Beat (t, RR) over a window, artifact-corrected if ``art`` is enabled."""
    t, rr = session.rr.window(t_from - _ART_PREROLL, t_to)
    if art and art.get("enabled", True) and rr.size:
        t, rr, _ = correct_rr(
            t, rr,
            rr_min=art.get("rr_min", 0.30), rr_max=art.get("rr_max", 2.00),
            hampel_window=art.get("hampel_window", 5),
            hampel_sigma=art.get("hampel_sigma", 3.0),
            min_frac=art.get("min_frac", 0.20),
            repair=art.get("repair", True),
        )
    m = (t >= t_from) & (t <= t_to)
    return t[m], rr[m]


def smoothed_hr_grid(
    session: PersonSession, t_from: float, t_to: float, hz: float, tau: float,
    art: dict | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Instantaneous HR (artifact-corrected) resampled onto a uniform grid, EMA-smoothed."""
    grid = uniform_grid(t_from, t_to, hz)
    if art is not None:
        t, rr = corrected_beats(session, t_from, t_to, art)
        t, v = t, (60.0 / rr if rr.size else rr)
    else:
        t, v = session.inst_hr.arrays()
    if grid.size == 0 or t.size == 0:
        return grid, np.full(grid.shape, np.nan)
    series = resample(t, v, grid)
    smooth = ema(series, 1.0 / hz, tau)
    return grid, smooth


def current_hr(session: PersonSession, tau: float, art: dict | None = None) -> float | None:
    """Latest smoothed instantaneous HR."""
    t, v = session.inst_hr.arrays()
    if t.size == 0:
        return None
    if t.size == 1:
        return float(v[-1])
    window = min(15.0, float(t[-1] - t[0]))
    grid, smooth = smoothed_hr_grid(session, t[-1] - window, t[-1], 4.0, tau, art)
    if smooth.size == 0 or np.isnan(smooth[-1]):
        return float(v[-1])
    return float(smooth[-1])


def windowed_hr_std(
    session: PersonSession, now: float, window: float, hz: float, tau: float,
    art: dict | None = None,
) -> float | None:
    """SD (bpm) of the smoothed HR over the sync window.

    This is exactly the amplitude that shape-based synchrony (zscore/Pearson) divides
    out. When it's near the sensor noise floor (~<1-2 bpm) the HR is essentially flat,
    so zscore correlates noise and is unreliable — raw/level agreement is what to trust.
    """
    _, hr = smoothed_hr_grid(session, now - window, now, hz, tau, art)
    finite = hr[np.isfinite(hr)]
    if finite.size < 3:
        return None
    return float(np.std(finite))


def rolling_rmssd(
    session: PersonSession, now: float, window: float, art: dict | None = None
) -> float | None:
    if art is not None:
        _, rr = corrected_beats(session, now - window, now, art)
    else:
        _, rr = session.rr.window(now - window, now)
    if rr.size < 2:
        return None
    return rmssd(rr)


def rmssd_delta(
    session: PersonSession, now: float, window: float, art: dict | None = None
) -> float | None:
    """RMSSD relative to the person's own baseline (%). Requires calibration."""
    base = session.profile.calibration.hrv_baseline
    cur = rolling_rmssd(session, now, window, art)
    if base is None or cur is None or base <= 0:
        return None
    return (cur - base) / base * 100.0


def phase_at(session: PersonSession, now: float) -> float | None:
    """Beat-interpolated oscillator phase in [0, 2*pi).

    Phase advances linearly from 0 at the last beat toward 2*pi at the next expected
    beat, using the most recent RR as the period estimate. Makes each person a
    rotating phasor for the puddle and for PLV.
    """
    latest = session.rr.latest()
    if latest is None:
        return None
    t_last, rr = latest
    if rr <= 0:
        return None
    frac = (now - t_last) / rr
    return float((2.0 * np.pi * frac) % (2.0 * np.pi))


def phase_grid(session: PersonSession, grid: np.ndarray) -> np.ndarray:
    """Phase series over a grid via beat interpolation; NaN where no beat bracket."""
    t, _ = session.rr.arrays()
    if t.size < 2 or grid.size == 0:
        return np.full(grid.shape, np.nan)
    out = np.full(grid.shape, np.nan)
    idx = np.searchsorted(t, grid)
    for i, (g, k) in enumerate(zip(grid, idx)):
        if k <= 0 or k >= t.size:
            continue
        t0, t1 = t[k - 1], t[k]
        if t1 <= t0:
            continue
        out[i] = (2.0 * np.pi * (g - t0) / (t1 - t0)) % (2.0 * np.pi)
    return out
