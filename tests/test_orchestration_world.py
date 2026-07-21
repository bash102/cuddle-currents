"""Tests for the pure orchestration world model + coverage memory.

Coverage memory is fed from gateway `report` payloads:
{"capacity": int, "mode": "managed"|"opportunistic",
 "connected": [{"dev": str, "rssi": int|None}],
 "seen": [{"dev": str, "rssi": int|None}], "ts": int_ms}

Key physical fact under test throughout: a BLE band stops advertising once
connected, so a connected band never appears in any gateway's `seen`.
`advertising()` must therefore exclude any dev connected on ANY gateway, and
`apply_report` must only ever write coverage memory for `seen` (advertising)
devices.
"""

from cuddle.hub.orchestration.world import GatewayView, WorldModel


def _payload(capacity=4, mode="managed", connected=None, seen=None, ts=1_000):
    return {
        "capacity": capacity,
        "mode": mode,
        "connected": connected or [],
        "seen": seen or [],
        "ts": ts,
    }


def test_apply_report_populates_gateway_view_and_coverage():
    world = WorldModel()
    payload = _payload(
        capacity=5,
        mode="managed",
        connected=[{"dev": "AA:01", "rssi": -40}],
        seen=[{"dev": "BB:02", "rssi": -70}],
    )

    world.apply_report("gw1", payload, now=100.0)

    view = world.gateways["gw1"]
    assert isinstance(view, GatewayView)
    assert view.id == "gw1"
    assert view.capacity == 5
    assert view.mode == "managed"
    assert view.online is True
    assert view.connected == {"AA:01": -40}
    assert view.seen == {"BB:02": -70}
    assert view.last_report_ts == 100.0

    assert world.coverage["BB:02"]["gw1"] == (-70, 100.0)
    # connected devs are not advertising, so no coverage entry is written for them
    assert "AA:01" not in world.coverage


def test_apply_report_replaces_prior_view_for_same_gateway():
    world = WorldModel()
    world.apply_report(
        "gw1",
        _payload(seen=[{"dev": "BB:02", "rssi": -70}]),
        now=100.0,
    )
    world.apply_report(
        "gw1",
        _payload(capacity=2, mode="opportunistic", seen=[{"dev": "CC:03", "rssi": -55}]),
        now=200.0,
    )

    view = world.gateways["gw1"]
    assert view.capacity == 2
    assert view.mode == "opportunistic"
    assert view.seen == {"CC:03": -55}
    assert view.last_report_ts == 200.0


def test_advertising_dev_seen_by_two_gateways_reports_both_rssi():
    world = WorldModel()
    world.apply_report("gw1", _payload(seen=[{"dev": "BB:02", "rssi": -60}]), now=10.0)
    world.apply_report("gw2", _payload(seen=[{"dev": "BB:02", "rssi": -75}]), now=11.0)

    ads = world.advertising()

    assert ads["BB:02"] == {"gw1": -60, "gw2": -75}


def test_connected_dev_excluded_from_advertising_even_if_stale_seen_elsewhere():
    world = WorldModel()
    # gw2 saw BB:02 advertising (now stale, since it has since connected to gw1)
    world.apply_report("gw2", _payload(seen=[{"dev": "BB:02", "rssi": -80}]), now=10.0)
    # gw1 has since connected to BB:02 (it stopped advertising)
    world.apply_report("gw1", _payload(connected=[{"dev": "BB:02", "rssi": -40}]), now=20.0)

    ads = world.advertising()

    assert "BB:02" not in ads


def test_holder_of_returns_connecting_gateway():
    world = WorldModel()
    world.apply_report("gw1", _payload(connected=[{"dev": "AA:01", "rssi": -40}]), now=10.0)

    assert world.holder_of("AA:01") == "gw1"
    assert world.holder_of("ZZ:99") is None


def test_connected_devs_returns_union_across_gateways():
    world = WorldModel()
    world.apply_report("gw1", _payload(connected=[{"dev": "AA:01", "rssi": -40}]), now=10.0)
    world.apply_report("gw2", _payload(connected=[{"dev": "CC:03", "rssi": -30}]), now=11.0)

    assert world.connected_devs() == {"AA:01", "CC:03"}


def test_prune_coverage_drops_stale_keeps_fresh():
    world = WorldModel()
    world.apply_report("gw1", _payload(seen=[{"dev": "OLD:01", "rssi": -70}]), now=100.0)
    world.apply_report("gw1", _payload(seen=[{"dev": "NEW:02", "rssi": -70}]), now=190.0)

    world.prune_coverage(now=200.0, ttl=50.0)

    assert "OLD:01" not in world.coverage
    assert world.coverage["NEW:02"]["gw1"] == (-70, 190.0)


def test_prune_coverage_drops_only_stale_gateway_entry_for_multi_gateway_dev():
    world = WorldModel()
    world.apply_report("gw1", _payload(seen=[{"dev": "BB:02", "rssi": -60}]), now=100.0)
    world.apply_report("gw2", _payload(seen=[{"dev": "BB:02", "rssi": -75}]), now=190.0)

    world.prune_coverage(now=200.0, ttl=50.0)

    assert "gw1" not in world.coverage["BB:02"]
    assert world.coverage["BB:02"]["gw2"] == (-75, 190.0)


def test_prune_coverage_keeps_entry_for_currently_connected_dev():
    world = WorldModel()
    # gw2 saw BB:02 advertising at now=0
    world.apply_report("gw2", _payload(seen=[{"dev": "BB:02", "rssi": -75}]), now=0.0)
    # BB:02 has since connected on gw1 -- it stops advertising, so it can never
    # refresh its gw2 coverage entry via `seen` again.
    world.apply_report("gw1", _payload(connected=[{"dev": "BB:02", "rssi": -40}]), now=50.0)

    # Far past ttl relative to the gw2 coverage entry's timestamp (now=0).
    world.prune_coverage(now=200.0, ttl=50.0)

    # BB:02 is still connected (on gw1), so its gw2 coverage memory must be
    # frozen in place -- pruning it would strand a future rebalance with no
    # alternate gateway to hand it to.
    assert world.coverage["BB:02"]["gw2"] == (-75, 0.0)


def test_prune_coverage_drops_stale_entry_once_dev_disconnects():
    world = WorldModel()
    world.apply_report("gw2", _payload(seen=[{"dev": "BB:02", "rssi": -75}]), now=0.0)
    world.apply_report("gw1", _payload(connected=[{"dev": "BB:02", "rssi": -40}]), now=50.0)

    # BB:02 disconnects from gw1 and is not re-seen advertising anywhere, so
    # it drops out of connected_devs() with no fresh coverage entry either.
    world.apply_report("gw1", _payload(), now=60.0)

    world.prune_coverage(now=200.0, ttl=50.0)

    # No longer connected anywhere -- the stale gw2 entry prunes normally.
    assert "BB:02" not in world.coverage


def test_set_offline_marks_gateway_offline_and_clears_connected_and_seen():
    world = WorldModel()
    world.apply_report(
        "gw1",
        _payload(
            connected=[{"dev": "AA:01", "rssi": -40}],
            seen=[{"dev": "BB:02", "rssi": -70}],
        ),
        now=100.0,
    )

    world.set_offline("gw1", now=150.0)

    view = world.gateways["gw1"]
    assert view.online is False
    assert view.connected == {}
    assert view.seen == {}
    # coverage memory from before going offline is left in place to age out naturally
    assert world.coverage["BB:02"]["gw1"] == (-70, 100.0)


def test_set_offline_on_unknown_gateway_is_a_noop():
    world = WorldModel()
    # No prior report for gw1 -- must not raise, and must not fabricate a view.
    world.set_offline("gw1", now=150.0)
    assert "gw1" not in world.gateways


def test_advertising_excludes_dev_connected_on_a_different_gateway_than_seen_on():
    world = WorldModel()
    world.apply_report(
        "gw1",
        _payload(
            connected=[{"dev": "AA:01", "rssi": -40}],
            seen=[{"dev": "BB:02", "rssi": -70}],
        ),
        now=100.0,
    )

    ads = world.advertising()

    assert "AA:01" not in ads
    assert ads["BB:02"] == {"gw1": -70}
