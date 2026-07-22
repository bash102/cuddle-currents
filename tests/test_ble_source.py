"""Connection-state bookkeeping in ``DirectBleSource``.

These exercise the pure, time-based helpers (``_link_state`` / ``_evictable``) by
poking the internal dicts directly with fixed timestamps — no bleak, no event
loop, no real hardware.
"""

from cuddle.core.models import ConnectionState
from cuddle.sources.ble_source import DirectBleSource


def _src(**kw):
    return DirectBleSource(drop_after=20.0, evict_after=120.0, **kw)


# ---- Issue 1: disconnected is reachable while running ----------------------


def test_roamed_out_band_reports_disconnected_but_keeps_retrying():
    src = _src()
    dev = "AA:BB"
    # Had a healthy link, then roamed out: retry loop is stuck reconnecting.
    src._states[dev] = ConnectionState.reconnecting
    src._last_connect[dev] = 100.0
    src._last_seen[dev] = 100.0

    # Still within drop_after: keep showing the transient retry state.
    assert src._link_state(dev, now=115.0) == ConnectionState.reconnecting
    # Silent past drop_after: effectively disconnected.
    assert src._link_state(dev, now=125.0) == ConnectionState.disconnected

    # It stays in the tracking dicts (task keeps retrying); it is NOT evicted
    # yet at drop_after — only after the much longer evict_after.
    assert dev in src._states
    assert src._evictable(now=125.0) == []


def test_connected_link_gone_silent_reports_disconnected():
    src = _src()
    dev = "AA:BB"
    src._states[dev] = ConnectionState.connected
    src._last_connect[dev] = 100.0
    src._last_hr[dev] = 60  # expected RR = 1.0s
    src._last_seen[dev] = 100.0

    assert src._link_state(dev, now=100.5) == ConnectionState.connected
    # A few missed beats -> stale.
    assert src._link_state(dev, now=104.0) == ConnectionState.stale
    # Silent past drop_after -> disconnected even though raw says connected.
    assert src._link_state(dev, now=130.0) == ConnectionState.disconnected


def test_never_connected_device_disconnected_after_grace():
    src = _src()
    dev = "AA:BB"
    src._states[dev] = ConnectionState.connecting
    src._discovered_at[dev] = 100.0
    # Within the initial connect grace: still "connecting".
    assert src._link_state(dev, now=110.0) == ConnectionState.connecting
    # Past drop_after with no successful connect ever: disconnected.
    assert src._link_state(dev, now=130.0) == ConnectionState.disconnected


# ---- Issue 2: bounded growth via eviction ---------------------------------


def test_unbound_absent_device_is_evictable_bound_never_is():
    src = _src()
    src._states["unbound"] = ConnectionState.reconnecting
    src._last_seen["unbound"] = 0.0

    src._states["bound"] = ConnectionState.reconnecting
    src._last_seen["bound"] = 0.0
    src.bind("bound", "alice")

    now = 200.0  # both silent for 200s > evict_after (120s)
    evictable = src._evictable(now)
    assert "unbound" in evictable
    assert "bound" not in evictable  # enrolled band may return; never evict


def test_not_evictable_before_evict_after():
    src = _src()
    src._states["d"] = ConnectionState.reconnecting
    src._last_seen["d"] = 0.0
    assert src._evictable(now=100.0) == []  # 100s < evict_after
    assert src._evictable(now=130.0) == ["d"]  # 130s > evict_after


def test_evict_purges_all_bookkeeping():
    src = _src()
    dev = "d"
    src._states[dev] = ConnectionState.reconnecting
    src._seq[dev] = 5
    src._last_hr[dev] = 70
    src._rssi[dev] = -60
    src._last_seen[dev] = 0.0
    src._last_connect[dev] = 0.0
    src._discovered_at[dev] = 0.0

    src._evict(dev)  # no live task attached; must not raise

    for d in (
        src._states,
        src._seq,
        src._last_hr,
        src._rssi,
        src._last_seen,
        src._last_connect,
        src._discovered_at,
    ):
        assert dev not in d


# ---- Issue 3: unassigned_devices applies staleness ------------------------


def test_unassigned_devices_downgrades_stale_and_disconnected(monkeypatch):
    from cuddle.sources import ble_source

    src = _src()

    connected = "fresh"
    src._states[connected] = ConnectionState.connected
    src._last_hr[connected] = 60  # expected RR = 1.0s
    src._last_seen[connected] = 100.0

    stale = "quiet"
    src._states[stale] = ConnectionState.connected
    src._last_hr[stale] = 60
    src._last_seen[stale] = 96.0  # 4s silent > 2.5 * 1.0 -> stale

    gone = "roamed"
    src._states[gone] = ConnectionState.connected
    src._last_hr[gone] = 60
    src._last_seen[gone] = 70.0  # 30s silent > drop_after -> disconnected

    monkeypatch.setattr(ble_source.clock, "now", lambda: 100.0)
    by_id = {d.device_id: d.connection for d in src.unassigned_devices()}

    assert by_id[connected] == ConnectionState.connected
    assert by_id[stale] == ConnectionState.stale
    assert by_id[gone] == ConnectionState.disconnected


def test_unassigned_excludes_bound_devices(monkeypatch):
    from cuddle.sources import ble_source

    src = _src()
    src._states["d1"] = ConnectionState.connected
    src._last_seen["d1"] = 100.0
    src.bind("d1", "alice")

    monkeypatch.setattr(ble_source.clock, "now", lambda: 100.0)
    assert src.unassigned_devices() == []
