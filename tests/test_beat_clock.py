"""Beat-time reconstruction from cumulative RR.

A notification's ``t_recv`` is when the *packet* landed, not when the beats happened:
it carries connection-interval quantisation, host scheduling, and (over a gateway)
WiFi/MQTT queueing. RR is measured on the band before any of that. These cover placing
beats by integrating RR and using ``t_recv`` only as a slow anchor.
"""

import numpy as np
import pytest

from cuddle.core.models import NormalizedSample, PersonProfile, Source
from cuddle.hub.registry import PersonSession


def _sess():
    return PersonSession(PersonProfile(person_id="p", display_name="P"))


def _pkt(t, rrs, seq=0, hr=None):
    rrs = list(rrs)
    return NormalizedSample(
        person_id="p", device_id="d1", source=Source.ble, t_recv=t,
        hr_bpm=hr if hr is not None else int(round(60.0 / rrs[0])) if rrs else 0,
        rr_intervals=rrs, seq=seq, contact=True,
    )


def test_multi_rr_packet_is_spread_over_time_not_collapsed():
    # Three beats delivered in one notification must span ~3 beats of history, not
    # land on a single instant (which is what stamping them all at t_recv did).
    s = _sess()
    s.add_beat(_pkt(100.0, [0.8, 0.9, 1.0], seq=1))
    t, rr = s.rr.arrays()
    assert t.size == 3
    assert np.allclose(np.diff(t), [0.9, 1.0])
    assert t[-1] == pytest.approx(100.0)  # last beat anchors to the packet
    assert np.allclose(rr, [0.8, 0.9, 1.0])


def test_beats_never_post_date_the_packet_that_carried_them():
    s = _sess()
    t_recv = 100.0
    for i in range(20):
        t_recv += 0.9
        s.add_beat(_pkt(t_recv, [0.9], seq=i))
        assert s.rr.latest()[0] <= t_recv + 1e-9


def test_transport_jitter_is_filtered_out_of_beat_spacing():
    # A perfectly regular heart seen through a jittery link: beat spacing should
    # follow the RR the band reported, not the arrival times.
    rng = np.random.default_rng(3)
    s_jit, s_clean = _sess(), _sess()
    t = 1000.0
    for i in range(120):
        t += 0.85
        s_clean.add_beat(_pkt(t, [0.85], seq=i))
        s_jit.add_beat(_pkt(t + abs(rng.normal(0, 0.06)), [0.85], seq=i))

    d_jit = np.diff(s_jit.rr.arrays()[0])
    d_clean = np.diff(s_clean.rr.arrays()[0])
    # Reconstructed spacing sits on the true RR with far less spread than the
    # arrival jitter that was injected (~60 ms sigma).
    assert np.std(d_jit) < 0.02
    assert abs(np.mean(d_jit) - 0.85) < 0.01
    assert np.std(d_clean) < 1e-6


def test_clock_drift_is_tracked_not_ignored():
    # Band crystal runs 0.3% slow relative to the host: reported RR sums short, so an
    # un-anchored integrator would walk off. The servo must keep it pinned.
    s = _sess()
    t = 0.0
    for i in range(600):
        t += 0.9 * 1.003  # true (host) interval
        s.add_beat(_pkt(t, [0.9], seq=i))  # band under-reports the interval
    assert abs(s.rr.latest()[0] - t) < 0.5
    assert s.gap_count == 0  # drift is absorbed by the servo, not by re-anchoring


def test_a_dropout_re_anchors_and_is_counted():
    s = _sess()
    t = 0.0
    for i in range(10):
        t += 0.9
        s.add_beat(_pkt(t, [0.9], seq=i))
    # Band out of range for 20 s; those beats are simply gone.
    t += 20.0
    s.add_beat(_pkt(t, [0.9], seq=11))
    assert s.gap_count == 1
    assert s.gap_seconds > 18.0
    assert s.last_gap_at == pytest.approx(t)
    assert s.rr.latest()[0] == pytest.approx(t)  # re-anchored, not 20 s behind


def test_reconnect_resets_the_integrator():
    s = _sess()
    t = 0.0
    for i in range(1, 6):
        t += 0.9
        s.add_beat(_pkt(t, [0.9], seq=i))
    # seq reset = fresh link. The integrated phase can't survive it.
    s.add_beat(_pkt(t + 30.0, [0.9], seq=1))
    assert s.rr.latest()[0] == pytest.approx(t + 30.0)
    assert len(s.rr) == 6  # history preserved


def test_timestamps_stay_strictly_increasing():
    # np.interp (resample) and np.searchsorted (phase_grid) both require sorted x, so
    # no packet shape may produce a flat or backwards step.
    rng = np.random.default_rng(11)
    s = _sess()
    t = 500.0
    for i in range(300):
        n = int(rng.integers(0, 4))
        t += max(0.05, rng.normal(0.9 * max(n, 1), 0.3))
        if n == 0:
            s.add_beat(_pkt(t, [], seq=i, hr=68))  # HR-only notification
        else:
            s.add_beat(_pkt(t, list(rng.normal(0.9, 0.05, n)), seq=i))
    for ring in (s.rr, s.inst_hr):
        ts, _ = ring.arrays()
        assert ts.size > 0
        assert np.all(np.diff(ts) > 0)


def test_hr_only_packets_do_not_create_a_gap():
    # HR 60 notified at 1 Hz: beats straddle the notification boundary, so packets
    # alternate between carrying nothing and carrying two intervals. No beats are
    # lost, so this must not read as a dropout.
    s = _sess()
    t = 0.0
    for i in range(20):
        t += 1.0
        s.add_beat(_pkt(t, [1.0, 1.0], seq=i) if i % 2 else _pkt(t, [], seq=i, hr=60))
    assert s.gap_count == 0
    assert len(s.rr) == 20


def test_phase_reflects_beat_timing_through_a_jittery_link():
    # The payoff: one heart, two bands, independent transport jitter. Phase-locking
    # should read ~1. Stamping every RR at t_recv put this at ~0.4 on real hardware.
    from cuddle.processing.abstract import phase_grid

    rng = np.random.default_rng(7)
    a, b = _sess(), _sess()
    t = 1000.0
    for i in range(200):
        t += 0.85
        a.add_beat(_pkt(t + abs(rng.normal(0, 0.08)), [0.85], seq=i))
        b.add_beat(_pkt(t + abs(rng.normal(0, 0.08)), [0.85], seq=i))

    grid = np.arange(t - 60.0, t - 1.0, 0.25)
    pa, pb = phase_grid(a, grid), phase_grid(b, grid)
    m = np.isfinite(pa) & np.isfinite(pb)
    plv = float(abs(np.mean(np.exp(1j * (pa[m] - pb[m])))))
    assert plv > 0.9
