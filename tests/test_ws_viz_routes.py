import json

from fastapi.testclient import TestClient

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

    def add_client(self, ws):
        pass

    def remove_client(self, ws):
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
