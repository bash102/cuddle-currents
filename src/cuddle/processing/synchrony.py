"""Cross-person synchrony: the payoff metric feeding the alignment view.

Two complementary measures over a sliding window on a common uniform grid:

- **Concordance (matrix)** — Lin's concordance correlation coefficient (CCC) of the
  smoothed-HR series. CCC rewards two people whose HR *moves together*; unlike plain
  Pearson it is NOT affine-invariant, so the per-person normalization applied first
  genuinely changes what "together" means:
    raw            CCC on absolute HR — two people at 55 and 80 bpm score low even if
                   their shapes match (level gap counts against agreement).
    zscore         CCC on each series standardized by ITS OWN window mean/std. That
                   makes every series mean-0 / var-1, so CCC collapses to Pearson
                   correlation — a pure, offset- and scale-invariant shape match, and
                   the robust default. (Calibration-independent.)
    baseline_delta CCC on (HR - personal resting HR): offset removed but bpm scale
                   kept, so it rewards people whose departures from their OWN rest
                   co-move even from different resting points. Needs the baseline.
  Note: earlier this standardized zscore by the *resting* baseline SD, which — paired
  with CCC — manufactured per-person variance/offset mismatch and made zscore score
  worst; window-based standardization fixes that.

- **Phase-locking (PLV) + Kuramoto order parameter** — from beat-interpolated phase.
  Offset-robust by construction, so it cross-validates the concordance matrix and is
  largely mode-independent. The order parameter R is the single group-cohesion scalar
  the simulator's Kuramoto coupling drives.
"""

from __future__ import annotations

import numpy as np

from cuddle.core.models import EnrollmentState
from cuddle.processing.abstract import phase_grid, smoothed_hr_grid
from cuddle.processing.resample import uniform_grid


def ccc(x: np.ndarray, y: np.ndarray) -> float:
    """Lin's concordance correlation coefficient over paired samples."""
    if x.size < 3:
        return 0.0
    mx, my = x.mean(), y.mean()
    vx, vy = x.var(), y.var()
    cov = np.mean((x - mx) * (y - my))
    denom = vx + vy + (mx - my) ** 2
    if denom <= 1e-12:
        return 0.0
    return float(2.0 * cov / denom)


def _transform(series: np.ndarray, mode: str, cal) -> np.ndarray:
    finite = series[np.isfinite(series)]
    if finite.size == 0:
        return series
    if mode == "raw":
        # Absolute HR: CCC penalizes differing resting levels between people.
        return series
    if mode == "baseline_delta":
        # Deviation from each person's own resting HR (offset removed, natural bpm
        # scale kept) — "are our departures from our own rest co-moving". Uses the
        # baseline calibration; falls back to the window mean when uncalibrated.
        rest = cal.resting_hr if cal and cal.resting_hr else float(np.nanmean(series))
        return series - rest
    # zscore (default): standardize by THIS window's own mean/std (not the resting
    # baseline). That makes each series mean-0 / var-1 in-window, so CCC reduces to
    # Pearson correlation — a pure, offset- and scale-invariant "shape" match.
    mu = float(np.nanmean(series))
    sd = float(np.nanstd(finite)) or 1.0
    return (series - mu) / sd


def compute(sessions, now: float, cfg: dict) -> dict:
    proc = cfg["processing"]
    window = proc["sync_window"]
    hz = proc["resample_hz"]
    tau = proc["hr_smooth_tau"]
    mode = proc.get("sync_mode", "zscore")
    grace = proc.get("sync_grace", 10.0)

    grid = uniform_grid(now - window, now, hz)

    # Eligible = active people with recent data (roamed-out people drop after grace).
    people = []
    hr_series = []
    ph_series = []
    for s in sorted(sessions, key=lambda x: x.person_id):
        if s.profile.enrollment_state != EnrollmentState.active:
            continue
        if s.last_seen is None or (now - s.last_seen) > grace:
            continue
        _, hr = smoothed_hr_grid(s, now - window, now, hz, tau)
        if not np.isfinite(hr).any():
            continue
        people.append(s)
        hr_series.append(_transform(hr, mode, s.profile.calibration))
        ph_series.append(phase_grid(s, grid))

    n = len(people)
    ids = [s.person_id for s in people]
    matrix = [[0.0] * n for _ in range(n)]
    plv = [[0.0] * n for _ in range(n)]

    pair_ccc = []
    pair_plv = []
    for i in range(n):
        matrix[i][i] = 1.0
        plv[i][i] = 1.0
        for j in range(i + 1, n):
            xi, xj = hr_series[i], hr_series[j]
            m = np.isfinite(xi) & np.isfinite(xj)
            c = ccc(xi[m], xj[m]) if m.sum() >= 3 else 0.0
            matrix[i][j] = matrix[j][i] = c
            pair_ccc.append(c)

            pi, pj = ph_series[i], ph_series[j]
            pm = np.isfinite(pi) & np.isfinite(pj)
            if pm.sum() >= 3:
                p = float(np.abs(np.mean(np.exp(1j * (pi[pm] - pj[pm])))))
            else:
                p = 0.0
            plv[i][j] = plv[j][i] = p
            pair_plv.append(p)

    cohesion = float(np.mean(pair_ccc)) if pair_ccc else 0.0
    order_param = _kuramoto_order(ph_series, grid) if n >= 2 else 0.0

    return {
        "person_ids": ids,
        "matrix": matrix,
        "plv": plv,
        "cohesion": cohesion,
        "order_param": order_param,
        "mode": mode,
    }


def _kuramoto_order(ph_series: list[np.ndarray], grid: np.ndarray) -> float:
    """Mean over the window of R(t) = |(1/N) sum_i e^{i phi_i(t)}| — group cohesion."""
    if not ph_series or grid.size == 0:
        return 0.0
    stacked = np.vstack(ph_series)  # N x T
    vals = []
    for k in range(stacked.shape[1]):
        col = stacked[:, k]
        col = col[np.isfinite(col)]
        if col.size >= 2:
            vals.append(np.abs(np.mean(np.exp(1j * col))))
    return float(np.mean(vals)) if vals else 0.0
