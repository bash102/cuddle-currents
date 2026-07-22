"""Tests for orchestration gateway-state additions to StateFrame."""

from cuddle.core.config import load_config
from cuddle.core.models import (
    ConnectionState,
    GatewayState,
    Source,
    StateFrame,
    UnservedBand,
)
from cuddle.hub.enrollment import EnrollmentManager
from cuddle.hub.registry import SessionStore
from cuddle.processing import frame as frame_builder


def test_stateframe_defaults_empty_gateways():
    """StateFrame(t=0.0) has gateways == [] and unserved == []."""
    frame = StateFrame(t=0.0)
    assert frame.gateways == []
    assert frame.unserved == []


class FakeSource:
    """Minimal source for build_frame testing."""

    def __init__(self):
        self._connection_states = {}

    @property
    def connection_states(self) -> dict:
        return self._connection_states

    def unassigned_devices(self):
        return []


class FakeOrchestrator:
    """Fake orchestrator with gateway state."""

    def __init__(self, gateways=None, unserved=None):
        self._gateways = gateways or []
        self._unserved = unserved or []

    def gateway_states(self):
        return self._gateways

    def unserved(self):
        return self._unserved


def test_build_frame_with_orchestrator():
    """build_frame with orchestrator puts gateways and unserved on the frame."""
    store = SessionStore()
    source = FakeSource()
    cfg = load_config()
    enrollment = EnrollmentManager(store, source, config=cfg, store_path="/tmp/test_enr.yaml")

    # Create fake orchestrator with one gateway and one unserved band
    gateway = GatewayState(id="gw-1", online=True, mode="opportunistic", capacity=5)
    unserved = UnservedBand(dev="dev-1", reason="no_capacity")
    orchestrator = FakeOrchestrator(gateways=[gateway], unserved=[unserved])

    now = 0.0
    frame = frame_builder.build_frame(
        store,
        source,
        enrollment,
        cfg,
        now,
        scenario=None,
        source_type=Source.mqtt,
        orchestrator=orchestrator,
    )

    assert len(frame.gateways) == 1
    assert frame.gateways[0].id == "gw-1"
    assert len(frame.unserved) == 1
    assert frame.unserved[0].dev == "dev-1"


def test_build_frame_without_orchestrator():
    """build_frame with orchestrator=None has empty gateways and unserved."""
    store = SessionStore()
    source = FakeSource()
    cfg = load_config()
    enrollment = EnrollmentManager(store, source, config=cfg, store_path="/tmp/test_enr_none.yaml")

    now = 0.0
    frame = frame_builder.build_frame(
        store,
        source,
        enrollment,
        cfg,
        now,
        scenario=None,
        source_type=Source.sim,
        orchestrator=None,
    )

    assert frame.gateways == []
    assert frame.unserved == []
