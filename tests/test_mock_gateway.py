import json

from cuddle.sources.ble_parser import parse_hr_measurement
from tools.mock_gateway import build_hr_topic, frames_from_capture


def test_topic_builder():
    assert build_hr_topic("cuddle", "gwA", "AA:BB") == "cuddle/gwA/hr/AA:BB"


def test_frames_from_capture_encode_roundtrip(tmp_path):
    cap = tmp_path / "cap.jsonl"
    rows = [
        {"person_id": "AA:BB", "device_id": "AA:BB", "source": "ble", "t_recv": 100.0,
         "hr_bpm": 60, "rr_intervals": [1.0], "contact": True, "raw_flags": 16, "seq": 1},
        {"person_id": "AA:BB", "device_id": "AA:BB", "source": "ble", "t_recv": 101.0,
         "hr_bpm": 61, "rr_intervals": [0.98], "contact": True, "raw_flags": 16, "seq": 2},
    ]
    cap.write_text("\n".join(json.dumps(r) for r in rows))
    frames = frames_from_capture(str(cap))
    assert [round(t, 2) for t, _, _ in frames] == [0.0, 1.0]  # relative timing
    assert frames[0][1] == "cuddle/mock/hr/AA:BB"  # default gw id "mock"
    m = parse_hr_measurement(frames[0][2])
    assert m.hr_bpm == 60 and len(m.rr_intervals) == 1
