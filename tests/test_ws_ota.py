"""Tests for OTA routes (`POST /api/ota`, `GET /firmware/{name}`).

Uses a fake engine exposing the same surface the real `Engine` gives
`ws_server` routes: `.orchestrator` (with `publish_ota`/`gateway_states`),
`.firmware_dir`, and `.ota_url_base`. Mirrors the FakeEngine pattern in
tests/test_ws_orchestrator_routes.py.
"""

import struct

from fastapi.testclient import TestClient

from cuddle.transport.ws_server import create_app


def _image(version: str) -> bytes:
    buf = bytearray(0x60)
    struct.pack_into("<I", buf, 0x20, 0xABCD5432)
    buf[0x30:0x30 + len(version)] = version.encode()
    return bytes(buf)


class FakeGateway:
    def __init__(self, id: str):
        self.id = id


class FakeOrchestrator:
    def __init__(self):
        self.published: list[tuple[str, str, str]] = []

    def publish_ota(self, url: str, version: str, sha256: str) -> None:
        self.published.append((url, version, sha256))

    def gateway_states(self) -> list[FakeGateway]:
        return [FakeGateway("gw1"), FakeGateway("gw2")]


class FakeEngine:
    """Fake engine giving OTA routes the surface the real Engine provides."""

    def __init__(self, tmp_path, *, orchestrator=None, ota_url_base="http://192.168.1.50:8770"):
        self.orchestrator = orchestrator
        self.firmware_dir = tmp_path
        self.ota_url_base = ota_url_base
        self.latest = None

    async def start(self):
        pass

    async def stop(self):
        pass

    def add_client(self, ws):
        pass

    def remove_client(self, ws):
        pass


def _client_with_routable_host(tmp_path):
    orch = FakeOrchestrator()
    engine = FakeEngine(tmp_path, orchestrator=orch)
    app = create_app(engine)
    client = TestClient(app)
    return client, orch.published, tmp_path


def test_post_ota_stores_publishes_and_returns_version(tmp_path):
    client, published, firmware_dir = _client_with_routable_host(tmp_path)
    r = client.post("/api/ota", files={"bin": ("fw.bin", _image("1.4.0"), "application/octet-stream")})
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == "1.4.0"
    assert body["url"] == "http://192.168.1.50:8770/firmware/1.4.0.bin"
    assert body["url"].endswith("/firmware/1.4.0.bin")
    assert body["gateways"] == ["gw1", "gw2"]
    assert "sha256" in body
    assert published and published[-1][1] == "1.4.0"   # (url, version, sha256)
    assert (firmware_dir / "1.4.0.bin").is_file()


def test_post_ota_rejects_non_image(tmp_path):
    client, published, _ = _client_with_routable_host(tmp_path)
    r = client.post("/api/ota", files={"bin": ("x.bin", b"not an image", "application/octet-stream")})
    assert r.status_code == 400
    assert not published


def test_post_ota_requires_orchestrator(tmp_path):
    engine = FakeEngine(tmp_path, orchestrator=None)
    app = create_app(engine)
    client = TestClient(app)
    r = client.post("/api/ota", files={"bin": ("fw.bin", _image("1.4.0"), "application/octet-stream")})
    assert r.status_code == 503


def test_get_firmware_serves_then_404_and_rejects_traversal(tmp_path):
    client, _, _ = _client_with_routable_host(tmp_path)
    client.post("/api/ota", files={"bin": ("fw.bin", _image("1.4.0"), "application/octet-stream")})
    assert client.get("/firmware/1.4.0.bin").status_code == 200
    assert client.get("/firmware/9.9.9.bin").status_code == 404
    assert client.get("/firmware/..%2fsecrets").status_code in (400, 404)
    # `..` must never resolve to firmware_dir's parent: the served path is
    # built from the validated filename, so this can only be a miss, not a hit.
    assert client.get("/firmware/%2e%2e").status_code in (400, 404)


def test_post_ota_errors_when_host_is_loopback(tmp_path):
    orch = FakeOrchestrator()
    engine = FakeEngine(tmp_path, orchestrator=orch, ota_url_base=None)
    app = create_app(engine)
    client = TestClient(app)
    r = client.post("/api/ota", files={"bin": ("fw.bin", _image("1.4.0"), "application/octet-stream")})
    assert r.status_code == 409
    assert not orch.published
