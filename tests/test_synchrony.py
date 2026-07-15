"""Synchrony metric sanity: known signals -> known concordance / PLV."""

import numpy as np

from cuddle.processing.synchrony import _kuramoto_order, ccc


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


def test_kuramoto_order_locked_vs_scattered():
    grid = np.linspace(0, 10, 100)
    # All phases equal -> order ~ 1
    locked = [np.full_like(grid, 1.0) for _ in range(5)]
    assert _kuramoto_order(locked, grid) > 0.99
    # Evenly spread phases -> order ~ 0
    scattered = [np.full_like(grid, 2 * np.pi * k / 5) for k in range(5)]
    assert _kuramoto_order(scattered, grid) < 0.2
