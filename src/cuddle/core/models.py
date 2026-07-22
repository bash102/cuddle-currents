"""Canonical data contracts shared across every layer.

These schemas are the load-bearing interfaces of the system:

- ``NormalizedSample`` — what every ingestion source (BLE, simulator, future
  gateway/MQTT) emits. Nothing downstream knows or cares where a sample came from.
- ``PersonProfile`` — the per-person object keyed by ``person_id``: the enrollment
  binding plus the calibration learned at baseline.
- ``StateFrame`` — what the transport broadcasts to both frontends ~10x/second.

Keeping these in one place means the Show view, the Ops view, the DSP, and the
sources can all evolve independently as long as they honour these shapes.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Source(str, Enum):
    ble = "ble"
    sim = "sim"
    mqtt = "mqtt"


class ConnectionState(str, Enum):
    """Lifecycle of a device's live link. Not a boolean — bands roam in and out."""

    scanning = "scanning"
    connecting = "connecting"
    connected = "connected"
    stale = "stale"  # connected but no beat within stale_after_rr_factor * expected RR
    reconnecting = "reconnecting"
    disconnected = "disconnected"


class EnrollmentState(str, Enum):
    """Session-setup lifecycle of a person."""

    discovered = "discovered"  # a device is present but not yet bound to a person
    assigned = "assigned"  # bound to a named person, not yet baselined
    baselining = "baselining"  # rest capture in progress
    calibrated = "calibrated"  # baseline accepted, profile populated
    active = "active"  # participating in synchrony / shown on stage
    retired = "retired"  # explicitly removed this session


class NormalizedSample(BaseModel):
    """One heartbeat notification, normalized across all source types."""

    person_id: str  # stable logical identity, NOT the device address
    device_id: str  # BLE peripheral UUID / MAC / sim id
    source: Source
    t_recv: float  # monotonic host receive time (seconds) — master clock
    hr_bpm: int
    rr_intervals: list[float] = Field(default_factory=list)  # seconds
    contact: bool | None = None  # sensor-contact bit if the flags expose it
    raw_flags: int = 0  # original 0x2A37 flags byte, for debugging
    seq: int = 0  # per-person monotonic counter for gap detection


class Calibration(BaseModel):
    """Per-person physiological calibration learned during the baseline rest capture.

    Individual physiology varies enough (resting HR, HRV, respiration) that
    cross-person comparison must normalize per person. These values anchor that
    normalization. ``None`` until a baseline has been accepted.
    """

    resting_hr: float | None = None
    hr_mean: float | None = None  # anchors z-scoring
    hr_std: float | None = None
    hrv_baseline: float | None = None  # RMSSD at rest; HRV later reported as delta
    respiration_hz: float | None = None  # from RR/RSA spectrum
    baseline_quality: float | None = None  # 0..1 mean quality during capture
    baseline_at: float | None = None  # host time the baseline completed

    @property
    def is_calibrated(self) -> bool:
        return self.hr_mean is not None and self.hr_std is not None


class PersonProfile(BaseModel):
    """Everything we know about a person, keyed by ``person_id``."""

    person_id: str
    display_name: str
    color: str = "#888888"
    shape: str = "disc"  # glyph shape; (color, shape) is a unique visual identity
    seat: int = 0  # 1-based number assigned at enrollment, for "you're #7"
    device_id: str | None = None  # currently bound sensor (may change on swap)
    enrollment_state: EnrollmentState = EnrollmentState.discovered
    calibration: Calibration = Field(default_factory=Calibration)


# ---- StateFrame (transport -> frontend) -------------------------------------


class PersonState(BaseModel):
    """Per-person snapshot in a StateFrame."""

    person_id: str
    display_name: str
    color: str
    shape: str = "disc"
    seat: int = 0
    device_id: str | None = None
    connection: ConnectionState = ConnectionState.disconnected
    enrollment: EnrollmentState = EnrollmentState.discovered
    quality: float = 0.0  # 0..1
    quality_flags: list[str] = Field(default_factory=list)
    hr: float | None = None  # smoothed instantaneous HR
    hr_var: float | None = None  # SD of smoothed HR over the sync window (bpm)
    rmssd: float | None = None  # rolling HRV
    rmssd_delta: float | None = None  # relative to personal baseline, if calibrated
    phase: float | None = None  # oscillator phase, radians 0..2pi
    last_seen: float | None = None  # host time of last sample
    uptime: float | None = None  # seconds connected in current link
    baseline_progress: float | None = None  # 0..1 while baselining
    rr_tail: list[float] = Field(default_factory=list)  # recent RR intervals (s)
    hr_trace_tail: list[float] = Field(default_factory=list)  # recent smoothed HR


class DeviceInfo(BaseModel):
    """An unassigned device seen by a source, for the Ops enrollment list."""

    device_id: str
    source: Source
    hr_bpm: int | None = None  # live HR so the operator can identify it physically
    connection: ConnectionState = ConnectionState.connected
    rssi: int | None = None


class SynchronyState(BaseModel):
    person_ids: list[str] = Field(default_factory=list)  # row/col order of matrix
    matrix: list[list[float]] = Field(default_factory=list)  # pairwise, NxN
    plv: list[list[float]] = Field(default_factory=list)  # phase-locking, NxN
    cohesion: float = 0.0  # mean pairwise correlation
    order_param: float = 0.0  # Kuramoto R (mean PLV proxy)
    mode: str = "zscore"  # raw | zscore | baseline_delta


class ConnectedBand(BaseModel):
    """A band currently connected to a gateway."""

    dev: str
    person_id: str | None = None
    rssi: int | None = None


class SeenBand(BaseModel):
    """A band visible to a gateway but not yet connected. `person_id` is set
    when the band's address is already enrolled, so the UI can show whose band
    is nearby (advertising) rather than a bare MAC."""

    dev: str
    person_id: str | None = None
    rssi: int | None = None


class OtaPhase(BaseModel):
    """Latest OTA progress reported by a gateway on cuddle/<gw>/ota."""

    phase: str  # start | downloading | ok | failed | rejected
    version: str
    detail: str = ""


class GatewayState(BaseModel):
    """State of a single gateway in the orchestration network."""

    id: str
    online: bool = True
    mode: str = "opportunistic"  # "managed" | "opportunistic"
    capacity: int = 0
    connected: list[ConnectedBand] = Field(default_factory=list)
    seen: list[SeenBand] = Field(default_factory=list)
    version: str | None = None
    ota: OtaPhase | None = None


class UnservedBand(BaseModel):
    """A band that cannot be served by any gateway."""

    dev: str
    rssi: int | None = None
    reason: str  # "no_capacity" | "waiting_to_advertise"


class StateFrame(BaseModel):
    """The broadcast contract. Both frontends render off this and nothing else."""

    t: float
    people: list[PersonState] = Field(default_factory=list)
    unassigned: list[DeviceInfo] = Field(default_factory=list)
    synchrony: SynchronyState = Field(default_factory=SynchronyState)
    scenario: str | None = None
    source: Source = Source.sim
    gateways: list[GatewayState] = Field(default_factory=list)
    unserved: list[UnservedBand] = Field(default_factory=list)
