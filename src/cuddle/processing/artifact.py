"""Beat-level artifact correction for optical (PPG) RR streams.

Optical HR bands mis-detect the occasional beat (motion, poor contact), and because
instantaneous HR = 60/RR, one bad interval becomes a large spike. This module cleans
the *beat series* — targeting only statistically impossible beats and interpolating
them — so the genuine slow dynamics the coherence metric reads are preserved. That is
the key property: surgical outlier removal, never blanket smoothing (which would
attenuate and phase-shift the real HR oscillations and hurt cross-person correlation).

Pipeline (order matters — correct beats before resampling/smoothing downstream):
  1. missed/extra-beat repair RR ~k x local median -> split into k; ~0.5x -> merge
  2. plausibility gate        drop RR outside [rr_min, rr_max] (HR ~30..200)
  3. Hampel filter            replace RR > n_sigma * MAD from the local median
Steps 1 and 3 replace values from local context (median), which is a robust interpolation
that keeps the series continuous. Grounded in standard HRV practice (Kubios threshold /
cubic-spline correction; Hampel/MAD spike removal for PPG).

Repair runs *before* the plausibility gate on purpose: a genuine missed beat at a
resting rate produces an interval above ``rr_max``, so gating first deleted exactly the
intervals repair exists to fix.

What this module will not do is invent beats it cannot justify. An interval spanning
more than ``max_split`` beats is a dropout, not a mis-detection, so it is dropped rather
than subdivided — leaving a hole that the resampler renders as NaN and the metrics
exclude. Guessing there is worse than admitting the gap.
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


def _repair_missed_extra(
    t: np.ndarray, rr: np.ndarray, ratio: float, med: float, max_split: int = 3
) -> tuple[np.ndarray, np.ndarray, int]:
    """Split a long interval into the k beats it spans; merge ~0.5x (an extra beat).

    ``k = round(rr / median)``. Splitting every long interval into exactly two beats
    fabricates a large HR dip whenever more than one beat was missed — two missed beats
    at 67 bpm came out as two beats at 44 bpm, a 23 bpm excursion that never happened,
    fed straight into the correlation. Past ``max_split`` the interval is a dropout
    rather than a mis-detection, so it is dropped and the hole left visible.

    Returns (t, rr, n_dropouts).
    """
    if rr.size < 3 or med <= 0:
        return t, rr, 0
    out_t: list[float] = []
    out_rr: list[float] = []
    dropouts = 0
    for i in range(rr.size):
        v = float(rr[i])
        if v > ratio * med:  # missed beat(s): one long interval -> k beats
            k = max(2, int(round(v / med)))
            if k > max_split:
                dropouts += 1  # too much missing to reconstruct: leave the gap
                continue
            step = v / k
            for j in range(k - 1, -1, -1):
                out_t.append(float(t[i]) - step * j)
                out_rr.append(step)
        elif v < med / ratio and out_rr:  # extra beat: merge with previous
            out_rr[-1] = out_rr[-1] + v
            out_t[-1] = float(t[i])
        else:
            out_t.append(float(t[i]))
            out_rr.append(v)
    return np.asarray(out_t), np.asarray(out_rr), dropouts


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
    max_split: int = 3,
) -> tuple[np.ndarray, np.ndarray, int]:
    """Return (t_corrected, rr_corrected, n_artifacts). Beat times and RR stay aligned.

    ``min_frac`` is the absolute-deviation floor as a fraction of the median RR (Malik
    ~20%): a beat is only corrected if it also jumps this much, so resting/flat signals
    are not over-smoothed.

    ``max_split`` caps how many beats one long interval may be reconstructed into;
    beyond it the interval is treated as a dropout and dropped (see module docstring).
    """
    t = np.asarray(t, dtype=float)
    rr = np.asarray(rr, dtype=float)
    if rr.size == 0:
        return t, rr, 0

    # Reference median from plausible beats only, so one absurd interval (a dropout
    # arriving as a single huge RR) can't drag the scale that repair measures against.
    plausible = rr[(rr >= rr_min) & (rr <= rr_max)]
    med = float(np.median(plausible)) if plausible.size else float(np.median(rr))

    dropouts = 0
    if repair:
        t, rr, dropouts = _repair_missed_extra(t, rr, repair_ratio, med, max_split)

    keep = (rr >= rr_min) & (rr <= rr_max)
    dropped = int((~keep).sum())
    t, rr = t[keep], rr[keep]
    if rr.size == 0:
        return t, rr, dropped + dropouts

    min_abs = min_frac * float(np.median(rr))
    rr_h, replaced = hampel(rr, window=hampel_window, n_sigma=hampel_sigma, min_abs=min_abs)
    n_artifacts = dropped + dropouts + int(replaced.sum())
    return t, rr_h, n_artifacts
