"""Reconnection identity: a drop -> rejoin keeps the person's history."""

from cuddle.core.models import (
    EnrollmentState,
    NormalizedSample,
    PersonProfile,
    Source,
)
from cuddle.hub.registry import PersonSession, SessionStore


def _beat(t, seq, rr=0.9):
    return NormalizedSample(
        person_id="alice", device_id="d1", source=Source.sim, t_recv=t,
        hr_bpm=int(round(60 / rr)), rr_intervals=[rr], seq=seq,
    )


def test_seq_reset_is_a_gap_not_a_new_person():
    sess = PersonSession(PersonProfile(person_id="alice", display_name="Alice"))
    for i in range(1, 6):
        sess.add_beat(_beat(float(i), i))
    assert len(sess.rr) == 5
    first_connect = sess.connect_since

    # Device reconnects: seq resets to 1. History must be preserved, link refreshed.
    sess.add_beat(_beat(10.0, 1))
    assert len(sess.rr) == 6  # old beats kept
    assert sess.connect_since == 10.0  # fresh link window
    assert sess.connect_since != first_connect


def test_store_keeps_session_across_rebind():
    store = SessionStore()
    store.create_person(
        PersonProfile(
            person_id="alice", display_name="Alice", device_id="d1",
            enrollment_state=EnrollmentState.active,
        )
    )
    sess = store.get("alice")
    for i in range(1, 4):
        sess.add_beat(_beat(float(i), i))

    # Rebind alice onto a new band (battery swap).
    store.bind_device("d2", "alice")
    assert store.person_for_device("d2") == "alice"
    assert store.person_for_device("d1") is None  # old binding cleared
    assert store.get("alice") is sess  # same session, history intact
    assert len(sess.rr) == 3


def test_active_filter():
    store = SessionStore()
    store.create_person(
        PersonProfile(person_id="a", display_name="A", enrollment_state=EnrollmentState.active)
    )
    store.create_person(
        PersonProfile(person_id="b", display_name="B", enrollment_state=EnrollmentState.assigned)
    )
    active = [s.person_id for s in store.active()]
    assert active == ["a"]
