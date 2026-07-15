"""Per-person session state, keyed by ``person_id`` and stable across reconnects.

A ``PersonSession`` holds the running raw signal (RR intervals and instantaneous HR
in ring buffers) plus link bookkeeping. Because sessions are keyed by ``person_id``
and never by the ephemeral device address, a band that drops and rejoins resumes the
same session — history, RMSSD window, and matrix position intact.

``SessionStore`` is the roster: it maps ``device_id -> person_id`` (the enrollment
binding) and owns every session.
"""

from __future__ import annotations

from cuddle.core.models import (
    ConnectionState,
    NormalizedSample,
    PersonProfile,
    EnrollmentState,
)
from cuddle.core.ring import TimeSeries


class PersonSession:
    def __init__(self, profile: PersonProfile, *, capacity: int = 4096) -> None:
        self.profile = profile
        self.rr = TimeSeries(capacity)  # (t_beat, rr seconds)
        self.inst_hr = TimeSeries(capacity)  # (t_beat, bpm)
        self.last_seen: float | None = None
        self.connect_since: float | None = None
        self.last_seq: int | None = None
        self.contact: bool | None = None
        self.connection: ConnectionState = ConnectionState.disconnected
        # scratch space for downstream processors (quality/abstract) to stash state
        self.scratch: dict = {}

    @property
    def person_id(self) -> str:
        return self.profile.person_id

    def add_beat(self, sample: NormalizedSample) -> None:
        t = sample.t_recv
        if self.connect_since is None:
            self.connect_since = t
        # A seq reset (device reconnected) is a gap, not a new person: keep history.
        if self.last_seq is not None and sample.seq <= self.last_seq:
            self.connect_since = t  # fresh link
        self.last_seq = sample.seq
        self.last_seen = t
        self.contact = sample.contact
        # Prefer RR-derived instantaneous HR (beat-to-beat); fall back to reported HR.
        if sample.rr_intervals:
            for rr in sample.rr_intervals:
                if rr > 0:
                    self.rr.push(t, rr)
                    self.inst_hr.push(t, 60.0 / rr)
        elif sample.hr_bpm > 0:
            self.inst_hr.push(t, float(sample.hr_bpm))

    def uptime(self, now: float) -> float | None:
        if self.connect_since is None:
            return None
        return now - self.connect_since


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, PersonSession] = {}
        self._device_to_person: dict[str, str] = {}

    # ---- roster ----------------------------------------------------------

    def create_person(self, profile: PersonProfile) -> PersonSession:
        if profile.person_id in self._sessions:
            self._sessions[profile.person_id].profile = profile
        else:
            self._sessions[profile.person_id] = PersonSession(profile)
        if profile.device_id:
            self._device_to_person[profile.device_id] = profile.person_id
        return self._sessions[profile.person_id]

    def bind_device(self, device_id: str, person_id: str) -> None:
        # Remove any stale binding of this device to another person.
        for dev, pid in list(self._device_to_person.items()):
            if pid == person_id and dev != device_id:
                del self._device_to_person[dev]
        self._device_to_person[device_id] = person_id
        sess = self._sessions.get(person_id)
        if sess:
            sess.profile.device_id = device_id

    def person_for_device(self, device_id: str) -> str | None:
        return self._device_to_person.get(device_id)

    def get(self, person_id: str) -> PersonSession | None:
        return self._sessions.get(person_id)

    def all(self) -> list[PersonSession]:
        return list(self._sessions.values())

    def active(self) -> list[PersonSession]:
        return [
            s
            for s in self._sessions.values()
            if s.profile.enrollment_state == EnrollmentState.active
        ]

    def retire(self, person_id: str) -> None:
        sess = self._sessions.get(person_id)
        if sess:
            sess.profile.enrollment_state = EnrollmentState.retired
            if sess.profile.device_id:
                self._device_to_person.pop(sess.profile.device_id, None)
