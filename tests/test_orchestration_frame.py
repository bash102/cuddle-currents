"""Tests for orchestration gateway-state additions to StateFrame."""

from cuddle.core.models import StateFrame


def test_stateframe_defaults_empty_gateways():
    """StateFrame(t=0.0) has gateways == [] and unserved == []."""
    frame = StateFrame(t=0.0)
    assert frame.gateways == []
    assert frame.unserved == []
