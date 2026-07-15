"""Golden byte-vector tests for the 0x2A37 Heart Rate Measurement decoder."""

import math

import pytest

from cuddle.sources.ble_parser import parse_hr_measurement


def test_uint8_hr_no_rr():
    # flags=0x00 (8-bit HR, no contact support, no RR), HR=60
    m = parse_hr_measurement(bytes([0x00, 60]))
    assert m.hr_bpm == 60
    assert m.rr_intervals == []
    assert m.contact is None
    assert m.energy_expended is None


def test_uint16_hr():
    # flags=0x01 (16-bit HR), HR=300 -> 0x012C little-endian = 2C 01
    m = parse_hr_measurement(bytes([0x01, 0x2C, 0x01]))
    assert m.hr_bpm == 300


def test_single_rr_present():
    # flags=0x10 (RR present), HR=60, RR=1024/1024 = 1.0 s -> 0x0400 LE = 00 04
    m = parse_hr_measurement(bytes([0x10, 60, 0x00, 0x04]))
    assert m.hr_bpm == 60
    assert len(m.rr_intervals) == 1
    assert math.isclose(m.rr_intervals[0], 1.0, rel_tol=1e-9)


def test_multi_rr_present():
    # flags=0x10, HR=75, RR = 1.0 s (00 04) and 0.5 s (512 -> 00 02)
    m = parse_hr_measurement(bytes([0x10, 75, 0x00, 0x04, 0x00, 0x02]))
    assert m.hr_bpm == 75
    assert len(m.rr_intervals) == 2
    assert math.isclose(m.rr_intervals[0], 1.0, rel_tol=1e-9)
    assert math.isclose(m.rr_intervals[1], 0.5, rel_tol=1e-9)


def test_contact_supported_and_detected():
    # bit2 supported=1, bit1 status=1 -> contact True. flags=0x06, HR=80
    m = parse_hr_measurement(bytes([0x06, 80]))
    assert m.contact is True


def test_contact_supported_not_detected():
    # bit2 supported=1, bit1 status=0 -> contact False. flags=0x04
    m = parse_hr_measurement(bytes([0x04, 80]))
    assert m.contact is False


def test_contact_not_supported_returns_none():
    # bit2 supported=0 -> None regardless of bit1. flags=0x02
    m = parse_hr_measurement(bytes([0x02, 80]))
    assert m.contact is None


def test_energy_expended_skipped_before_rr():
    # flags = RR(0x10) | energy(0x08) = 0x18, HR=60, energy=200 (C8 00), RR=1.0s (00 04)
    m = parse_hr_measurement(bytes([0x18, 60, 0xC8, 0x00, 0x00, 0x04]))
    assert m.hr_bpm == 60
    assert m.energy_expended == 200
    assert len(m.rr_intervals) == 1
    assert math.isclose(m.rr_intervals[0], 1.0, rel_tol=1e-9)


def test_full_house_16bit_hr_energy_and_rr():
    # flags = HR16(0x01) | energy(0x08) | RR(0x10) = 0x19
    # HR=500 (F4 01), energy=1000 (E8 03), RR=0.8s -> 819.2 -> 819 (0x0333 = 33 03)
    m = parse_hr_measurement(bytes([0x19, 0xF4, 0x01, 0xE8, 0x03, 0x33, 0x03]))
    assert m.hr_bpm == 500
    assert m.energy_expended == 1000
    assert math.isclose(m.rr_intervals[0], 819 / 1024.0, rel_tol=1e-9)


def test_too_short_raises():
    with pytest.raises(ValueError):
        parse_hr_measurement(bytes([0x00]))


def test_truncated_16bit_raises():
    with pytest.raises(ValueError):
        parse_hr_measurement(bytes([0x01, 0x2C]))  # claims 16-bit but only 1 HR byte


def test_stray_trailing_rr_byte_ignored():
    # RR present, one full RR (00 04) plus a stray odd byte -> ignore the stray
    m = parse_hr_measurement(bytes([0x10, 60, 0x00, 0x04, 0x07]))
    assert len(m.rr_intervals) == 1
