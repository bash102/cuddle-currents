"""Tests for `SessionStore` device->person resolution.

The seen/connected lists in Ops resolve a band's address to its enrolled
person via `person_for_device`. BLE addresses can surface in different case
depending on the source (gateway firmware NimBLE `toString()` is lowercase),
so resolution must be case-insensitive or a registered band shows as a bare
MAC while merely advertising.
"""

from cuddle.core.models import EnrollmentState, PersonProfile
from cuddle.hub.registry import SessionStore


def _store_with(device_id: str) -> SessionStore:
    store = SessionStore()
    store.create_person(
        PersonProfile(
            person_id="alice",
            display_name="Alice",
            device_id=device_id,
            enrollment_state=EnrollmentState.assigned,
        )
    )
    return store


def test_person_for_device_exact_match():
    store = _store_with("d8:14:6f:ed:2a:b5")
    assert store.person_for_device("d8:14:6f:ed:2a:b5") == "alice"


def test_person_for_device_is_case_insensitive():
    # Enrolled lowercase (firmware), seen uppercase (another source) -> still
    # resolves, so the seen list shows "Alice" rather than the raw address.
    store = _store_with("d8:14:6f:ed:2a:b5")
    assert store.person_for_device("D8:14:6F:ED:2A:B5") == "alice"


def test_person_for_device_unknown_returns_none():
    store = _store_with("d8:14:6f:ed:2a:b5")
    assert store.person_for_device("aa:bb:cc:dd:ee:ff") is None
