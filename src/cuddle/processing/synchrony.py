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


def _lag_pair(xi: np.ndarray, xj: np.ndarray, lag: int) -> tuple[np.ndarray, np.ndarray]:
    """Align xi against xj shifted by ``lag`` samples (xj delayed for lag > 0)."""
    if lag > 0:
        return xi[lag:], xj[: xj.size - lag]
    if lag < 0:
        return xi[: xi.size + lag], xj[-lag:]
    return xi, xj


def best_lag_ccc(xi: np.ndarray, xj: np.ndarray, max_lag: int) -> tuple[float, int]:
    """Max CCC over integer sample lags in [-max_lag, max_lag]; returns (ccc, lag).

    Bands timestamp the same beat up to ~0.5 s apart (different BLE notify schedules),
    which shifts the two HR envelopes and deflates their correlation. A small bounded
    lag scan removes that. Kept bounded because a wide max-over-lag search upward-biases
    the score for uncorrelated series.
    """
    best_c: float | None = None
    best_lag = 0
    for lag in range(-max_lag, max_lag + 1):
        a, b = _lag_pair(xi, xj, lag)
        m = np.isfinite(a) & np.isfinite(b)
        if m.sum() >= 3:
            c = ccc(a[m], b[m])
            if best_c is None or c > best_c:
                best_c, best_lag = c, lag
    return (best_c if best_c is not None else 0.0), best_lag


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


def _ccc_pairs(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """N×N Lin's CCC of a[i] vs b[j] over their both-finite samples, via masked
    matmuls (population mean/var/cov, matching ``ccc``). NaN where <3 paired
    finite samples; 0 where degenerate (denom <= 1e-12) — same guards as ``ccc``.

    Vectorizes the pairwise loop: with the NaNs zeroed and a 0/1 finite mask, the
    per-pair paired-finite count and sums of x, y, x², y², xy are all just
    matmuls (mask@mask.T, x@mask.T, …), from which CCC follows elementwise."""
    ma, mb = np.isfinite(a), np.isfinite(b)
    a0, b0 = np.where(ma, a, 0.0), np.where(mb, b, 0.0)
    maf, mbf = ma.astype(float), mb.astype(float)
    npair = maf @ mbf.T
    sx = a0 @ mbf.T
    sy = maf @ b0.T
    sxx = (a0 * a0) @ mbf.T
    syy = maf @ (b0 * b0).T
    sxy = a0 @ b0.T
    with np.errstate(invalid="ignore", divide="ignore"):
        mx, my = sx / npair, sy / npair
        vx = sxx / npair - mx * mx
        vy = syy / npair - my * my
        cov = sxy / npair - mx * my
        denom = vx + vy + (mx - my) ** 2
        c = 2.0 * cov / denom
    c = np.where(denom <= 1e-12, 0.0, c)
    return np.where(npair >= 3, c, np.nan)  # <3 paired samples -> excluded


def _ccc_matrix(series: list[np.ndarray], max_lag: int) -> np.ndarray:
    """Vectorized equivalent of ``best_lag_ccc`` for every pair: the max Lin's CCC
    over integer lags in [-max_lag, max_lag] (each lag one batch of matmuls, ~17
    total, vs. 435×17 tiny per-pair calls). Diagonal 1.0. Returns N×N."""
    n = len(series)
    if n == 0:
        return np.zeros((0, 0))
    X = np.vstack(series).astype(float)
    T = X.shape[1]
    lags = range(-max_lag, max_lag + 1) if max_lag > 0 else (0,)
    best = np.full((n, n), -np.inf)
    for lag in lags:
        if lag > 0:
            a, b = X[:, lag:], X[:, : T - lag]
        elif lag < 0:
            a, b = X[:, : T + lag], X[:, -lag:]
        else:
            a, b = X, X
        c = _ccc_pairs(a, b)
        best = np.maximum(best, np.where(np.isnan(c), -np.inf, c))
    out = np.where(np.isinf(best), 0.0, best)
    # Mirror upper->lower so [i][j] == [j][i] exactly (the loop set both from the
    # i<j value; max-over-symmetric-lags is symmetric, but pin it to be safe).
    il = np.tril_indices(n, -1)
    out[il] = out.T[il]
    np.fill_diagonal(out, 1.0)
    return out


def _plv_matrix(ph_series: list[np.ndarray]) -> np.ndarray:
    """Vectorized pairwise phase-locking value: |mean over both-finite samples of
    exp(i(phi_i - phi_j))|. One complex matmul (z @ conj(z).T) gives every pair's
    sum at once. 0 where <3 paired samples; diagonal 1.0. Returns N×N."""
    n = len(ph_series)
    if n == 0:
        return np.zeros((0, 0))
    P = np.vstack(ph_series).astype(float)
    m = np.isfinite(P)
    z = np.where(m, np.exp(1j * np.where(m, P, 0.0)), 0.0)  # unit phasors, 0 at NaN
    mf = m.astype(float)
    npair = mf @ mf.T
    s = z @ np.conj(z).T
    with np.errstate(invalid="ignore", divide="ignore"):
        plv = np.abs(s) / npair
    plv = np.where(npair >= 3, plv, 0.0)
    np.fill_diagonal(plv, 1.0)
    return plv


def compute(sessions, now: float, cfg: dict) -> dict:
    proc = cfg["processing"]
    window = proc["sync_window"]
    hz = proc["resample_hz"]
    tau = proc["hr_smooth_tau"]
    mode = proc.get("sync_mode", "zscore")
    grace = proc.get("sync_grace", 10.0)
    art = cfg.get("artifact")
    max_lag = int(round(proc.get("sync_max_lag", 0.0) * hz))  # samples; 0 disables

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
        _, hr = smoothed_hr_grid(s, now - window, now, hz, tau, art)
        if not np.isfinite(hr).any():
            continue
        people.append(s)
        hr_series.append(_transform(hr, mode, s.profile.calibration))
        ph_series.append(phase_grid(s, grid))

    n = len(people)
    ids = [s.person_id for s in people]

    # Vectorized pairwise CCC (max over lags) + PLV — one batch of matmuls each,
    # equivalent to the old per-pair best_lag_ccc/PLV double loop (see helpers).
    ccc_m = _ccc_matrix(hr_series, max_lag)
    plv_m = _plv_matrix(ph_series)
    matrix = ccc_m.tolist()
    plv = plv_m.tolist()

    pair_ccc = ccc_m[np.triu_indices(n, 1)] if n >= 2 else np.empty(0)
    cohesion = float(np.mean(pair_ccc)) if pair_ccc.size else 0.0
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
