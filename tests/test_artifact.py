"""Beat-level artifact correction: kill spikes, preserve genuine dynamics."""

import numpy as np

from cuddle.processing.artifact import correct_rr, hampel


def test_hampel_replaces_isolated_spike():
    x = np.ones(21) * 1.0
    x[10] = 5.0  # a big spike
    cleaned, replaced = hampel(x, window=5, n_sigma=3.0, min_abs=0.2)
    assert replaced[10]
    assert abs(cleaned[10] - 1.0) < 1e-9
    assert replaced.sum() == 1


def test_hampel_floor_protects_flat_signal():
    # Small genuine variation (well under the absolute floor) must not be "corrected".
    rng = np.random.default_rng(0)
    x = 1.0 + rng.normal(scale=0.01, size=200)  # ~1% jitter
    _, replaced = hampel(x, window=5, n_sigma=3.0, min_abs=0.2)
    assert replaced.sum() == 0


def test_correct_rr_neutral_on_clean_series():
    # A smooth RR trend with tiny noise -> nothing corrected, series barely changes.
    t = np.cumsum(np.full(120, 1.0))
    rr = 1.0 + 0.03 * np.sin(np.linspace(0, 6, 120))
    ct, cr, n = correct_rr(t, rr)
    assert n == 0
    assert np.allclose(cr, rr)


def test_correct_rr_removes_doubled_and_halved_beats():
    rng = np.random.default_rng(1)
    rr = 1.0 + 0.02 * rng.normal(size=200)
    t = np.cumsum(rr)
    rr_spiky = rr.copy()
    rr_spiky[50] *= 2.0   # missed beat (doubled RR -> HR spike down)
    rr_spiky[120] *= 0.5  # extra beat (halved RR -> HR spike up)
    _, cr, n = correct_rr(t, rr_spiky)
    assert n >= 1
    # corrected instantaneous HR has no extreme spikes left
    ihr = 60.0 / cr
    assert ihr.max() < 90 and ihr.min() > 40


def test_correct_rr_preserves_amplitude_and_phase_of_real_signal():
    # Inject spikes onto a clean oscillation; the corrected series should track the
    # clean one (high correlation) -> spike removal doesn't distort dynamics.
    t = np.cumsum(np.full(300, 1.0))
    clean = 1.0 + 0.08 * np.sin(np.linspace(0, 10, 300))
    spiky = clean.copy()
    for i in (40, 90, 150, 210, 260):
        spiky[i] *= 1.8
    # repair=False isolates Hampel (in-place replacement keeps length aligned with clean)
    _, corrected, _ = correct_rr(t, spiky, repair=False)
    r = np.corrcoef(clean, corrected)[0, 1]
    assert r > 0.98
