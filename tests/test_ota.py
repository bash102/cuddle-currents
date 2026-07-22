import struct
import pytest
from cuddle.hub import ota


def _fake_image(version: str, magic: int = 0xABCD5432) -> bytes:
    buf = bytearray(0x60)
    struct.pack_into("<I", buf, 0x20, magic)          # app-desc magic at file 0x20
    v = version.encode()
    buf[0x30:0x30 + len(v)] = v                        # 32-byte version at 0x30
    return bytes(buf)


def test_parse_firmware_version_reads_embedded_semver():
    assert ota.parse_firmware_version(_fake_image("1.2.3")) == "1.2.3"


def test_parse_firmware_version_rejects_bad_magic():
    with pytest.raises(ValueError):
        ota.parse_firmware_version(_fake_image("1.2.3", magic=0xDEADBEEF))


def test_parse_firmware_version_rejects_too_small():
    with pytest.raises(ValueError):
        ota.parse_firmware_version(b"\x00" * 8)


def test_sha256_hex_is_stable():
    assert ota.sha256_hex(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


@pytest.mark.parametrize("ip,ok", [
    ("192.168.1.212", True), ("10.0.0.5", True),
    ("127.0.0.1", False), ("0.0.0.0", False), ("", False),
])
def test_is_routable_host(ip, ok):
    assert ota.is_routable_host(ip) is ok


def test_safe_firmware_name_ok():
    assert ota.safe_firmware_name("1.2.3") == "1.2.3.bin"


@pytest.mark.parametrize("bad", ["../etc", "a/b", "1 2", "", "a;b"])
def test_safe_firmware_name_rejects_unsafe(bad):
    with pytest.raises(ValueError):
        ota.safe_firmware_name(bad)
