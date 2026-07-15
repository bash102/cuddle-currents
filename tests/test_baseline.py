"""Baseline calibration: clean rest yields sane stats; poor signal is rejected."""

from cuddle.core.models import NormalizedSample, Source
from cuddle.processing.baseline import BaselineCollector, rmssd


def _sample(t, rr, pid="p1"):
    return NormalizedSample(
        person_id=pid, device_id="d1", source=Source.sim, t_recv=t,
        hr_bpm=int(round(60 / rr)), rr_intervals=[rr], seq=int(t * 10),
    )


def test_rmssd_basic():
    # RR alternating 1.0 / 1.1 s -> successive diff 100 ms -> RMSSD 100
    assert abs(rmssd([1.0, 1.1, 1.0, 1.1]) - 100.0) < 1e-6


def test_baseline_accepts_clean_rest():
    c = BaselineCollector(duration=60, rr_min=0.3, rr_max=2.0, min_quality=0.6, min_beats=30)
    c.start(0.0)
    t = 0.0
    for i in range(80):
        rr = 0.9 + (0.02 if i % 2 else -0.02)  # ~66 bpm with small RSA
        t += rr
        c.add(_sample(t, rr))
    cal, reason = c.result()
    assert cal is not None, reason
    assert 60 < cal.resting_hr < 75
    assert cal.hr_std and cal.hr_std > 0
    assert cal.hrv_baseline and cal.hrv_baseline > 0
    assert cal.baseline_quality == 1.0


def test_baseline_rejects_too_few_beats():
    c = BaselineCollector(duration=60, rr_min=0.3, rr_max=2.0, min_quality=0.6, min_beats=30)
    c.start(0.0)
    for i in range(5):
        c.add(_sample(i + 1.0, 0.9))
    cal, reason = c.result()
    assert cal is None
    assert "clean beats" in reason


def test_baseline_rejects_noisy_signal():
    # Half the beats are implausible (rr outside range) -> quality below threshold.
    c = BaselineCollector(duration=60, rr_min=0.3, rr_max=2.0, min_quality=0.8, min_beats=10)
    c.start(0.0)
    t = 0.0
    for i in range(60):
        rr = 0.9 if i % 2 == 0 else 5.0  # every other beat implausible
        t += 0.9
        c.add(_sample(t, rr))
    cal, reason = c.result()
    assert cal is None
    assert "quality" in reason
