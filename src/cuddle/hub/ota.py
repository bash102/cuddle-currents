"""Pure helpers for over-the-air firmware updates.

No app state, no MQTT. The one non-trivial piece is reading the firmware
version straight out of an ESP-IDF app image: IDF places an esp_app_desc_t at
file offset 0x20 whose 32-byte NUL-terminated `version` field (offset 0x30)
carries PROJECT_VER (from version.txt). We validate the magic word before
trusting the file so a non-image upload fails loudly.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
import struct

_APP_DESC_OFFSET = 0x20
_APP_DESC_MAGIC = 0xABCD5432
_VERSION_OFFSET = _APP_DESC_OFFSET + 0x10   # 0x30
_VERSION_LEN = 32
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9._-]+$")


def parse_firmware_version(data: bytes) -> str:
    if len(data) < _VERSION_OFFSET + _VERSION_LEN:
        raise ValueError("too small to be an ESP-IDF app image")
    (magic,) = struct.unpack_from("<I", data, _APP_DESC_OFFSET)
    if magic != _APP_DESC_MAGIC:
        raise ValueError(f"bad app-desc magic {magic:#010x}; not an ESP-IDF image")
    raw = data[_VERSION_OFFSET:_VERSION_OFFSET + _VERSION_LEN]
    version = raw.split(b"\x00", 1)[0].decode("ascii", "replace")
    if not version:
        raise ValueError("empty firmware version")
    return version


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_routable_host(ip: str) -> bool:
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (addr.is_loopback or addr.is_unspecified)


def detect_lan_ip(target: tuple[str, int] = ("8.8.8.8", 80)) -> str | None:
    """This host's primary LAN IP. A connected UDP socket sends no packets;
    getsockname reveals the source address the OS would route toward `target`
    -- i.e. the default-route (LAN) interface. `target` defaults to a public
    address so this resolves the real LAN IP even when the MQTT broker runs on
    localhost (targeting a local broker resolves to 127.0.0.1 and yields
    nothing useful). No traffic is sent; only a route lookup happens."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(target)
        ip = s.getsockname()[0]
        return ip if is_routable_host(ip) else None
    except OSError:
        return None
    finally:
        s.close()


def ota_url_base_for_host(host: str, port: int) -> str | None:
    """Base URL gateways use to fetch firmware, given the app's HTTP bind host.

    - all-interfaces (`0.0.0.0`/`::`/empty) -> discover this host's LAN IP.
    - a specific routable host -> use it verbatim.
    - loopback-only (`127.0.0.1`) -> None: gateways on the LAN can't reach
      loopback, so refuse rather than publish an unreachable OTA URL.
    """
    if host in ("0.0.0.0", "::", ""):
        lan = detect_lan_ip()
    elif is_routable_host(host):
        lan = host
    else:
        lan = None
    return f"http://{lan}:{port}" if lan else None


def safe_firmware_name(version: str) -> str:
    if not _SAFE_VERSION.match(version):
        raise ValueError(f"unsafe firmware version {version!r}")
    return f"{version}.bin"
