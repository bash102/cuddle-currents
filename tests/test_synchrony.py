"""Synchrony metric sanity: known signals -> known concordance / PLV."""

import numpy as np

from cuddle.core.models import Calibration
from cuddle.processing.synchrony import _kuramoto_order, _transform, best_lag_ccc, ccc


def test_ccc_identical_series():
    x = np.sin(np.linspace(0, 6 * np.pi, 200))
    assert ccc(x, x) > 0.99


def test_ccc_antiphase_negative():
    t = np.linspace(0, 6 * np.pi, 200)
    x = np.sin(t)
    y = np.sin(t + np.pi)  # inverted
    assert ccc(x, y) < -0.8


def test_ccc_independent_near_zero():
    rng = np.random.default_rng(0)
    x = rng.normal(size=500)
    y = rng.normal(size=500)
    assert abs(ccc(x, y)) < 0.2


def test_ccc_offset_penalized_but_not_for_zscore():
    # Same dynamics, different mean: raw CCC drops, but z-scoring restores agreement.
    t = np.linspace(0, 6 * np.pi, 200)
    x = np.sin(t) + 60
    y = np.sin(t) + 80
    assert ccc(x, y) < 0.6  # offset penalized in raw space
    zx = (x - x.mean()) / x.std()
    zy = (y - y.mean()) / y.std()
    assert ccc(zx, zy) > 0.99  # equals Pearson after z-scoring


def test_zscore_transform_uses_window_stats_not_baseline():
    # Even with a (misleading) baseline calibration, zscore standardizes by the
    # window's own mean/std -> output is mean 0, std 1.
    x = np.sin(np.linspace(0, 6 * np.pi, 200)) * 4 + 70
    cal = Calibration(hr_mean=50.0, hr_std=1.0)  # deliberately wrong-for-window
    z = _transform(x, "zscore", cal)
    assert abs(float(z.mean())) < 1e-9
    assert abs(float(z.std()) - 1.0) < 1e-9


def test_zscore_ccc_equals_pearson():
    # CCC on window-z-scored series should equal Pearson correlation of the originals,
    # regardless of per-series offset and scale.
    rng = np.random.default_rng(3)
    base = np.cumsum(rng.normal(size=300))
    x = 3.0 * base + 60
    y = 0.5 * base + rng.normal(scale=0.2, size=300) + 80  # correlated, diff scale/offset
    pearson = float(np.corrcoef(x, y)[0, 1])
    zc = ccc(_transform(x, "zscore", None), _transform(y, "zscore", None))
    assert abs(zc - pearson) < 1e-6


def test_zscore_not_worse_than_raw_for_similar_dynamics():
    # Two people, same shape, different scale + offset: raw is dragged down by the
    # level/scale gap; zscore (pure shape) stays high.
    t = np.linspace(0, 6 * np.pi, 240)
    shape = np.sin(t) + 0.3 * np.sin(2.3 * t)
    x = 2.0 * shape + 62
    y = 6.0 * shape + 78
    raw = ccc(_transform(x, "raw", None), _transform(y, "raw", None))
    zc = ccc(_transform(x, "zscore", None), _transform(y, "zscore", None))
    assert zc > 0.99
    assert zc > raw


def test_best_lag_recovers_shifted_signal():
    # Same signal, one delayed by 6 samples -> lag-0 CCC is deflated, lag scan recovers.
    t = np.linspace(0, 12 * np.pi, 400)
    x = np.sin(t) + 0.4 * np.sin(2.7 * t)
    shift = 6
    y = np.roll(x, shift)
    zero_lag = ccc(x[shift:], y[shift:])  # a naive aligned slice still mixes the shift
    best, lag = best_lag_ccc(x, y, max_lag=10)
    assert best > 0.99
    assert lag == shift or lag == -shift
    assert best > ccc(x, y)  # improves over the unshifted comparison


def test_best_lag_small_window_does_not_inflate_independent():
    # Independent noise with a small lag budget should stay low (no spurious ~1).
    rng = np.random.default_rng(7)
    x = rng.normal(size=300)
    y = rng.normal(size=300)
    best, _ = best_lag_ccc(x, y, max_lag=8)
    assert abs(best) < 0.3


def test_kuramoto_order_locked_vs_scattered():
    grid = np.linspace(0, 10, 100)
    # All phases equal -> order ~ 1
    locked = [np.full_like(grid, 1.0) for _ in range(5)]
    assert _kuramoto_order(locked, grid) > 0.99
    # Evenly spread phases -> order ~ 0
    scattered = [np.full_like(grid, 2 * np.pi * k / 5) for k in range(5)]
    assert _kuramoto_order(scattered, grid) < 0.2
