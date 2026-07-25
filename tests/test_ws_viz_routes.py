import json

from fastapi.testclient import TestClient

import cuddle.transport.ws_server as ws_server
from cuddle.transport.viz_config import VizConfigStore
from cuddle.transport.ws_server import create_app


class FakeEngine:
    """Minimal engine surface for the viz-config routes; uses a real VizConfigStore."""

    def __init__(self, tmp_path):
        self.viz_config = VizConfigStore(tmp_path / "viz.json")
        self.latest = None

    async def start(self):
        pass

    async def stop(self):
        pass


def _client(tmp_path):
    return TestClient(create_app(FakeEngine(tmp_path)))


def test_get_active_is_null_before_any_set(tmp_path):
    r = _client(tmp_path).get("/api/viz/active")
    assert r.status_code == 200
    assert r.json() is None


def test_post_then_get_active_roundtrip(tmp_path):
    client = _client(tmp_path)
    cfg = {"id": "node-graph", "renderer": "node-graph", "params": {"gravityK": 2}}
    r = client.post("/api/viz/active", json=cfg)
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert client.get("/api/viz/active").json() == cfg


def test_ws_viz_sends_current_config_on_connect(tmp_path):
    engine = FakeEngine(tmp_path)
    engine.viz_config.set({"id": "chord"})
    client = TestClient(create_app(engine))
    with client.websocket_connect("/ws/viz") as ws:
        assert json.loads(ws.receive_text()) == {"id": "chord"}


def _client_fs(tmp_path, monkeypatch):
    (tmp_path / "presets").mkdir()
    monkeypatch.setattr(ws_server, "FRONTEND", tmp_path)
    return TestClient(create_app(FakeEngine(tmp_path)))


def test_presets_manifest_lists_committed_files(tmp_path, monkeypatch):
    client = _client_fs(tmp_path, monkeypatch)
    (tmp_path / "presets" / "node-graph.preset.json").write_text("{}")
    (tmp_path / "presets" / "chord.preset.json").write_text("{}")
    assert set(client.get("/api/presets").json()) == {
        "/presets/chord.preset.json",
        "/presets/node-graph.preset.json",
    }


def test_post_preset_writes_file_inside_presets(tmp_path, monkeypatch):
    client = _client_fs(tmp_path, monkeypatch)
    r = client.post("/api/preset", json={"id": "my-look", "renderer": "chord", "params": {}})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert (tmp_path / "presets" / "my-look.preset.json").is_file()


def test_post_preset_id_cannot_escape_presets_dir(tmp_path, monkeypatch):
    client = _client_fs(tmp_path, monkeypatch)
    client.post("/api/preset", json={"id": "../../evil", "renderer": "chord"})
    # sanitized to a safe name inside presets/, never at the repo root
    assert not (tmp_path / "evil.preset.json").exists()
    assert not (tmp_path.parent / "evil.preset.json").exists()
    assert list((tmp_path / "presets").glob("*.preset.json"))  # something was written, in-dir


def test_delete_preset_removes_file(tmp_path, monkeypatch):
    client = _client_fs(tmp_path, monkeypatch)
    (tmp_path / "presets" / "gone.preset.json").write_text("{}")
    r = client.post("/api/preset/delete", json={"id": "gone"})
    assert r.status_code == 200
    assert not (tmp_path / "presets" / "gone.preset.json").exists()


def test_pages_serve(tmp_path):
    client = _client(tmp_path)
    for path in ("/", "/viz-settings", "/puddle", "/ops"):
        assert client.get(path).status_code == 200
