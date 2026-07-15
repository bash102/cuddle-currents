"""Enrollment lifecycle: assign, persist/restore, rebind, retire."""

from cuddle.core.config import load_config
from cuddle.core.models import EnrollmentState
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
