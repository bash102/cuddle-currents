"""Tests for the pure multi-mock-gateway managed-mode logic (Task 10).

`ManagedGatewayWorld` is the shared, broker-free model driving every mock
gateway in one `--mode managed` process -- the seam that lets these tests run
with no MQTT client and no event loop, mirroring how `GatewayMqttSource` and
`Orchestrator` keep their decision logic synchronous and side-effect-free.

Wire contract under test (matches the app-side `report`/`cmd` schema in
`cuddle/hub/orchestration/world.py` and `orchestrator.py`):
  report: {"capacity": int, "mode": "managed"|"opportunistic",
           "connected": [{"dev": str, "rssi": int|None}],
           "seen": [{"dev": str, "rssi": int|None}], "ts": int_ms}
  cmd:    {"action": "connect"|"release", "dev": str}

Key physical fact under test: a connected BLE band stops advertising, so it
must vanish from EVERY gateway's `seen` at once (not just the connecting
gateway's) -- `ManagedGatewayWorld` centralizes connection state for exactly
this reason.
"""

import json

from tools.mock_gateway import (
    DEFAULT_GRACE_S,
    ManagedGatewayWorld,
    MockGateway,
    cmd_topic,
    control_mode_topic,
    control_online_topic,
    device_frames_from_capture,
    load_gateways,
    report_topic,
)


def _world(**kw):
    gateways = [
        MockGateway(id="gw1", capacity=2, coverage={"AA:01": -55, "BB:02": -70}),
        MockGateway(id="gw2", capacity=2, coverage={"BB:02": -60, "CC:03": -80}),
    ]
    return ManagedGatewayWorld(gateways, **kw)


# ---- topic helpers ----------------------------------------------------------


def test_report_topic():
    assert report_topic("cuddle", "gw1") == "cuddle/gw1/report"


def test_cmd_topic():
    assert cmd_topic("cuddle", "gw1") == "cuddle/gw1/cmd"


def test_control_mode_topic():
    assert control_mode_topic("cuddle") == "cuddle/control/mode"


def test_control_online_topic():
    assert control_online_topic("cuddle") == "cuddle/control/online"


# ---- seen/connected before any cmd -------------------------------------------


def test_boot_default_is_opportunistic():
    world = _world()
    assert world.effective_mode(now=0.0) == "opportunistic"


def test_seen_reflects_each_gateways_coverage_before_any_connect():
    world = _world()
    assert world.seen_for("gw1") == {"AA:01": -55, "BB:02": -70}
    assert world.seen_for("gw2") == {"BB:02": -60, "CC:03": -80}
    assert world.connected_for("gw1") == {}


# ---- connect: seen -> connected, centrally hidden everywhere ----------------


def test_connect_moves_band_seen_to_connected_and_hides_it_on_every_gateway():
    world = _world()
    changed = world.handle_cmd("gw1", "connect", "BB:02")

    assert changed is True
    assert world.connected_for("gw1") == {"BB:02": -70}
    assert "BB:02" not in world.seen_for("gw1")
    # BB:02 is also in gw2's coverage -- it must vanish there too, since a
    # connected band stops advertising everywhere, not just on the gateway
    # that connected it.
    assert "BB:02" not in world.seen_for("gw2")
    assert world.connected_for("gw2") == {}


def test_connect_on_a_dev_not_in_coverage_is_a_noop():
    world = _world()
    assert world.handle_cmd("gw1", "connect", "ZZ:99") is False
    assert world.connected == {}


def test_connect_on_an_already_connected_dev_is_a_noop():
    world = _world()
    world.handle_cmd("gw1", "connect", "BB:02")

    assert world.handle_cmd("gw2", "connect", "BB:02") is False
    assert world.connected_for("gw1") == {"BB:02": -70}
    assert world.connected_for("gw2") == {}


def test_connect_beyond_capacity_is_a_noop():
    world = _world()
    world.handle_cmd("gw1", "connect", "AA:01")
    world.handle_cmd("gw2", "connect", "CC:03")  # unrelated dev, doesn't affect gw1
    assert len(world.connected_for("gw1")) == 1

    # gw1 capacity is 2; fill it, then a third connect must be rejected.
    world.gateways["gw1"].coverage["DD:04"] = -90
    world.handle_cmd("gw1", "connect", "BB:02")
    assert len(world.connected_for("gw1")) == 2

    assert world.handle_cmd("gw1", "connect", "DD:04") is False
    assert "DD:04" not in world.connected


# ---- release: connected -> seen, on every covering gateway -------------------


def test_release_returns_band_to_seen_on_every_gateway_whose_coverage_includes_it():
    world = _world()
    world.handle_cmd("gw1", "connect", "BB:02")

    changed = world.handle_cmd("gw1", "release", "BB:02")

    assert changed is True
    assert world.connected == {}
    assert world.seen_for("gw1")["BB:02"] == -70
    assert world.seen_for("gw2")["BB:02"] == -60


def test_release_from_non_holding_gateway_is_a_noop():
    world = _world()
    world.handle_cmd("gw1", "connect", "BB:02")

    assert world.handle_cmd("gw2", "release", "BB:02") is False
    assert world.connected_for("gw1") == {"BB:02": -70}


# ---- report() shape -----------------------------------------------------------


def test_report_shape_matches_wire_contract():
    world = _world()
    world.on_control_mode(b"managed")
    world.on_control_online(b"1", now=0.0)
    world.handle_cmd("gw1", "connect", "AA:01")

    report = world.report("gw1", now=1.0)

    assert report == {
        "capacity": 2,
        "mode": "managed",
        "connected": [{"dev": "AA:01", "rssi": -55}],
        "seen": [{"dev": "BB:02", "rssi": -70}],
        "ts": 1000,
    }
    # round-trips through JSON exactly like the real wire payload
    json.dumps(report)


# ---- control/mode + control/online + auto-revert -----------------------------


def test_managed_mode_holds_while_online_heartbeats_arrive():
    world = _world(grace=15.0)
    world.on_control_mode(b"managed")
    world.on_control_online(b"1", now=0.0)

    assert world.effective_mode(now=10.0) == "managed"


def test_auto_revert_to_opportunistic_after_grace_elapses_without_online():
    world = _world(grace=15.0)
    world.on_control_mode(b"managed")
    world.on_control_online(b"1", now=0.0)

    assert world.effective_mode(now=14.9) == "managed"
    assert world.effective_mode(now=15.0) == "opportunistic"


def test_auto_revert_also_triggers_when_online_goes_explicitly_zero():
    world = _world(grace=15.0)
    world.on_control_mode(b"managed")
    world.on_control_online(b"1", now=0.0)
    world.on_control_online(b"0", now=5.0)  # explicit 0 doesn't reset the clock

    assert world.effective_mode(now=14.0) == "managed"
    assert world.effective_mode(now=16.0) == "opportunistic"


def test_snap_back_to_managed_when_online_resumes():
    world = _world(grace=15.0)
    world.on_control_mode(b"managed")
    world.on_control_online(b"1", now=0.0)
    assert world.effective_mode(now=20.0) == "opportunistic"

    world.on_control_online(b"1", now=21.0)

    assert world.effective_mode(now=25.0) == "managed"


def test_managed_mode_with_no_online_heartbeat_ever_stays_opportunistic():
    world = _world(grace=15.0)
    world.on_control_mode(b"managed")

    assert world.effective_mode(now=1.0) == "opportunistic"


def test_opportunistic_commanded_mode_ignores_online_heartbeat():
    world = _world(grace=15.0)
    world.on_control_mode(b"opportunistic")
    world.on_control_online(b"1", now=0.0)

    assert world.effective_mode(now=1.0) == "opportunistic"


def test_default_grace_constant_used_when_not_overridden():
    world = _world()
    assert world.grace == DEFAULT_GRACE_S


# ---- coverage-config loading ---------------------------------------------------


def test_load_gateways_parses_coverage_config(tmp_path):
    cfg = tmp_path / "coverage.json"
    cfg.write_text(
        json.dumps(
            {
                "grace": 20.0,
                "gateways": [
                    {"id": "gw1", "capacity": 3, "coverage": {"AA:01": -50, "BB:02": -70}},
                    {"id": "gw2", "capacity": 2, "coverage": {"BB:02": -60}},
                ],
            }
        )
    )

    gateways, grace = load_gateways(str(cfg))

    assert grace == 20.0
    assert [g.id for g in gateways] == ["gw1", "gw2"]
    assert gateways[0].capacity == 3
    assert gateways[0].coverage == {"AA:01": -50, "BB:02": -70}


def test_load_gateways_defaults_grace_when_absent(tmp_path):
    cfg = tmp_path / "coverage.json"
    cfg.write_text(
        json.dumps({"gateways": [{"id": "gw1", "capacity": 1, "coverage": {"AA:01": -50}}]})
    )

    _, grace = load_gateways(str(cfg))

    assert grace == DEFAULT_GRACE_S


# ---- per-device HR frame extraction (pure) -------------------------------------


def test_device_frames_from_capture_groups_by_device_with_own_t0(tmp_path):
    cap = tmp_path / "cap.jsonl"
    rows = [
        {"device_id": "AA:01", "t_recv": 100.0, "hr_bpm": 60, "rr_intervals": [1.0]},
        {"device_id": "BB:02", "t_recv": 200.0, "hr_bpm": 70, "rr_intervals": [0.9]},
        {"device_id": "AA:01", "t_recv": 101.0, "hr_bpm": 61, "rr_intervals": [0.98]},
        {"device_id": "BB:02", "t_recv": 202.0, "hr_bpm": 71, "rr_intervals": [0.88]},
    ]
    cap.write_text("\n".join(json.dumps(r) for r in rows))

    by_device = device_frames_from_capture(str(cap))

    assert set(by_device) == {"AA:01", "BB:02"}
    assert [round(t, 2) for t, _ in by_device["AA:01"]] == [0.0, 1.0]
    assert [round(t, 2) for t, _ in by_device["BB:02"]] == [0.0, 2.0]
