"""Missing beats: bounded repair, and refusing to interpolate across a dropout.

Two failure modes covered here. A long RR interval used to be split into exactly two
beats no matter how long it was, fabricating a large HR dip whenever more than one beat
was missed. And a dropout was silently bridged by ``np.interp``, so the correlation
window saw a straight-line ramp — invented data shaped exactly like signal.
"""

import numpy as np
import pytest

from cuddle.core.config import load_config
from cuddle.core.models import (
    EnrollmentState, NormalizedSample, PersonProfile, Source,
)
from cuddle.hub.registry import SessionStore
from cuddle.processing import abstract, synchrony
from cuddle.processing.artifact import correct_rr
from cuddle.processing.resample import coverage, ema, resample, uniform_grid

# ---- C: bounded multi-beat repair ------------------------------------------


def test_two_missed_beats_become_three_beats_at_the_right_rate():
    # One 3x interval = two missed beats. Splitting it in half (the old behaviour)
    # produced two beats at 44 bpm inside a 67 bpm stretch — a 23 bpm dip that never
    # happened. It must come back as three beats at the surrounding rate.
    rr = np.array([0.9] * 6 + [2.7] + [0.9] * 6)
    t = np.cumsum(rr)
    _, out, _ = correct_rr(t, rr)
    hr = 60.0 / out
    assert out.size == 15  # 12 real + 3 reconstructed
    assert hr.min() > 60.0 and hr.max() < 74.0


def test_a_dropout_is_left_as_a_hole_not_subdivided():
    # 20 s at ~67 bpm is 22 beats. Reconstructing them would be pure invention, so the
    # interval is dropped and the hole left for the resampler to render as NaN.
    rr = np.array([0.9] * 6 + [20.0] + [0.9] * 6)
    t = np.cumsum(rr)
    ct, out, n = correct_rr(t, rr)
    assert out.size == 12  # nothing fabricated into the gap
    assert n >= 1  # counted as an artifact
    assert np.diff(ct).max() > 15.0  # the hole survives in the beat times


def test_max_split_is_the_boundary():
    for k, expect_beats in ((2, 14), (3, 15), (4, 12)):  # 4x exceeds max_split -> gap
        rr = np.array([0.9] * 6 + [0.9 * k] + [0.9] * 6)
        t = np.cumsum(rr)
        _, out, _ = correct_rr(t, rr)
        assert out.size == expect_beats, f"k={k}"


def test_repair_runs_before_the_plausibility_gate():
    # At 50 bpm a single missed beat is a 2.4 s interval — above rr_max (2.0 s). The
    # gate used to delete it before repair could see it, turning a recoverable missed
    # beat into lost data.
    rr = np.array([1.2] * 6 + [2.4] + [1.2] * 6)
    t = np.cumsum(rr)
    _, out, _ = correct_rr(t, rr, rr_max=2.0)
    assert out.size == 14
    assert np.allclose(out, 1.2)


def test_an_absurd_interval_cannot_drag_the_reference_median():
    # The median repair measures against is taken from plausible beats only, so one
    # 40 s dropout interval can't shift the scale and cause healthy beats to be
    # "repaired" against a wrong baseline.
    rr = np.array([0.9] * 10 + [40.0])
    t = np.cumsum(rr)
    _, out, _ = correct_rr(t, rr)
    assert np.allclose(out, 0.9)


# ---- B: don't bridge dropouts ----------------------------------------------


def test_resample_leaves_a_gap_as_nan():
    t = np.array([0.0, 1.0, 2.0, 22.0, 23.0, 24.0])
    v = np.array([60.0, 60.0, 60.0, 62.0, 62.0, 62.0])
    grid = uniform_grid(0.0, 24.0, 4.0)
    bridged = resample(t, v, grid)
    honest = resample(t, v, grid, max_gap=3.0)
    assert np.isfinite(bridged).all()  # old behaviour: a ramp drawn over the hole
    hole = (grid > 5.0) & (grid < 19.0)
    assert np.isnan(honest[hole]).all()
    assert np.isfinite(honest[~hole]).any()
    # Real data on either side is untouched.
    assert honest[0] == pytest.approx(60.0)
    assert honest[-1] == pytest.approx(62.0)


def test_resample_without_max_gap_is_unchanged():
    t = np.array([0.0, 1.0, 9.0])
    v = np.array([1.0, 2.0, 3.0])
    grid = uniform_grid(0.0, 9.0, 2.0)
    assert np.allclose(resample(t, v, grid), np.interp(grid, t, v))


def test_ema_does_not_smear_a_nan_over_the_rest_of_the_series():
    x = np.array([60.0] * 10 + [np.nan] * 10 + [80.0] * 10)
    out = ema(x, 0.25, 3.0)
    assert np.isnan(out[10:20]).all()
    assert np.isfinite(out[:10]).all() and np.isfinite(out[20:]).all()
    # The accumulator restarts after the gap rather than dragging pre-gap state
    # across a stretch where nothing was measured.
    assert out[20] == pytest.approx(80.0)


def test_coverage_reports_the_hole():
    t = np.array([0.0, 1.0, 2.0, 22.0, 23.0, 24.0])
    grid = uniform_grid(0.0, 24.0, 4.0)
    assert coverage(t, grid, 3.0) < 0.55
    assert coverage(np.arange(0.0, 25.0, 1.0), grid, 3.0) == pytest.approx(1.0)


def test_phase_is_not_interpolated_across_a_dropout():
    # Two real beats 25 s apart are not one enormously slow beat; stretching a single
    # 2*pi rotation over the gap is a trajectory the heart never had.
    sess = _session("p")
    for t in list(np.arange(0.9, 10.0, 0.9)) + list(np.arange(35.0, 45.0, 0.9)):
        _push(sess, t, 0.9)
    grid = uniform_grid(0.0, 45.0, 4.0)
    bridged = abstract.phase_grid(sess, grid)
    honest = abstract.phase_grid(sess, grid, max_gap=3.0)
    hole = (grid > 12.0) & (grid < 33.0)
    assert np.isfinite(bridged[hole]).any()
    assert np.isnan(honest[hole]).all()
    assert np.isfinite(honest[grid < 9.0]).any()


# ---- end to end -------------------------------------------------------------


def _session(pid, store=None):
    store = store or SessionStore()
    store.create_person(PersonProfile(
        person_id=pid, display_name=pid, device_id=f"D-{pid}",
        enrollment_state=EnrollmentState.active,
    ))
    return store.get(pid)


def _push(sess, t, rr):
    seq = sess.scratch.setdefault("seq", 0) + 1
    sess.scratch["seq"] = seq
    sess.add_beat(NormalizedSample(
        person_id=sess.person_id, device_id=sess.profile.device_id, source=Source.ble,
        t_recv=t, hr_bpm=int(round(60.0 / rr)), rr_intervals=[rr], contact=True, seq=seq,
    ))


def _pair_with_hole(hole, max_gap):
    """Two people with the same HR wobble; one of them drops out over ``hole``."""
    cfg = load_config()
    cfg["processing"]["resample_max_gap"] = max_gap
    store = SessionStore()
    a, b = _session("a", store), _session("b", store)
    t = 0.0
    while t < 120.0:
        rr = 0.9 + 0.08 * np.sin(2 * np.pi * t / 25.0)  # a shared, real oscillation
        t += rr
        _push(a, t, rr)
        if not (hole[0] <= t <= hole[1]):
            _push(b, t, rr)
    out = synchrony.compute(store.all(), 118.0, cfg)
    return out["matrix"][0][1], out["plv"][0][1]


def test_a_short_dropout_still_reports_the_pair():
    # b misses 6 s of the 30 s window: plenty of joint data left, so the coverage
    # floor must not throw the pair away — it is measured from the 24 s that exist.
    # (On a smooth synthetic wobble, bridging a 6 s hole happens to cost little; on
    # the real two-band capture the same 5 s hole took 0.935 down to 0.792.)
    honest_cc, honest_plv = _pair_with_hole((100.0, 106.0), max_gap=3.0)
    assert honest_cc > 0.9
    assert honest_plv > 0.5


def test_a_long_dropout_reports_no_information_instead_of_desynchrony():
    # b is absent for 25 s of the 30 s window. Interpolating over that produced a
    # confident negative — the visualization asserting these two disagree about a
    # stretch where one of them wasn't there. Below the coverage floor the pair must
    # instead read 0 ("nothing to say"), with PersonState.coverage explaining why.
    bridged_cc, bridged_plv = _pair_with_hole((90.0, 115.0), max_gap=0)
    honest_cc, honest_plv = _pair_with_hole((90.0, 115.0), max_gap=3.0)
    assert bridged_cc < -0.1  # confidently wrong
    assert honest_cc == 0.0 and honest_plv == 0.0


def test_no_gap_means_no_change():
    # The whole mechanism must be inert on clean data.
    a, _ = _pair_with_hole((1e9, 1e9), max_gap=0)
    b, _ = _pair_with_hole((1e9, 1e9), max_gap=3.0)
    assert a == pytest.approx(b, abs=1e-9)
    assert a > 0.9


def test_frame_surfaces_coverage_and_lost_beats():
    from cuddle.hub.enrollment import EnrollmentManager
    from cuddle.processing import frame as frame_builder

    class FakeSource:
        connection_states: dict = {}

        def unassigned_devices(self):
            return []

    cfg = load_config()
    store = SessionStore()
    sess = _session("a", store)
    t = 0.0
    while t < 120.0:
        t += 0.9
        if 90.0 <= t <= 112.0:
            continue  # roamed out
        _push(sess, t, 0.9)

    src = FakeSource()
    enr = EnrollmentManager(store, src, config=cfg, store_path="/tmp/test_gaps_enr.yaml")
    f = frame_builder.build_frame(store, src, enr, cfg, 118.0,
                                  scenario=None, source_type=Source.ble)
    p = f.people[0]
    assert p.gap_count == 1
    assert p.gap_seconds > 20.0
    assert 0.0 < p.coverage < 0.5  # most of the sync window is hole, and says so
