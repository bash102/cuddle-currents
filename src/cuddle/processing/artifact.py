"""Beat-level artifact correction for optical (PPG) RR streams.

Optical HR bands mis-detect the occasional beat (motion, poor contact), and because
instantaneous HR = 60/RR, one bad interval becomes a large spike. This module cleans
the *beat series* — targeting only statistically impossible beats and interpolating
them — so the genuine slow dynamics the coherence metric reads are preserved. That is
the key property: surgical outlier removal, never blanket smoothing (which would
attenuate and phase-shift the real HR oscillations and hurt cross-person correlation).

Pipeline (order matters — correct beats before resampling/smoothing downstream):
  1. plausibility gate        drop RR outside [rr_min, rr_max] (HR ~30..200)
  2. missed/extra-beat repair RR ~2x local median -> split; ~0.5x -> merge
  3. Hampel filter            replace RR > n_sigma * MAD from the local median
Steps 2-3 replace values from local context (median), which is a robust interpolation
that keeps the series continuous. Grounded in standard HRV practice (Kubios threshold /
cubic-spline correction; Hampel/MAD spike removal for PPG).
"""

from __future__ import annotations

import numpy as np

MAD_TO_SIGMA = 1.4826  # MAD -> Gaussian-equivalent SD


def hampel(
    x: np.ndarray, window: int = 5, n_sigma: float = 3.0, min_abs: float = 0.0
) -> tuple[np.ndarray, np.ndarray]:
    """Hampel filter: replace points that are outliers vs the local median.

    A point is an artifact only if it deviates by more than BOTH ``n_sigma`` robust
    SDs AND ``min_abs`` (an absolute floor). The floor is essential on low-variability
    (resting) signals: there the MAD is tiny, so a pure n_sigma test flags genuine
    beat-to-beat variation and flattens the real signal — the floor keeps correction
    to physiologically large jumps (a spike), leaving normal variation untouched.

    Returns (cleaned, replaced_mask).
    """
    x = np.asarray(x, dtype=float).copy()
    n = x.size
    replaced = np.zeros(n, dtype=bool)
    if n < 3:
        return x, replaced
    k = max(1, window // 2)
    for i in range(n):
        lo, hi = max(0, i - k), min(n, i + k + 1)
        w = x[lo:hi]
        med = np.median(w)
        sigma = MAD_TO_SIGMA * np.median(np.abs(w - med))
        thresh = max(n_sigma * sigma, min_abs)
        if thresh > 0 and abs(x[i] - med) > thresh:
            x[i] = med
            replaced[i] = True
    return x, replaced


def _repair_missed_extra(t: np.ndarray, rr: np.ndarray, ratio: float) -> tuple[np.ndarray, np.ndarray]:
    """Split ~2x intervals (a missed beat) and merge ~0.5x intervals (an extra beat)."""
    if rr.size < 3:
        return t, rr
    med = float(np.median(rr))
    out_t: list[float] = []
    out_rr: list[float] = []
    i = 0
    while i < rr.size:
        v = rr[i]
        if v > ratio * med:  # missed beat: one long gap -> two beats
            half = v / 2.0
            out_t.extend([t[i] - half, t[i]])
            out_rr.extend([half, half])
        elif v < med / ratio and out_rr:  # extra beat: merge with previous
            out_rr[-1] = out_rr[-1] + v
            out_t[-1] = t[i]
        else:
            out_t.append(t[i])
            out_rr.append(v)
        i += 1
    return np.asarray(out_t), np.asarray(out_rr)


def correct_rr(
    t: np.ndarray,
    rr: np.ndarray,
    *,
    rr_min: float = 0.30,
    rr_max: float = 2.00,
    hampel_window: int = 5,
    hampel_sigma: float = 3.0,
    min_frac: float = 0.20,
    repair_ratio: float = 1.75,
    repair: bool = True,
) -> tuple[np.ndarray, np.ndarray, int]:
    """Return (t_corrected, rr_corrected, n_artifacts). Beat times and RR stay aligned.

    ``min_frac`` is the absolute-deviation floor as a fraction of the median RR (Malik
    ~20%): a beat is only corrected if it also jumps this much, so resting/flat signals
    are not over-smoothed.
    """
    t = np.asarray(t, dtype=float)
    rr = np.asarray(rr, dtype=float)
    if rr.size == 0:
        return t, rr, 0

    keep = (rr >= rr_min) & (rr <= rr_max)
    dropped = int((~keep).sum())
    t, rr = t[keep], rr[keep]
    if rr.size == 0:
        return t, rr, dropped

    if repair:
        t, rr = _repair_missed_extra(t, rr, repair_ratio)

    min_abs = min_frac * float(np.median(rr))
    rr_h, replaced = hampel(rr, window=hampel_window, n_sigma=hampel_sigma, min_abs=min_abs)
    n_artifacts = dropped + int(replaced.sum())
    return t, rr_h, n_artifacts
