"""FastAPI transport: one WebSocket stream + REST control, serving both frontends.

- ``GET /``       -> the clean Show view (frontend/show.html)
- ``GET /ops``    -> the technical Ops view (frontend/ops.html)
- ``GET /js/*``   -> shared + per-view JS modules
- ``WS  /ws``     -> StateFrame broadcast (~10 Hz), the only thing the pages render
- ``POST /api/*`` -> enrollment / baseline / sync-mode / scenario control

Both pages consume the same ``/ws`` stream and are fully independent — either can be
opened, refreshed, or closed without affecting the other.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from cuddle.hub import ota as ota_helpers

FRONTEND = Path(__file__).resolve().parents[3] / "frontend"


class EnrollBody(BaseModel):
    device_id: str
    display_name: str
    color: str | None = None


class RebindBody(BaseModel):
    person_id: str
    device_id: str


class ReassignBody(BaseModel):
    device_id: str
    person_id: str


class PersonBody(BaseModel):
    person_id: str


class ModeBody(BaseModel):
    mode: str


class ScenarioBody(BaseModel):
    scenario: str


class OrchModeBody(BaseModel):
    mode: str


class OrchConnectBody(BaseModel):
    dev: str
    gw: str


class OrchReleaseBody(BaseModel):
    dev: str


class OrchPinBody(BaseModel):
    dev: str
    pinned: bool


def create_app(engine) -> FastAPI:
    app = FastAPI(title="Cuddle Currents")

    @app.on_event("startup")
    async def _startup() -> None:
        await engine.start()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await engine.stop()

    # ---- pages ----------------------------------------------------------

    @app.get("/")
    async def show() -> FileResponse:
        return FileResponse(FRONTEND / "show.html")

    @app.get("/ops")
    async def ops() -> FileResponse:
        return FileResponse(FRONTEND / "ops.html")

    @app.get("/theme.css")
    async def theme() -> FileResponse:
        return FileResponse(FRONTEND / "theme.css", media_type="text/css")

    @app.get("/favicon.ico")
    async def favicon() -> Response:
        return Response(status_code=204)

    if (FRONTEND / "js").exists():
        app.mount("/js", StaticFiles(directory=FRONTEND / "js"), name="js")

    # ---- data -----------------------------------------------------------

    @app.get("/api/state")
    async def state() -> JSONResponse:
        if engine.latest is None:
            return JSONResponse({"t": 0, "people": [], "unassigned": []})
        return JSONResponse(engine.latest.model_dump())

    @app.websocket("/ws")
    async def ws(sock: WebSocket) -> None:
        await sock.accept()
        engine.add_client(sock)
        if engine.latest is not None:
            await sock.send_text(engine.latest.model_dump_json())
        try:
            while True:
                await sock.receive_text()  # clients don't send; keeps the socket open
        except WebSocketDisconnect:
            pass
        finally:
            engine.remove_client(sock)

    # ---- control --------------------------------------------------------

    @app.post("/api/enroll")
    async def enroll(body: EnrollBody) -> JSONResponse:
        p = engine.enroll(body.device_id, body.display_name, body.color)
        return JSONResponse({"person_id": p.person_id})

    @app.post("/api/rebind")
    async def rebind(body: RebindBody) -> JSONResponse:
        engine.rebind(body.person_id, body.device_id)
        return JSONResponse({"ok": True})

    @app.post("/api/reassign")
    async def reassign(body: ReassignBody) -> JSONResponse:
        engine.reassign(body.device_id, body.person_id)
        return JSONResponse({"ok": True})

    @app.post("/api/release")
    async def release(body: PersonBody) -> JSONResponse:
        engine.release(body.person_id)
        return JSONResponse({"ok": True})

    @app.post("/api/baseline/start")
    async def baseline_start(body: PersonBody) -> JSONResponse:
        engine.start_baseline(body.person_id)
        return JSONResponse({"ok": True})

    @app.post("/api/retire")
    async def retire(body: PersonBody) -> JSONResponse:
        engine.retire(body.person_id)
        return JSONResponse({"ok": True})

    @app.post("/api/sync-mode")
    async def sync_mode(body: ModeBody) -> JSONResponse:
        engine.set_sync_mode(body.mode)
        return JSONResponse({"ok": True, "mode": body.mode})

    @app.post("/api/scenario")
    async def scenario(body: ScenarioBody) -> JSONResponse:
        try:
            engine.set_scenario(body.scenario)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "scenario": body.scenario})

    @app.post("/api/orchestrator/mode")
    async def orch_mode(body: OrchModeBody) -> JSONResponse:
        try:
            engine.orch_set_mode(body.mode)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True})

    @app.post("/api/orchestrator/connect")
    async def orch_connect(body: OrchConnectBody) -> JSONResponse:
        try:
            engine.orch_connect(body.dev, body.gw)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True})

    @app.post("/api/orchestrator/release")
    async def orch_release(body: OrchReleaseBody) -> JSONResponse:
        try:
            engine.orch_release(body.dev)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True})

    @app.post("/api/orchestrator/pin")
    async def orch_pin(body: OrchPinBody) -> JSONResponse:
        try:
            engine.orch_pin(body.dev, body.pinned)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True})

    # ---- OTA --------------------------------------------------------------

    @app.post("/api/ota")
    async def api_ota(bin: UploadFile = File(...)) -> JSONResponse:
        orch = engine.orchestrator
        if orch is None:
            raise HTTPException(503, "OTA requires the orchestrator (run with --orchestrate)")
        if engine.ota_url_base is None:
            raise HTTPException(
                409,
                "app is not serving on a LAN address; start with --host 0.0.0.0 so "
                "gateways can reach the firmware image",
            )
        data = await bin.read()
        try:
            version = ota_helpers.parse_firmware_version(data)
            name = ota_helpers.safe_firmware_name(version)
        except ValueError as e:
            raise HTTPException(400, f"not a valid gateway firmware image: {e}") from e
        sha = ota_helpers.sha256_hex(data)
        path = engine.firmware_dir / name
        path.write_bytes(data)
        url = f"{engine.ota_url_base}/firmware/{name}"
        orch.publish_ota(url, version, sha)
        gateways = [gw.id for gw in orch.gateway_states()]
        return JSONResponse({"version": version, "sha256": sha, "url": url, "gateways": gateways})

    @app.get("/firmware/{name}")
    async def api_firmware(name: str) -> FileResponse:
        try:
            # `name` is `<version>.bin`; reuse the version validator on the
            # stem so path traversal / unsafe names are rejected the same way.
            ota_helpers.safe_firmware_name(Path(name).stem)
        except ValueError as e:
            raise HTTPException(400, "bad firmware name") from e
        path = engine.firmware_dir / name
        if not path.is_file():
            raise HTTPException(404, "unknown firmware")
        return FileResponse(path, media_type="application/octet-stream")

    return app
