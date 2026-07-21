"""Tests for the pure stability-first `plan()` orchestration core.

`plan()` consumes a `WorldModel` (Task 2) snapshot plus pinned/pending state
and decides which gateway should connect/release which band this tick. It is
a pure function: no I/O, no async, no wall-clock reads -- `now` is always
passed in.
"""

from cuddle.hub.orchestration.plan import Cmd, PlanCfg, Pending, plan
from cuddle.hub.orchestration.world import WorldModel


def _payload(capacity=4, mode="managed", connected=None, seen=None, ts=1_000):
    return {
        "capacity": capacity,
        "mode": mode,
        "connected": connected or [],
        "seen": seen or [],
        "ts": ts,
    }


def test_single_advertising_band_one_managed_gw_gets_connected():
    world = WorldModel()
    world.apply_report(
        "gw1",
        _payload(capacity=2, seen=[{"dev": "bandA", "rssi": -50}]),
        now=100.0,
    )

    cmds, unserved = plan(
        world, pinned=set(), pending={}, cfg=PlanCfg(), now=100.0, allow_rebalance=False
    )

    assert cmds == [Cmd(gw="gw1", action="connect", dev="bandA")]
    assert unserved == []


def test_stronger_rssi_wins_tie_broken_by_fewer_connected():
    # Scenario A: different RSSI -> stronger (numerically greater, i.e. less
    # negative) signal wins.
    world_a = WorldModel()
    world_a.apply_report("gw1", _payload(capacity=1, seen=[{"dev": "bandA", "rssi": -70}]), now=100.0)
    world_a.apply_report("gw2", _payload(capacity=1, seen=[{"dev": "bandA", "rssi": -40}]), now=100.0)

    cmds_a, unserved_a = plan(
        world_a, pinned=set(), pending={}, cfg=PlanCfg(), now=100.0, allow_rebalance=False
    )

    assert cmds_a == [Cmd(gw="gw2", action="connect", dev="bandA")]
    assert unserved_a == []

    # Scenario B: tied RSSI -> gateway with fewer already-connected devs wins.
    world_b = WorldModel()
    world_b.apply_report(
        "gw1",
        _payload(capacity=2, connected=[{"dev": "other1", "rssi": -1}], seen=[{"dev": "bandB", "rssi": -50}]),
        now=100.0,
    )
    world_b.apply_report(
        "gw2",
        _payload(capacity=2, connected=[], seen=[{"dev": "bandB", "rssi": -50}]),
        now=100.0,
    )

    cmds_b, unserved_b = plan(
        world_b, pinned=set(), pending={}, cfg=PlanCfg(), now=100.0, allow_rebalance=False
    )

    assert cmds_b == [Cmd(gw="gw2", action="connect", dev="bandB")]
    assert unserved_b == []


def test_band_with_live_pending_gets_no_duplicate_connect():
    world = WorldModel()
    world.apply_report(
        "gw1", _payload(capacity=2, seen=[{"dev": "bandA", "rssi": -50}]), now=100.0
    )
    pending = {"bandA": Pending(gw="gw1", deadline=130.0)}

    cmds, unserved = plan(
        world, pinned=set(), pending=pending, cfg=PlanCfg(), now=100.0, allow_rebalance=False
    )

    assert cmds == []
    assert unserved == []


def test_connected_band_gets_no_connect_and_no_release():
    world = WorldModel()
    world.apply_report(
        "gw1", _payload(capacity=2, connected=[{"dev": "bandA", "rssi": -50}]), now=100.0
    )

    cmds, unserved = plan(
        world, pinned=set(), pending={}, cfg=PlanCfg(), now=100.0, allow_rebalance=True
    )

    assert cmds == []
    assert unserved == []


def test_opportunistic_gateway_never_issued_a_connect():
    world = WorldModel()
    world.apply_report(
        "gw_opp",
        _payload(capacity=5, mode="opportunistic", seen=[{"dev": "bandA", "rssi": -50}]),
        now=100.0,
    )

    cmds, unserved = plan(
        world, pinned=set(), pending={}, cfg=PlanCfg(), now=100.0, allow_rebalance=False
    )

    assert cmds == []
    assert unserved == [{"dev": "bandA", "rssi": -50, "reason": "no_capacity"}]


def test_pinned_band_placed_before_unpinned_competing_for_last_slot():
    world = WorldModel()
    world.apply_report(
        "gw1",
        _payload(
            capacity=1,
            seen=[{"dev": "bandPinned", "rssi": -50}, {"dev": "bandUnpinned", "rssi": -50}],
        ),
        now=100.0,
    )

    cmds, unserved = plan(
        world,
        pinned={"bandPinned"},
        pending={},
        cfg=PlanCfg(),
        now=100.0,
        allow_rebalance=False,
    )

    assert cmds == [Cmd(gw="gw1", action="connect", dev="bandPinned")]
    assert unserved == [{"dev": "bandUnpinned", "rssi": -50, "reason": "no_capacity"}]


def test_pinned_band_never_released_as_rebalance_target():
    world = WorldModel()
    # gw2 saw bandPinned advertising before it connected to gw1 -- stale-ish
    # but fresh coverage memory that would otherwise tempt a rebalance.
    world.apply_report(
        "gw2", _payload(capacity=1, seen=[{"dev": "bandPinned", "rssi": -30}]), now=90.0
    )
    world.apply_report(
        "gw1",
        _payload(
            capacity=1,
            connected=[{"dev": "bandPinned", "rssi": -50}],
            seen=[{"dev": "bandX", "rssi": -40}],
        ),
        now=95.0,
    )
    # gw2 now has a free slot and no longer sees bandPinned (it connected).
    world.apply_report("gw2", _payload(capacity=1, connected=[], seen=[]), now=96.0)

    cmds, unserved = plan(
        world,
        pinned={"bandPinned"},
        pending={},
        cfg=PlanCfg(coverage_ttl=60.0),
        now=100.0,
        allow_rebalance=True,
    )

    assert cmds == []
    assert unserved == [{"dev": "bandX", "rssi": -40, "reason": "no_capacity"}]


def test_unserved_band_without_rebalance_yields_no_cmd():
    world = WorldModel()
    world.apply_report(
        "gw1",
        _payload(
            capacity=1,
            connected=[{"dev": "bandY", "rssi": -50}],
            seen=[{"dev": "bandZ", "rssi": -40}],
        ),
        now=100.0,
    )
    world.apply_report("gw2", _payload(capacity=1, connected=[], seen=[{"dev": "bandY", "rssi": -45}]), now=90.0)

    cmds, unserved = plan(
        world, pinned=set(), pending={}, cfg=PlanCfg(coverage_ttl=60.0), now=100.0, allow_rebalance=False
    )

    assert cmds == []
    assert unserved == [{"dev": "bandZ", "rssi": -40, "reason": "no_capacity"}]


def test_unserved_band_with_rebalance_and_fresh_coverage_releases_movable_y():
    world = WorldModel()
    world.apply_report(
        "gw1",
        _payload(
            capacity=1,
            connected=[{"dev": "bandY", "rssi": -50}],
            seen=[{"dev": "bandZ", "rssi": -40}],
        ),
        now=100.0,
    )
    # gw2 has a free slot and fresh coverage memory of bandY (age 10 <= ttl 60).
    world.apply_report("gw2", _payload(capacity=1, connected=[], seen=[{"dev": "bandY", "rssi": -45}]), now=90.0)

    cmds, unserved = plan(
        world, pinned=set(), pending={}, cfg=PlanCfg(coverage_ttl=60.0), now=100.0, allow_rebalance=True
    )

    assert cmds == [Cmd(gw="gw1", action="release", dev="bandY")]
    assert unserved == []


def test_unserved_band_with_rebalance_but_stale_coverage_yields_no_release():
    world = WorldModel()
    world.apply_report(
        "gw1",
        _payload(
            capacity=1,
            connected=[{"dev": "bandY", "rssi": -50}],
            seen=[{"dev": "bandZ", "rssi": -40}],
        ),
        now=100.0,
    )
    # gw2 has a free slot but stale coverage memory of bandY (age 100 > ttl 60).
    world.apply_report("gw2", _payload(capacity=1, connected=[], seen=[{"dev": "bandY", "rssi": -45}]), now=0.0)

    cmds, unserved = plan(
        world, pinned=set(), pending={}, cfg=PlanCfg(coverage_ttl=60.0), now=100.0, allow_rebalance=True
    )

    assert cmds == []
    assert unserved == [{"dev": "bandZ", "rssi": -40, "reason": "no_capacity"}]


def test_pinned_band_with_no_gateway_seeing_it_is_waiting_to_advertise():
    world = WorldModel()
    world.apply_report("gw1", _payload(capacity=5, connected=[], seen=[]), now=100.0)

    cmds, unserved = plan(
        world,
        pinned={"bandGhost"},
        pending={},
        cfg=PlanCfg(),
        now=100.0,
        allow_rebalance=True,
    )

    assert cmds == []
    assert unserved == [{"dev": "bandGhost", "rssi": None, "reason": "waiting_to_advertise"}]
