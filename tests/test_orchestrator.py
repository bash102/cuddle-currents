"""Tests for the async `Orchestrator` (Task 4).

Drive the sync, testable seams directly (`_handle_report`, `_handle_online`,
`_run_plan`, `force_connect`, `force_release`, `set_pin`, `set_mode`,
`gateway_states`, `unserved`) -- no broker, mirroring `test_ble_source.py`'s
style of poking internal state with fixed timestamps. `_publish` is swapped
for a recorder so publishing is observable without any MQTT client/event loop.
"""

import json
from unittest.mock import Mock

import pytest

from cuddle.app import Engine
from cuddle.core.models import EnrollmentState, PersonProfile, Source
from cuddle.hub.orchestration.orchestrator import Orchestrator
from cuddle.hub.orchestration.plan import Cmd, Pending
from cuddle.hub.registry import SessionStore


def _payload(capacity=4, mode="managed", connected=None, seen=None, ts=1_000):
    return {
        "capacity": capacity,
        "mode": mode,
        "connected": connected or [],
        "seen": seen or [],
        "ts": ts,
    }


class _Recorder:
    """Replaces `Orchestrator._publish`; records (topic, payload, qos, retain)."""

    def __init__(self):
        self.calls: list[tuple[str, bytes, int, bool]] = []

    def __call__(self, topic, payload, qos=0, retain=False):
        self.calls.append((topic, payload, qos, retain))


def _orch(store=None, **kw):
    orch = Orchestrator(store or SessionStore(), **kw)
    orch._publish = _Recorder()
    return orch


def _person(store, person_id, device_id, state):
    return store.create_person(
        PersonProfile(
            person_id=person_id,
            display_name=person_id,
            device_id=device_id,
            enrollment_state=state,
        )
    )


# ---- (a) _handle_report builds world -> gateway_states reflects it --------


def test_handle_report_updates_world_and_gateway_states():
    orch = _orch()
    orch._handle_report(
        "gw1",
        json.dumps(
            _payload(capacity=3, connected=[{"dev": "AA:01", "rssi": -40}], seen=[{"dev": "BB:02", "rssi": -70}])
        ).encode(),
        now=100.0,
    )

    states = orch.gateway_states()
    assert len(states) == 1
    gw = states[0]
    assert gw.id == "gw1"
    assert gw.online is True
    assert gw.mode == "managed"
    assert gw.capacity == 3
    assert [c.dev for c in gw.connected] == ["AA:01"]
    assert [s.dev for s in gw.seen] == ["BB:02"]


def test_handle_report_marks_dirty():
    orch = _orch()
    assert not orch._dirty_event.is_set()
    orch._handle_report("gw1", json.dumps(_payload()).encode(), now=100.0)
    assert orch._dirty_event.is_set()


def test_handle_report_ignores_malformed_json():
    orch = _orch()
    orch._handle_report("gw1", b"not json", now=100.0)
    assert orch._world.gateways == {}


def test_gateway_states_maps_connected_dev_to_person():
    store = SessionStore()
    _person(store, "alice", "AA:01", EnrollmentState.active)
    orch = _orch(store=store)
    orch._handle_report(
        "gw1",
        json.dumps(_payload(connected=[{"dev": "AA:01", "rssi": -40}])).encode(),
        now=100.0,
    )

    gw = orch.gateway_states()[0]
    assert gw.connected[0].person_id == "alice"


# ---- (b)/(c) _run_plan emits a connect + records pending; pending guard ---


def test_run_plan_emits_connect_and_registers_pending():
    orch = _orch()
    orch._handle_report(
        "gw1", json.dumps(_payload(capacity=2, seen=[{"dev": "bandA", "rssi": -50}])).encode(), now=100.0
    )

    cmds = orch._run_plan(100.0, allow_rebalance=False)

    assert cmds == [Cmd(gw="gw1", action="connect", dev="bandA")]
    assert "bandA" in orch._pending
    assert orch._pending["bandA"].gw == "gw1"
    assert orch._pending["bandA"].deadline == 100.0 + orch._pending_ttl


def test_run_plan_pending_guard_no_duplicate_before_confirmation():
    orch = _orch()
    orch._handle_report(
        "gw1", json.dumps(_payload(capacity=2, seen=[{"dev": "bandA", "rssi": -50}])).encode(), now=100.0
    )

    first = orch._run_plan(100.0, allow_rebalance=False)
    assert first == [Cmd(gw="gw1", action="connect", dev="bandA")]

    # World unchanged (report hasn't confirmed the connect yet); a second
    # _run_plan before the deadline must not re-issue.
    second = orch._run_plan(101.0, allow_rebalance=False)
    assert second == []


def test_run_plan_drops_pending_once_connect_is_confirmed():
    orch = _orch()
    orch._handle_report(
        "gw1", json.dumps(_payload(capacity=2, seen=[{"dev": "bandA", "rssi": -50}])).encode(), now=100.0
    )
    orch._run_plan(100.0, allow_rebalance=False)
    assert "bandA" in orch._pending

    # The gateway's next report confirms bandA is now connected.
    orch._handle_report(
        "gw1", json.dumps(_payload(capacity=2, connected=[{"dev": "bandA", "rssi": -50}])).encode(), now=102.0
    )
    orch._run_plan(102.0, allow_rebalance=False)
    assert "bandA" not in orch._pending


def test_run_plan_drops_pending_past_deadline():
    orch = _orch(pending_ttl=5.0)
    orch._pending["ghost"] = Pending(gw="gw1", deadline=100.0)

    orch._run_plan(101.0, allow_rebalance=False)

    assert "ghost" not in orch._pending


def test_run_plan_populates_unserved():
    orch = _orch()
    orch._handle_report(
        "gw_opp",
        json.dumps(_payload(mode="opportunistic", seen=[{"dev": "bandA", "rssi": -50}])).encode(),
        now=100.0,
    )

    orch._run_plan(100.0, allow_rebalance=False)

    assert len(orch.unserved()) == 1
    assert orch.unserved()[0].dev == "bandA"
    assert orch.unserved()[0].reason == "no_capacity"


# ---- (d) _pinned returns only assigned/baselining/active devices ----------


def test_pinned_includes_only_assigned_baselining_active_with_device_id():
    store = SessionStore()
    _person(store, "p_assigned", "d1", EnrollmentState.assigned)
    _person(store, "p_baselining", "d2", EnrollmentState.baselining)
    _person(store, "p_active", "d3", EnrollmentState.active)
    _person(store, "p_discovered", "d4", EnrollmentState.discovered)
    _person(store, "p_calibrated", "d5", EnrollmentState.calibrated)
    _person(store, "p_retired", "d6", EnrollmentState.retired)
    _person(store, "p_no_device", None, EnrollmentState.active)

    orch = _orch(store=store)

    assert orch._pinned() == {"d1", "d2", "d3"}


# ---- (e) force_connect publishes immediately + pins ------------------------


def test_force_connect_publishes_immediately_and_pins():
    orch = _orch()

    orch.force_connect("bandA", "gw1")

    assert orch._publish.calls == [
        (
            "cuddle/gw1/cmd",
            json.dumps({"action": "connect", "dev": "bandA"}).encode(),
            1,
            False,
        )
    ]
    assert "bandA" in orch._manual_pins
    assert "bandA" in orch._pending


def test_force_connect_pin_survives_run_plan_release_pressure():
    # bandA is connected on gw1 (capacity 1, full); gw2 has fresh coverage
    # memory of bandA and a free slot -- would normally be a rebalance
    # candidate, except bandA is pinned via force_connect.
    orch = _orch()
    orch._handle_report(
        "gw1",
        json.dumps(_payload(capacity=1, connected=[{"dev": "bandA", "rssi": -50}])).encode(),
        now=99.0,
    )
    orch._handle_report(
        "gw2",
        json.dumps(_payload(capacity=1, seen=[{"dev": "bandA", "rssi": -40}, {"dev": "bandB", "rssi": -40}])).encode(),
        now=100.0,
    )
    orch.force_connect("bandA", "gw1")
    orch._publish.calls.clear()

    cmds = orch._run_plan(100.0, allow_rebalance=True)

    assert all(c.dev != "bandA" for c in cmds)


def test_force_release_publishes_immediately():
    orch = _orch()
    orch._handle_report(
        "gw1", json.dumps(_payload(connected=[{"dev": "bandA", "rssi": -50}])).encode(), now=100.0
    )

    orch.force_release("bandA")

    assert orch._publish.calls == [
        (
            "cuddle/gw1/cmd",
            json.dumps({"action": "release", "dev": "bandA"}).encode(),
            1,
            False,
        )
    ]


def test_force_release_noop_when_dev_not_connected_anywhere():
    orch = _orch()
    orch.force_release("ghost")
    assert orch._publish.calls == []


# ---- set_pin / set_mode -----------------------------------------------------


def test_set_pin_toggles_manual_pin():
    orch = _orch()
    orch.set_pin("bandA", True)
    assert "bandA" in orch._manual_pins
    orch.set_pin("bandA", False)
    assert "bandA" not in orch._manual_pins


def test_set_mode_publishes_retained_control_mode():
    orch = _orch()
    orch.set_mode("opportunistic")
    assert orch._publish.calls == [("cuddle/control/mode", b"opportunistic", 1, True)]


# ---- (f) control/online 0 clears that gateway ------------------------------


def test_handle_online_zero_sets_gateway_offline():
    orch = _orch()
    orch._handle_report(
        "gw1", json.dumps(_payload(connected=[{"dev": "bandA", "rssi": -50}])).encode(), now=100.0
    )
    orch._dirty_event.clear()

    orch._handle_online("gw1", b"0", now=105.0)

    view = orch._world.gateways["gw1"]
    assert view.online is False
    assert view.connected == {}
    assert orch._dirty_event.is_set()


def test_handle_message_routes_report_and_online():
    orch = _orch()

    orch._handle_message(
        "cuddle/gw1/report",
        json.dumps(_payload(connected=[{"dev": "bandA", "rssi": -50}])).encode(),
        now=100.0,
    )
    assert "gw1" in orch._world.gateways

    orch._handle_message("cuddle/gw1/online", b"0", now=101.0)
    assert orch._world.gateways["gw1"].online is False


# ---- Engine integration (Task 5) -------------------------------------------
#
# The Engine owns the SessionStore, so it also builds the Orchestrator. These
# tests exercise only construction and action-method delegation -- never
# `Engine.start()`, which would need a live broker.


class _StubMqttSource:
    """Minimal stand-in for `GatewayMqttSource`. Engine.__init__ only stores
    a reference to the source (via EnrollmentManager/IngestHub) and never
    calls into it, so no bind/unbind/subscribe behavior is needed here."""


def _engine(**kw):
    return Engine(_StubMqttSource(), source_type=Source.mqtt, **kw)


def test_orchestrate_true_with_mqtt_source_builds_orchestrator():
    engine = _engine(orchestrate=True)
    assert isinstance(engine.orchestrator, Orchestrator)
    assert engine.orchestrator._store is engine.store


def test_orchestrate_false_leaves_orchestrator_none():
    engine = _engine(orchestrate=False)
    assert engine.orchestrator is None


def test_orchestrate_defaults_false():
    engine = _engine()
    assert engine.orchestrator is None


def test_orchestrate_true_with_non_mqtt_source_raises():
    with pytest.raises(ValueError, match="orchestration requires the mqtt source"):
        Engine(object(), source_type=Source.sim, orchestrate=True)


def test_orch_connect_delegates_to_force_connect():
    engine = _engine(orchestrate=True)
    engine.orchestrator.force_connect = Mock()

    engine.orch_connect("bandA", "gw1")

    engine.orchestrator.force_connect.assert_called_once_with("bandA", "gw1")


def test_orch_release_delegates_to_force_release():
    engine = _engine(orchestrate=True)
    engine.orchestrator.force_release = Mock()

    engine.orch_release("bandA")

    engine.orchestrator.force_release.assert_called_once_with("bandA")


def test_orch_pin_delegates_to_set_pin():
    engine = _engine(orchestrate=True)
    engine.orchestrator.set_pin = Mock()

    engine.orch_pin("bandA", True)

    engine.orchestrator.set_pin.assert_called_once_with("bandA", True)


def test_orch_set_mode_delegates_to_set_mode():
    engine = _engine(orchestrate=True)
    engine.orchestrator.set_mode = Mock()

    engine.orch_set_mode("managed")

    engine.orchestrator.set_mode.assert_called_once_with("managed")


def test_orch_connect_raises_when_orchestrator_disabled():
    engine = _engine(orchestrate=False)
    with pytest.raises(ValueError, match="orchestration not enabled"):
        engine.orch_connect("bandA", "gw1")


def test_orch_release_raises_when_orchestrator_disabled():
    engine = _engine(orchestrate=False)
    with pytest.raises(ValueError, match="orchestration not enabled"):
        engine.orch_release("bandA")


def test_orch_pin_raises_when_orchestrator_disabled():
    engine = _engine(orchestrate=False)
    with pytest.raises(ValueError, match="orchestration not enabled"):
        engine.orch_pin("bandA", True)


def test_orch_set_mode_raises_when_orchestrator_disabled():
    engine = _engine(orchestrate=False)
    with pytest.raises(ValueError, match="orchestration not enabled"):
        engine.orch_set_mode("managed")


def test_orchestrator_timing_kwargs_pulled_from_config_when_present():
    engine = _engine(
        orchestrate=True,
        config={
            "mqtt": {"broker": "127.0.0.1", "port": 1883, "topic_prefix": "cuddle"},
            "orchestrator": {"report_debounce": 1.5, "reconcile_interval": 9.0},
        },
    )
    assert engine.orchestrator._report_debounce == 1.5
    assert engine.orchestrator._reconcile_interval == 9.0
    # untouched timing keys keep the Orchestrator's own defaults
    assert engine.orchestrator._pending_ttl == 8.0


def test_orchestrator_builds_with_missing_orchestrator_config_section():
    # cfg["orchestrator"] doesn't exist yet (added in Task 9) -- must not KeyError.
    engine = _engine(orchestrate=True, config={})
    assert isinstance(engine.orchestrator, Orchestrator)
