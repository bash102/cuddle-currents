import json
import pytest

from cuddle.core.models import ConnectionState, Source
from cuddle.sources.ble_parser import encode_hr_measurement
from cuddle.sources.mqtt_source import GatewayMqttSource
import cuddle.sources.mqtt_source as mqtt_mod


def _src():
    return GatewayMqttSource(broker="127.0.0.1", port=1883, topic_prefix="cuddle")


def _status(event, rssi=-60):
    return json.dumps({"event": event, "rssi": rssi}).encode()


def test_hr_message_emits_sample_with_device_id_when_unbound():
    s = _src()
    frame = encode_hr_measurement(66, rr_intervals=[0.9])
    s._handle_message("cuddle/gw1/hr/AA:BB", frame)
    sample = s._queue.get_nowait()
    assert sample.device_id == "AA:BB"
    assert sample.person_id == "AA:BB"  # unbound -> provisional id == device id
    assert sample.source == Source.mqtt
    assert sample.hr_bpm == 66
    assert sample.rr_intervals[0] == pytest.approx(0.9, abs=1 / 1024)
    assert sample.seq == 1


def test_hr_message_uses_bound_person_id():
    s = _src()
    s.bind("AA:BB", "wren")
    s._handle_message("cuddle/gw1/hr/AA:BB", encode_hr_measurement(70))
    assert s._queue.get_nowait().person_id == "wren"


def test_seq_increments_per_device():
    s = _src()
    for _ in range(3):
        s._handle_message("cuddle/gw1/hr/AA:BB", encode_hr_measurement(70))
    seqs = [s._queue.get_nowait().seq for _ in range(3)]
    assert seqs == [1, 2, 3]


def test_malformed_hr_payload_is_ignored():
    s = _src()
    s._handle_message("cuddle/gw1/hr/AA:BB", b"\x00")  # too short for a valid frame
    assert s._queue.empty()


def test_wrong_prefix_ignored():
    s = _src()
    s._handle_message("other/gw1/hr/AA:BB", encode_hr_measurement(70))
    assert s._queue.empty()


# Task 3: Presence, handoff, and eviction tests


def test_status_connected_then_disconnected(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(mqtt_mod.clock, "now", lambda: t[0])
    s = _src()
    s._handle_message("cuddle/gw1/status/AA:BB", _status("connected"))
    assert s.connection_states["AA:BB"] == ConnectionState.connected
    s._handle_message("cuddle/gw1/status/AA:BB", _status("disconnected"))
    assert s.connection_states["AA:BB"] == ConnectionState.disconnected


def test_silence_past_drop_after_reads_disconnected(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(mqtt_mod.clock, "now", lambda: t[0])
    s = _src()
    s._handle_message("cuddle/gw1/status/AA:BB", _status("connected"))
    t[0] += 25.0  # > drop_after (20)
    assert s.connection_states["AA:BB"] == ConnectionState.disconnected


def test_handoff_ignores_stale_disconnect_from_old_gateway(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(mqtt_mod.clock, "now", lambda: t[0])
    s = _src()
    s._handle_message("cuddle/gw1/status/AA:BB", _status("connected"))
    s._handle_message("cuddle/gw2/status/AA:BB", _status("connected"))  # handoff to gw2
    s._handle_message("cuddle/gw1/status/AA:BB", _status("disconnected"))  # stale from gw1
    assert s.connection_states["AA:BB"] == ConnectionState.connected


def test_gateway_lwt_marks_its_devices_disconnected(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(mqtt_mod.clock, "now", lambda: t[0])
    s = _src()
    s._handle_message("cuddle/gw1/status/AA:BB", _status("connected"))
    s._handle_message("cuddle/gw1/online", b"0")
    assert s.connection_states["AA:BB"] == ConnectionState.disconnected


def test_unassigned_lists_unbound_only():
    s = _src()
    s._handle_message("cuddle/gw1/status/AA:BB", _status("connected"))
    s._handle_message("cuddle/gw1/status/CC:DD", _status("connected"))
    s.bind("AA:BB", "wren")
    devs = {d.device_id for d in s.unassigned_devices()}
    assert devs == {"CC:DD"}


def test_evictable_only_unbound_and_absent(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(mqtt_mod.clock, "now", lambda: t[0])
    s = _src()
    s._handle_message("cuddle/gw1/status/AA:BB", _status("connected"))
    s._handle_message("cuddle/gw1/status/CC:DD", _status("connected"))
    s.bind("CC:DD", "wren")
    now = t[0] + 130.0  # > evict_after (120)
    assert s._evictable(now) == ["AA:BB"]  # bound CC:DD never evictable
    s._evict("AA:BB")
    assert "AA:BB" not in s._states
