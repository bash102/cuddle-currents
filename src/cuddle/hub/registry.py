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

# ---- beat-clock reconstruction ---------------------------------------------
# A BLE notification carries *when it arrived* (``t_recv``) and the RR intervals the
# band measured since the last one. ``t_recv`` is the arrival of the packet, not of the
# beats: it inherits connection-interval quantisation, host scheduling, and (over a
# gateway) WiFi/MQTT queueing — tens to hundreds of ms of jitter. RR, by contrast, is
# measured on the band at ~1 ms resolution *before* any of that.
#
# So beat times are reconstructed by integrating RR forward from a running clock and
# using ``t_recv`` only as a slow anchor, rather than stamping every beat in a packet
# at its arrival time (which collapses multi-RR packets onto one instant and injects
# transport jitter straight into phase — measured PLV of a single heart read by two
# bands was ~0.41 instead of ~1).
_BEAT_RESYNC_S = 1.5   # |error| past this = lost beats / stall / reconnect -> re-anchor
_BEAT_SERVO = 0.05     # fraction of the residual absorbed per packet; low-pass on jitter
_BEAT_EPS = 1e-4       # keep timestamps strictly increasing (np.interp/searchsorted)


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
        # Beat clock: host time of the most recently placed beat (see module notes).
        self._beat_clock: float | None = None
        self._last_beat_t: float | None = None  # rr ring
        self._last_hr_t: float | None = None    # inst_hr ring (also fed HR-only packets)
        # Gap bookkeeping: every hard re-anchor is beats the band measured and we never
        # received. Surfaced so a dropout is visible instead of being silently bridged.
        self.gap_count: int = 0
        self.gap_seconds: float = 0.0
        self.last_gap_at: float | None = None
        # scratch space for downstream processors (quality/abstract) to stash state
        self.scratch: dict = {}

    @property
    def person_id(self) -> str:
        return self.profile.person_id

    def _place_beats(self, rrs: list[float], t_recv: float) -> list[float]:
        """Host timestamps for the beats ending a packet received at ``t_recv``.

        The last beat lands at the reconstructed packet end; earlier beats in the same
        packet are stepped back by their own RR, so a 3-RR notification spans ~3 beats
        of time instead of three samples at one instant.
        """
        total = sum(rrs)
        if self._beat_clock is None:
            end = t_recv  # first packet: nothing to integrate from
        else:
            predicted = self._beat_clock + total
            err = t_recv - predicted
            if abs(err) > _BEAT_RESYNC_S:
                # Beats were lost (or the link stalled/reset): the integrated clock no
                # longer describes reality, so drop the accumulated phase and re-anchor.
                if err > 0:
                    self.gap_count += 1
                    self.gap_seconds += err
                    self.last_gap_at = t_recv
                end = t_recv
            else:
                # Track drift between the band's crystal and the host clock, while
                # averaging out per-packet transport jitter.
                end = predicted + _BEAT_SERVO * err
        # A beat cannot post-date the packet that carried it.
        end = min(end, t_recv)
        self._beat_clock = end

        times: list[float] = []
        t = end
        for rr in reversed(rrs):
            times.append(t)
            t -= rr
        times.reverse()

        # Strictly increasing, or np.interp / np.searchsorted downstream misbehave.
        out: list[float] = []
        prev = self._last_beat_t
        for ts in times:
            if prev is not None and ts <= prev:
                ts = prev + _BEAT_EPS
            out.append(ts)
            prev = ts
        self._last_beat_t = prev
        return out

    def add_beat(self, sample: NormalizedSample) -> None:
        t = sample.t_recv
        if self.connect_since is None:
            self.connect_since = t
        # A seq reset (device reconnected) is a gap, not a new person: keep history.
        if self.last_seq is not None and sample.seq <= self.last_seq:
            self.connect_since = t  # fresh link
            self._beat_clock = None  # integrated phase can't survive a dropped link
        self.last_seq = sample.seq
        self.last_seen = t
        self.contact = sample.contact
        # Prefer RR-derived instantaneous HR (beat-to-beat); fall back to reported HR.
        if sample.rr_intervals:
            rrs = [float(rr) for rr in sample.rr_intervals if rr > 0]
            if rrs:
                for ts, rr in zip(self._place_beats(rrs, t), rrs):
                    self.rr.push(ts, rr)
                    # inst_hr also carries HR-only packets, so it needs its own
                    # monotonicity guard: one of those can sit at a t_recv later
                    # than a beat reconstructed from the following packet.
                    hr_t = ts
                    if self._last_hr_t is not None and hr_t <= self._last_hr_t:
                        hr_t = self._last_hr_t + _BEAT_EPS
                    self._last_hr_t = hr_t
                    self.inst_hr.push(hr_t, 60.0 / rr)
        elif sample.hr_bpm > 0:
            # No RR in this notification: nothing to integrate, so leave the beat clock
            # alone (an empty-RR packet means "no new beat", not a gap).
            ts = t
            if self._last_hr_t is not None and ts <= self._last_hr_t:
                ts = self._last_hr_t + _BEAT_EPS
            self._last_hr_t = ts
            self.inst_hr.push(ts, float(sample.hr_bpm))

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

    def unbind_device(self, device_id: str) -> str | None:
        """Free a device from its current owner. Returns the prior owner's id."""
        pid = self._device_to_person.pop(device_id, None)
        if pid:
            sess = self._sessions.get(pid)
            if sess and sess.profile.device_id == device_id:
                sess.profile.device_id = None
        return pid

    def person_for_device(self, device_id: str) -> str | None:
        pid = self._device_to_person.get(device_id)
        if pid is not None:
            return pid
        # BLE addresses can surface in different case per source (gateway
        # firmware NimBLE toString() is lowercase; others may be upper), so a
        # band enrolled in one case must still resolve when seen in another --
        # otherwise the seen list shows a bare MAC for a known person. This is a
        # read-only fallback: stored keys (and the profile.device_id /
        # source-binding three-way sync) are untouched. O(n) on a miss, n small.
        key = device_id.lower()
        for dev, mapped in self._device_to_person.items():
            if dev.lower() == key:
                return mapped
        return None

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
