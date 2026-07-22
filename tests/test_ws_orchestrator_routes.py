"""Tests for orchestrator REST routes (/api/orchestrator/*).

Uses a fake engine that records method calls and can raise ValueError to test
error handling (400 responses).
"""

from fastapi.testclient import TestClient

from cuddle.transport.ws_server import create_app


class FakeEngine:
    """Fake engine that records orchestrator calls and can raise ValueError."""

    def __init__(self, should_raise_error: bool = False):
        self.should_raise_error = should_raise_error
        self.calls = []
        self.latest = None

    async def start(self):
        pass

    async def stop(self):
        pass

    def add_client(self, ws):
        pass

    def remove_client(self, ws):
        pass

    def orch_set_mode(self, mode: str) -> None:
        self.calls.append(("orch_set_mode", mode))
        if self.should_raise_error:
            raise ValueError("orchestration not enabled")

    def orch_connect(self, device_id: str, gateway_id: str) -> None:
        self.calls.append(("orch_connect", device_id, gateway_id))
        if self.should_raise_error:
            raise ValueError("orchestration not enabled")

    def orch_release(self, device_id: str) -> None:
        self.calls.append(("orch_release", device_id))
        if self.should_raise_error:
            raise ValueError("orchestration not enabled")

    def orch_pin(self, device_id: str, pinned: bool) -> None:
        self.calls.append(("orch_pin", device_id, pinned))
        if self.should_raise_error:
            raise ValueError("orchestration not enabled")


def test_orch_set_mode():
    """POST /api/orchestrator/mode calls engine.orch_set_mode with parsed mode."""
    engine = FakeEngine()
    app = create_app(engine)
    client = TestClient(app)

    response = client.post("/api/orchestrator/mode", json={"mode": "failover"})

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert engine.calls == [("orch_set_mode", "failover")]


def test_orch_set_mode_error():
    """POST /api/orchestrator/mode returns 400 when engine raises ValueError."""
    engine = FakeEngine(should_raise_error=True)
    app = create_app(engine)
    client = TestClient(app)

    response = client.post("/api/orchestrator/mode", json={"mode": "failover"})

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "orchestration not enabled"}


def test_orch_connect():
    """POST /api/orchestrator/connect calls engine.orch_connect with parsed args."""
    engine = FakeEngine()
    app = create_app(engine)
    client = TestClient(app)

    response = client.post(
        "/api/orchestrator/connect", json={"dev": "device123", "gw": "gateway456"}
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert engine.calls == [("orch_connect", "device123", "gateway456")]


def test_orch_connect_error():
    """POST /api/orchestrator/connect returns 400 when engine raises ValueError."""
    engine = FakeEngine(should_raise_error=True)
    app = create_app(engine)
    client = TestClient(app)

    response = client.post(
        "/api/orchestrator/connect", json={"dev": "device123", "gw": "gateway456"}
    )

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "orchestration not enabled"}


def test_orch_release():
    """POST /api/orchestrator/release calls engine.orch_release with parsed device."""
    engine = FakeEngine()
    app = create_app(engine)
    client = TestClient(app)

    response = client.post("/api/orchestrator/release", json={"dev": "device789"})

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert engine.calls == [("orch_release", "device789")]


def test_orch_release_error():
    """POST /api/orchestrator/release returns 400 when engine raises ValueError."""
    engine = FakeEngine(should_raise_error=True)
    app = create_app(engine)
    client = TestClient(app)

    response = client.post("/api/orchestrator/release", json={"dev": "device789"})

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "orchestration not enabled"}


def test_orch_pin():
    """POST /api/orchestrator/pin calls engine.orch_pin with parsed args."""
    engine = FakeEngine()
    app = create_app(engine)
    client = TestClient(app)

    response = client.post(
        "/api/orchestrator/pin", json={"dev": "device123", "pinned": True}
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert engine.calls == [("orch_pin", "device123", True)]


def test_orch_pin_false():
    """POST /api/orchestrator/pin works with pinned=False."""
    engine = FakeEngine()
    app = create_app(engine)
    client = TestClient(app)

    response = client.post(
        "/api/orchestrator/pin", json={"dev": "device123", "pinned": False}
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert engine.calls == [("orch_pin", "device123", False)]


def test_orch_pin_error():
    """POST /api/orchestrator/pin returns 400 when engine raises ValueError."""
    engine = FakeEngine(should_raise_error=True)
    app = create_app(engine)
    client = TestClient(app)

    response = client.post(
        "/api/orchestrator/pin", json={"dev": "device123", "pinned": True}
    )

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "orchestration not enabled"}
