import pytest

from cuddle.core.models import Source
from cuddle.sources.ble_parser import encode_hr_measurement
from cuddle.sources.mqtt_source import GatewayMqttSource


def _src():
    return GatewayMqttSource(broker="127.0.0.1", port=1883, topic_prefix="cuddle")


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
