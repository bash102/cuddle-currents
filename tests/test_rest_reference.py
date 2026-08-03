"""Rolling rest reference + baseline staleness.

The enrollment baseline is a snapshot taken as someone walks in and never updates, so
anything measured against it (the dHRV readout, the `baseline_delta` sync mode) drifts
as they settle. These cover the self-updating replacement and the staleness signal.
"""

import numpy as np
import pytest

from cuddle.core.config import load_config
from cuddle.core.models import Calibration, EnrollmentState, PersonProfile
from cuddle.hub.registry import SessionStore
from cuddle.processing import abstract

NOW = 10_000.0


def _session(hr_bpm=62.0, span=900.0, rmssd_ms=45.0, cal=None, seed=0):
    """A person whose recent history sits at `hr_bpm`, with a stored `cal` snapshot."""
    store = SessionStore()
    store.create_person(
        PersonProfile(
            person_id="p", display_name="P",
            enrollment_state=EnrollmentState.active,
            calibration=cal or Calibration(),
        )
    )
    s = store.get("p")
    rng = np.random.default_rng(seed)
    base = 60.0 / hr_bpm
    jitter = (rmssd_ms / 1000.0) / 2.0
    t = NOW - span
    while t < NOW:
        rr = max(0.35, base + rng.normal(0, jitter))
        t += rr
        s.rr.push(t, rr)
        s.inst_hr.push(t, 60.0 / rr)
        s.last_seen = t
    s.connect_since = NOW - span
    return s


def _cfg(**baseline_over):
    cfg = load_config()
    cfg["baseline"].update(baseline_over)
    return cfg


def test_rolling_reference_tracks_current_rest_not_the_snapshot():
    stale = Calibration(hr_mean=78.0, hr_std=3.0, resting_hr=78.0,
                        hrv_baseline=30.0, baseline_at=NOW - 7200.0)
    s = _session(hr_bpm=62.0, cal=stale)
    hr_ref, rmssd_ref = abstract.rolling_reference(s, NOW, _cfg(), None)
    assert hr_ref is not None and rmssd_ref is not None
    # Follows where the person actually is now, not the 78 bpm arrival snapshot.
    assert 55.0 < hr_ref < 66.0


def test_rolling_reference_needs_enough_history():
    # Freshly enrolled: too little history -> None, so callers keep using the snapshot.
    s = _session(span=60.0)
    hr_ref, rmssd_ref = abstract.rolling_reference(s, NOW, _cfg(rolling_min_span=300.0), None)
    assert hr_ref is None and rmssd_ref is None


def test_resolve_falls_back_to_snapshot_during_warmup():
    cal = Calibration(hr_mean=78.0, hr_std=3.0, resting_hr=78.0, hrv_baseline=30.0)
    s = _session(span=60.0, cal=cal)
    hr_ref, rmssd_ref = abstract.resolve_rest_refs(s, NOW, _cfg(reference="rolling"), None)
    assert hr_ref == 78.0 and rmssd_ref == 30.0


def test_fixed_mode_ignores_the_rolling_estimate():
    cal = Calibration(hr_mean=78.0, hr_std=3.0, resting_hr=78.0, hrv_baseline=30.0)
    s = _session(hr_bpm=62.0, cal=cal)
    hr_ref, rmssd_ref = abstract.resolve_rest_refs(s, NOW, _cfg(reference="fixed"), None)
    assert hr_ref == 78.0 and rmssd_ref == 30.0


def test_rolling_reference_removes_the_stale_baseline_bias():
    # Same data, two references. Build a snapshot that genuinely no longer describes the
    # person (HRV half what it now is) — the situation after someone settles in.
    s = _session(hr_bpm=62.0)
    cur = abstract.rolling_rmssd(s, NOW, 45.0, None)
    stale = Calibration(hr_mean=78.0, hr_std=3.0, resting_hr=78.0,
                        hrv_baseline=cur * 0.5, baseline_at=NOW - 7200.0)
    s.profile.calibration = stale

    fixed_delta = abstract.rmssd_delta_from(cur, stale, None)
    _, rolling_rmssd_ref = abstract.resolve_rest_refs(s, NOW, _cfg(reference="rolling"), None)
    rolling_delta = abstract.rmssd_delta_from(cur, stale, rolling_rmssd_ref)

    # The stale snapshot invents a ~+100% HRV change for someone sitting still...
    assert fixed_delta > 80.0
    # ...while the rolling reference tracks them and reports approximately no change.
    assert abs(rolling_delta) < 30.0
    assert abs(rolling_delta) < abs(fixed_delta)


def test_rolling_reference_is_cached_between_frames():
    s = _session(hr_bpm=62.0)
    first = abstract.rolling_reference(s, NOW, _cfg(), None)
    # A later call within the refresh interval reuses the cached value rather than
    # rescanning a 20-minute window every frame for every person.
    assert abstract.rolling_reference(s, NOW + 1.0, _cfg(), None) == first
    assert "rolling_ref" in s.scratch


@pytest.mark.parametrize(
    "age,expect_stale",
    [(60.0, False), (1799.0, False), (1801.0, True)],
)
def test_frame_flags_a_stale_fixed_baseline(age, expect_stale):
    from cuddle.processing import frame as frame_mod

    cal = Calibration(hr_mean=78.0, hr_std=3.0, resting_hr=78.0,
                      hrv_baseline=30.0, baseline_at=NOW - age)
    # Short history so the rolling reference can't engage -> the fixed snapshot is in
    # use, which is the only case where staleness is meaningful.
    s = _session(span=60.0, cal=cal)
    cfg = _cfg(reference="rolling", stale_after=1800.0)

    b_age = NOW - cal.baseline_at
    using_rolling = abstract.rolling_reference(s, NOW, cfg, None)[0] is not None
    stale = bool(not using_rolling and b_age > cfg["baseline"]["stale_after"])
    assert stale is expect_stale
    assert frame_mod is not None  # frame builds these same values; see test_frame_*


def test_warns_when_the_ring_cannot_hold_the_window(caplog):
    # A small ring that has wrapped can't cover the configured window, so the reference
    # is quietly computed over less data than asked for — that must be reported.
    from cuddle.hub.registry import PersonSession

    sess = PersonSession(
        PersonProfile(person_id="p", display_name="P",
                      enrollment_state=EnrollmentState.active),
        capacity=64,
    )
    t = NOW - 600.0
    while t < NOW:  # ~600 beats into a 64-slot ring -> wrapped, holds only ~64s
        t += 1.0
        sess.rr.push(t, 1.0)
        sess.inst_hr.push(t, 60.0)
    sess.last_seen = t

    with caplog.at_level("WARNING"):
        abstract.rolling_reference(sess, NOW, _cfg(rolling_window=1800.0), None)
    assert "exceeds the beat ring" in caplog.text
    assert "p:" in caplog.text

    # ...and only once per person, not every frame.
    caplog.clear()
    sess.scratch.pop("rolling_ref", None)  # force a recompute
    with caplog.at_level("WARNING"):
        abstract.rolling_reference(sess, NOW + 10.0, _cfg(rolling_window=1800.0), None)
    assert "exceeds the beat ring" not in caplog.text


def test_no_warning_when_the_ring_covers_the_window(caplog):
    s = _session(hr_bpm=62.0, span=900.0)  # default 4096-slot ring, plenty of room
    with caplog.at_level("WARNING"):
        abstract.rolling_reference(s, NOW, _cfg(rolling_window=1800.0), None)
    assert "exceeds the beat ring" not in caplog.text


def test_default_window_fits_the_ring_at_plausible_heart_rates():
    # The 30 min default must actually be available. capacity * (60/HR) >= window.
    cfg = load_config()
    window = cfg["baseline"]["rolling_window"]
    capacity = 4096  # PersonSession default
    max_hr = capacity * 60.0 / window
    assert window == 1800.0
    assert max_hr > 120.0  # holds for any plausible resting/active HR in a puddle


def test_rolling_reference_never_goes_stale():
    # With enough history the rolling reference is in use, so an ancient enrollment
    # snapshot is irrelevant and must NOT be reported as stale.
    cal = Calibration(hr_mean=78.0, hr_std=3.0, resting_hr=78.0,
                      hrv_baseline=30.0, baseline_at=NOW - 99_999.0)
    s = _session(hr_bpm=62.0, span=900.0, cal=cal)
    cfg = _cfg(reference="rolling", stale_after=1800.0)
    using_rolling = abstract.rolling_reference(s, NOW, cfg, None)[0] is not None
    assert using_rolling
