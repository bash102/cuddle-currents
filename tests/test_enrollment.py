"""Enrollment lifecycle: assign, persist/restore, rebind, retire."""

from cuddle.core.config import load_config
from cuddle.core.models import Calibration, EnrollmentState
from cuddle.hub.enrollment import (
    EnrollmentManager,
    identity_for_seat,
    _DEFAULT_COLORS,
    _SHAPES,
)
from cuddle.hub.registry import SessionStore


class FakeSource:
    baseline_scale = 1.0

    def __init__(self):
        self.bindings = {}

    def bind(self, device_id, person_id):
        self.bindings[device_id] = person_id

    def unbind(self, device_id):
        self.bindings.pop(device_id, None)


def _mgr(tmp_path):
    store = SessionStore()
    src = FakeSource()
    mgr = EnrollmentManager(store, src, config=load_config(), store_path=tmp_path / "enr.yaml")
    return store, src, mgr


def test_assign_binds_and_sets_state(tmp_path):
    store, src, mgr = _mgr(tmp_path)
    p = mgr.assign("SIM-01", "Alice")
    assert p.person_id == "alice"
    assert p.enrollment_state == EnrollmentState.assigned
    assert store.person_for_device("SIM-01") == "alice"
    assert src.bindings["SIM-01"] == "alice"


def test_identity_unique_for_30_people():
    # Up to len(colors) x len(shapes) unique (color, shape) combos; assert the first
    # 30 seats are all distinct and consecutive seats differ in color.
    combos = [identity_for_seat(s) for s in range(1, 31)]
    assert len(set(combos)) == 30
    for a, b in zip(combos, combos[1:]):
        assert a[0] != b[0]  # color cycles fastest


def test_assign_sets_seat_shape_color(tmp_path):
    store, src, mgr = _mgr(tmp_path)
    people = [mgr.assign(f"SIM-{i:02d}", f"P{i}") for i in range(1, 4)]
    seats = [p.seat for p in people]
    assert seats == [1, 2, 3]
    assert people[0].color == _DEFAULT_COLORS[0]
    assert people[0].shape == _SHAPES[0]
    # all identities distinct
    assert len({(p.color, p.shape) for p in people}) == 3


def test_seat_survives_restart_and_no_reuse(tmp_path):
    store, src, mgr = _mgr(tmp_path)
    mgr.assign("SIM-01", "Alice")
    mgr.assign("SIM-02", "Bob")
    store2 = SessionStore()
    mgr2 = EnrollmentManager(store2, FakeSource(), config=load_config(), store_path=tmp_path / "enr.yaml")
    mgr2.load()
    assert store2.get("alice").profile.seat == 1
    assert store2.get("bob").profile.seat == 2
    # next enrollment continues after the highest restored seat
    p = mgr2.assign("SIM-03", "Carol")
    assert p.seat == 3


def test_unique_person_ids(tmp_path):
    _, _, mgr = _mgr(tmp_path)
    a = mgr.assign("SIM-01", "Sam")
    b = mgr.assign("SIM-02", "Sam")
    assert a.person_id != b.person_id


def test_persistence_round_trip(tmp_path):
    store, src, mgr = _mgr(tmp_path)
    p = mgr.assign("SIM-01", "Alice")
    store.get(p.person_id).profile.enrollment_state = EnrollmentState.active
    mgr.save()

    # Fresh store + manager loading the same file simulates a restart.
    store2 = SessionStore()
    src2 = FakeSource()
    mgr2 = EnrollmentManager(store2, src2, config=load_config(), store_path=tmp_path / "enr.yaml")
    mgr2.load()
    mgr2.rebind_source()
    sess = store2.get("alice")
    assert sess is not None
    assert sess.profile.device_id == "SIM-01"
    assert src2.bindings["SIM-01"] == "alice"


def test_rebind_keeps_identity(tmp_path):
    store, src, mgr = _mgr(tmp_path)
    mgr.assign("SIM-01", "Alice")
    mgr.rebind("alice", "SIM-09")
    assert store.person_for_device("SIM-09") == "alice"
    assert store.person_for_device("SIM-01") is None
    assert src.bindings["SIM-09"] == "alice"


def test_retire(tmp_path):
    store, src, mgr = _mgr(tmp_path)
    mgr.assign("SIM-01", "Alice")
    mgr.retire("alice")
    assert store.get("alice").profile.enrollment_state == EnrollmentState.retired


def _make_calibrated(store, person_id):
    prof = store.get(person_id).profile
    prof.calibration = Calibration(hr_mean=65.0, hr_std=3.0, resting_hr=65.0, hrv_baseline=40.0)
    prof.enrollment_state = EnrollmentState.active


def test_release_parks_and_keeps_baseline(tmp_path):
    store, src, mgr = _mgr(tmp_path)
    mgr.assign("SIM-01", "Alice")
    _make_calibrated(store, "alice")
    mgr.release_device("alice")
    prof = store.get("alice").profile
    # parked: no device, but retained in roster with baseline
    assert prof.device_id is None
    assert prof.enrollment_state == EnrollmentState.calibrated
    assert prof.calibration.is_calibrated
    assert store.person_for_device("SIM-01") is None
    # the band is returned to the source's unassigned pool
    assert "SIM-01" not in src.bindings


def test_reassign_reuses_band_and_retains_both_baselines(tmp_path):
    store, src, mgr = _mgr(tmp_path)
    # Alice on the band, calibrated + active.
    mgr.assign("SIM-01", "Alice")
    _make_calibrated(store, "alice")
    # Bob enrolled on a second band, calibrated, then parked (simulate hardware shortage).
    mgr.assign("SIM-02", "Bob")
    _make_calibrated(store, "bob")
    mgr.release_device("bob")
    assert store.get("bob").profile.enrollment_state == EnrollmentState.calibrated

    # Hand Alice's band to Bob.
    mgr.assign_device("SIM-01", "bob")

    alice, bob = store.get("alice").profile, store.get("bob").profile
    # Bob now holds the band and is active again — no re-baseline needed.
    assert bob.device_id == "SIM-01"
    assert bob.enrollment_state == EnrollmentState.active
    assert bob.calibration.is_calibrated
    assert src.bindings["SIM-01"] == "bob"
    # Alice parked but retains her identity + baseline.
    assert alice.device_id is None
    assert alice.enrollment_state == EnrollmentState.calibrated
    assert alice.calibration.is_calibrated
    assert alice.seat == 1 and bob.seat == 2  # identities unchanged
