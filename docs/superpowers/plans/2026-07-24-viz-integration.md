# Viz Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the merged PixiJS visualizer the live `/` Show off the real `/ws` feed, host its authoring UI at `/viz-settings`, and hold the active preset server-side so a second machine can drive the Show live.

**Architecture:** A small server-side `VizConfigStore` holds the active preset config (opaque JSON), persists it, and fans it out over a new `/ws/viz` WebSocket. `/` runs `pixiApp` chrome-free and applies pushed configs; `/viz-settings` runs `pixiApp` with its authoring UI and auto-pushes edits. FastAPI gains static mounts and a presets manifest so the frontend loads with no build step. The `StateFrame` data path is untouched.

**Tech Stack:** Python 3.11 / FastAPI / Starlette WebSockets / pydantic (backend); vanilla ES-module JS + vendored PixiJS (frontend, no build step); pytest + `fastapi.testclient` (tests).

## Global Constraints

- Python `>=3.11`; **no new dependencies** — stdlib `json`/`pathlib`/`re` + existing FastAPI only.
- Frontend has **no build step**; ES modules served live, verified by hard-refresh. No bundler, no `npm`.
- Preset-file writes MUST sanitize the id and re-check the resolved path is inside `frontend/presets/` (mirror the OTA `/firmware` traversal guard in `transport/ws_server.py`).
- The `StateFrame` contract (`core/models.py`) and the `/ws` data broadcast stay **unchanged**; viz config travels on its own `/ws/viz` channel.
- Settings/preset endpoints are **unauthenticated** (trusted-LAN POC, same posture as `/api/ota`).
- Runtime state files are gitignored (like `config/enrollment.yaml`): add `config/viz_active.json`.

---

### Task 1: `VizConfigStore` — server-held active preset

**Files:**
- Create: `src/cuddle/transport/viz_config.py`
- Create: `tests/test_viz_config.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: nothing (leaf module).
- Produces: `VizConfigStore(path: str | Path)` with `.load() -> None`, `.get() -> dict | None`, `.set(config: dict) -> None` (persists, does NOT broadcast), `.add_client(ws) -> None`, `.remove_client(ws) -> None`, `async .broadcast() -> None` (sends `json.dumps(get())` to every client, prunes dead ones). Message payload on the wire is the config dict itself, or `null`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_viz_config.py
import json

import pytest

from cuddle.transport.viz_config import VizConfigStore


def test_set_get_roundtrip(tmp_path):
    s = VizConfigStore(tmp_path / "viz.json")
    assert s.get() is None
    s.set({"id": "node-graph", "params": {"gravityK": 2}})
    assert s.get() == {"id": "node-graph", "params": {"gravityK": 2}}


def test_set_persists_and_load_restores(tmp_path):
    p = tmp_path / "viz.json"
    VizConfigStore(p).set({"id": "chord"})
    assert json.loads(p.read_text()) == {"id": "chord"}
    fresh = VizConfigStore(p)
    assert fresh.get() is None  # not loaded yet
    fresh.load()
    assert fresh.get() == {"id": "chord"}


def test_load_missing_file_is_noop(tmp_path):
    s = VizConfigStore(tmp_path / "absent.json")
    s.load()
    assert s.get() is None


class _FakeWS:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send_text(self, txt):
        if self.fail:
            raise RuntimeError("dead socket")
        self.sent.append(txt)


@pytest.mark.asyncio
async def test_broadcast_sends_current_config_and_prunes_dead(tmp_path):
    s = VizConfigStore(tmp_path / "viz.json")
    s.set({"id": "node-graph"})
    good, dead = _FakeWS(), _FakeWS(fail=True)
    s.add_client(good)
    s.add_client(dead)
    await s.broadcast()
    assert json.loads(good.sent[-1]) == {"id": "node-graph"}
    assert dead not in s._clients  # dead socket removed
    assert good in s._clients
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_viz_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cuddle.transport.viz_config'`.

(If `pytest.mark.asyncio` errors as unknown, the async test can't run — see Step 3's note; we avoid the plugin by running the coroutine with `asyncio.run` instead. Rewrite the async test body accordingly if the marker is unsupported.)

- [ ] **Step 3: Write the implementation**

```python
# src/cuddle/transport/viz_config.py
"""Server-authoritative active visualization preset.

Holds one opaque config dict (the frontend's ``getState()`` blob — the backend never
interprets it), persists it to a runtime JSON file, and fans it out to ``/ws/viz``
subscribers so every open Show updates live when an operator edits ``/viz-settings``.
The sync core (``get``/``set``/``load``) is side-effect-contained (persist only); the
route calls ``set`` then ``await broadcast``, keeping this unit testable with no loop.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class VizConfigStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._config: dict | None = None
        self._clients: set = set()

    # ---- state ----------------------------------------------------------
    def load(self) -> None:
        if not self._path.exists():
            return
        try:
            self._config = json.loads(self._path.read_text())
        except (ValueError, OSError):
            logger.warning("viz config unreadable at %s; ignoring", self._path)

    def get(self) -> dict | None:
        return self._config

    def set(self, config: dict) -> None:
        self._config = config
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(config))

    # ---- fan-out --------------------------------------------------------
    def add_client(self, ws) -> None:
        self._clients.add(ws)

    def remove_client(self, ws) -> None:
        self._clients.discard(ws)

    async def broadcast(self) -> None:
        payload = json.dumps(self._config)
        dead = []
        for ws in list(self._clients):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)
```

For the async test, if `pytest-asyncio` is not installed (it is not a dependency), replace the marker with an `asyncio.run` driver:

```python
def test_broadcast_sends_current_config_and_prunes_dead(tmp_path):
    import asyncio
    s = VizConfigStore(tmp_path / "viz.json")
    s.set({"id": "node-graph"})
    good, dead = _FakeWS(), _FakeWS(fail=True)
    s.add_client(good); s.add_client(dead)
    asyncio.run(s.broadcast())
    assert json.loads(good.sent[-1]) == {"id": "node-graph"}
    assert dead not in s._clients and good in s._clients
```

- [ ] **Step 4: Add the gitignore entry**

Append under the "Runtime enrollment store" block in `.gitignore`:

```
# Runtime active-visualization config (written by the app)
config/viz_active.json
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_viz_config.py -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add src/cuddle/transport/viz_config.py tests/test_viz_config.py .gitignore
git commit -m "feat(viz): VizConfigStore — server-held active preset + fan-out"
```

---

### Task 2: Wire `VizConfigStore` into the Engine

**Files:**
- Modify: `src/cuddle/app.py`
- Modify: `tests/test_orchestrator.py` (add one test near the existing Engine tests)

**Interfaces:**
- Consumes: `VizConfigStore` (Task 1).
- Produces: `engine.viz_config: VizConfigStore`, constructed in `Engine.__init__` at `config/viz_active.json`, `load()`ed in `Engine.start()`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_orchestrator.py  (add with the other Engine tests)
def test_engine_builds_viz_config_store():
    from cuddle.transport.viz_config import VizConfigStore
    engine = _engine()  # existing helper: Engine(_StubMqttSource(), source_type=Source.mqtt)
    assert isinstance(engine.viz_config, VizConfigStore)
    assert engine.viz_config.get() is None  # nothing set/loaded yet
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_orchestrator.py::test_engine_builds_viz_config_store -v`
Expected: FAIL — `AttributeError: 'Engine' object has no attribute 'viz_config'`.

- [ ] **Step 3: Implement the wiring**

In `src/cuddle/app.py`, add the import near the other transport imports:

```python
from cuddle.transport.viz_config import VizConfigStore
```

In `Engine.__init__`, after `self.ingest = IngestHub(...)` (before the orchestrator block is fine), add:

```python
        # Server-authoritative active viz preset (broadcast to Show clients over /ws/viz).
        self.viz_config = VizConfigStore("config/viz_active.json")
```

In `Engine.start()`, after `self.enrollment.load()`, add:

```python
        self.viz_config.load()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_orchestrator.py -v`
Expected: PASS (the new test plus all existing Engine tests unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/cuddle/app.py tests/test_orchestrator.py
git commit -m "feat(viz): Engine builds + loads the VizConfigStore"
```

---

### Task 3: Viz-config routes — static mounts, `/api/viz/active`, `/ws/viz`

**Files:**
- Modify: `src/cuddle/transport/ws_server.py`
- Create: `tests/test_ws_viz_routes.py`

**Interfaces:**
- Consumes: `engine.viz_config` (Task 2).
- Produces: `GET /api/viz/active` → the config dict or `null`; `POST /api/viz/active` (body = config dict) → `{"ok": true}`, sets + broadcasts; `WS /ws/viz` → sends the current config on connect, holds open. Static mounts for `/vendor`, `/assets`, `/presets`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ws_viz_routes.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ws_viz_routes.py -v`
Expected: FAIL — 404s / missing routes.

- [ ] **Step 3: Implement the routes**

In `src/cuddle/transport/ws_server.py`, add `Request` to the FastAPI import:

```python
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
```

Replace the existing single `/js` mount block:

```python
    if (FRONTEND / "js").exists():
        app.mount("/js", StaticFiles(directory=FRONTEND / "js"), name="js")
```

with a loop that also mounts the viz asset dirs:

```python
    for _name in ("js", "vendor", "assets", "presets"):
        _dir = FRONTEND / _name
        if _dir.exists():
            app.mount(f"/{_name}", StaticFiles(directory=_dir), name=_name)
```

Add the viz-config routes (near the other `/api/*` routes):

```python
    # ---- viz config (server-authoritative active preset) ----------------

    @app.get("/api/viz/active")
    async def viz_active_get() -> JSONResponse:
        return JSONResponse(engine.viz_config.get())

    @app.post("/api/viz/active")
    async def viz_active_set(request: Request) -> JSONResponse:
        config = await request.json()
        engine.viz_config.set(config)
        await engine.viz_config.broadcast()
        return JSONResponse({"ok": True})

    @app.websocket("/ws/viz")
    async def ws_viz(sock: WebSocket) -> None:
        import json as _json

        await sock.accept()
        engine.viz_config.add_client(sock)
        await sock.send_text(_json.dumps(engine.viz_config.get()))
        try:
            while True:
                await sock.receive_text()  # client is receive-only; keeps the socket open
        except WebSocketDisconnect:
            pass
        finally:
            engine.viz_config.remove_client(sock)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_ws_viz_routes.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/cuddle/transport/ws_server.py tests/test_ws_viz_routes.py
git commit -m "feat(viz): /api/viz/active + /ws/viz + vendor/assets/presets mounts"
```

---

### Task 4: Preset library API — manifest + save/delete

**Files:**
- Modify: `src/cuddle/transport/ws_server.py`
- Modify: `tests/test_ws_viz_routes.py`

**Interfaces:**
- Consumes: nothing new (reads/writes `frontend/presets/`).
- Produces: `GET /api/presets` → `["/presets/<name>.preset.json", ...]`; `POST /api/preset` (body has `id`) → writes `frontend/presets/<safe-id>.preset.json`, returns `{"ok": true, "file": ...}`; `POST /api/preset/delete` (body `{"id"}`) → removes it. Both sanitize the id and confine the path to `frontend/presets/`.

- [ ] **Step 1: Write the failing tests**

Presets live under the module-global `FRONTEND` dir; the tests monkeypatch it to `tmp_path` so the repo isn't touched.

```python
# tests/test_ws_viz_routes.py  (append)
import cuddle.transport.ws_server as ws_server


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ws_viz_routes.py -k preset -v`
Expected: FAIL — routes missing (404).

- [ ] **Step 3: Implement the routes**

In `src/cuddle/transport/ws_server.py`, add near the top (after imports):

```python
import json
import re

_PRESET_ID_RE = re.compile(r"[^a-z0-9_-]+")


def _safe_preset_name(pid) -> str:
    """Sanitize a preset id to a bare filename stem (matches tools/serve.py.safe_name)."""
    fn = _PRESET_ID_RE.sub("-", str(pid or "preset").lower()).strip("-")
    return fn or "preset"
```

Add the routes (near the viz-config routes from Task 3):

```python
    # ---- preset library (repo = shared source of truth) -----------------

    @app.get("/api/presets")
    async def presets_list() -> JSONResponse:
        d = FRONTEND / "presets"
        names = sorted(p.name for p in d.glob("*.preset.json")) if d.exists() else []
        return JSONResponse([f"/presets/{n}" for n in names])

    @app.post("/api/preset")
    async def preset_save(request: Request) -> JSONResponse:
        data = await request.json()
        presets_dir = (FRONTEND / "presets").resolve()
        name = _safe_preset_name(data.get("id")) + ".preset.json"
        path = (presets_dir / name).resolve()
        if path.parent != presets_dir:
            raise HTTPException(400, "bad preset id")
        presets_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
        return JSONResponse({"ok": True, "file": f"presets/{name}"})

    @app.post("/api/preset/delete")
    async def preset_delete(request: Request) -> JSONResponse:
        data = await request.json()
        presets_dir = (FRONTEND / "presets").resolve()
        name = _safe_preset_name(data.get("id")) + ".preset.json"
        path = (presets_dir / name).resolve()
        if path.parent == presets_dir and path.is_file():
            path.unlink()
        return JSONResponse({"ok": True})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_ws_viz_routes.py -v`
Expected: PASS (all Task 3 + Task 4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/cuddle/transport/ws_server.py tests/test_ws_viz_routes.py
git commit -m "feat(viz): preset manifest + save/delete (path-confined) API"
```

---

### Task 5: `pixiApp` — `chrome` flag, `applyConfig`/`getState`, manifest load

**Files:**
- Modify: `frontend/js/show/pixiApp.js`

**Interfaces:**
- Consumes: `GET /api/presets` (Task 4).
- Produces: `startPixiApp({ mount, chrome = true })` returns `{ app, select, applyConfig, getState }`. `chrome:false` runs with no on-screen authoring UI. `applyConfig(config)` applies a pushed config (setState if same renderer, else switch). `getState()` returns the current config blob (`{id,label,renderer,...state}`).

No automated test (frontend has no harness). Manual verification in Step 4.

- [ ] **Step 1: Add the `chrome` flag and gate the authoring DOM**

Change the signature (line ~131):

```js
export async function startPixiApp({ mount, chrome = true }) {
```

Change the three chrome-element creations (lines ~141-143) to create-always, append-only-when-chrome:

```js
  const openBtn = document.createElement("div"); openBtn.id = "preset-open";
  const dialog = document.createElement("div"); dialog.id = "preset-dialog";
  const ctrlPanel = document.createElement("div"); ctrlPanel.id = "preset-ctrl";
  if (chrome) document.body.append(openBtn, dialog, ctrlPanel);
```

(The elements still exist detached, so `refreshOpenBtn()` / `buildControls()` set `innerHTML` on them harmlessly when `chrome` is false — nothing visible, no crash.)

Wrap the global keydown handler (the `addEventListener("keydown", …)` block near line ~619) so preset hotkeys don't fire on the Show:

```js
  if (chrome) addEventListener("keydown", (e) => {
    // …existing handler body unchanged…
  });
```

- [ ] **Step 2: Load repo presets from the manifest instead of a directory listing**

In `loadRepoPresets()` (line ~170-171), replace the `listAssets` call:

```js
  async function loadRepoPresets() {
    let files; try { files = await (await fetch("/api/presets")).json(); } catch { return; }
```

(The rest of `loadRepoPresets` is unchanged — `files` is still an array of `/presets/*.preset.json` paths.)

- [ ] **Step 3: Add `applyConfig` + `getState` and export them**

Just before the final `return { app, select };` (line ~633), add:

```js
  function currentConfig() {
    if (!current?.getState) return null;
    const e = libEntry(currentId);
    return { id: currentId, label: e?.label || currentId, renderer: e?.renderer, ...current.getState() };
  }
  function applyConfig(config) {
    if (!config) return;
    const id = config.id || "__active__";
    let e = libEntry(id);
    if (!e) { e = { id, label: config.label || id, renderer: config.renderer, state: config }; library.push(e); }
    else { e.label = config.label || e.label; e.renderer = config.renderer; e.state = config; }
    const sameRenderer = current && libEntry(currentId)?.renderer === config.renderer;
    if (currentId === id && sameRenderer && current?.setState) current.setState(config);
    else select(id);
  }
```

Change the return line to:

```js
  return { app, select, applyConfig, getState: currentConfig };
```

- [ ] **Step 4: Manual verification (harness still works)**

The existing `dev.html` harness must be unaffected (it calls `startPixiApp({ mount })`, so `chrome` defaults true).

Run: `cd frontend && python3 serve.py` then open `http://127.0.0.1:8081/dev.html`.
Expected: the switcher + control panel appear as before; presets load (now via `/api/presets` when served by `serve.py`… note `serve.py` has no `/api/presets` — see the note below); no console errors.

Note: `serve.py` does not implement `/api/presets`, so under `serve.py` the repo-preset auto-load silently no-ops (the `catch { return; }` handles it) — the built-in presets still work. The manifest path is exercised by the FastAPI app in Task 6. This is acceptable: `dev.html` is the offline harness; the hosted app is the manifest's consumer.

- [ ] **Step 5: Commit**

```bash
git add frontend/js/show/pixiApp.js
git commit -m "feat(viz): pixiApp chrome flag + applyConfig/getState + manifest load"
```

---

### Task 6: Frontend pages + `/ws/viz` client + page routes

**Files:**
- Create: `frontend/show.html` (rewrite — overwrites the old puddle markup)
- Create: `frontend/viz-settings.html`
- Create: `frontend/puddle.html` (the OLD show markup, verbatim)
- Create: `frontend/js/vizConfigClient.js`
- Modify: `src/cuddle/transport/ws_server.py` (add `/viz-settings` + `/puddle` routes)
- Modify: `tests/test_ws_viz_routes.py` (assert the pages serve)

**Interfaces:**
- Consumes: `startPixiApp({chrome})` + `applyConfig`/`getState` (Task 5); `connect()` from `js/ws.js`; `/ws/viz`, `/api/viz/active` (Task 3).
- Produces: `/` (Show), `/viz-settings`, `/puddle` routes serving the pages.

- [ ] **Step 1: Preserve the old puddle at `frontend/puddle.html`**

Create `frontend/puddle.html` with the CURRENT contents of `frontend/show.html` (the canvas `#puddle` page that imports `js/show/puddle.js`). Copy it verbatim first, before Step 2 overwrites `show.html`:

```bash
git mv frontend/show.html frontend/puddle.html
```

(Using `git mv` keeps history; Step 2 recreates `frontend/show.html` fresh.)

- [ ] **Step 2: Write the new Show page `frontend/show.html`**

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Cuddle Currents</title>
    <link rel="stylesheet" href="/theme.css" />
    <style>
      html, body { margin: 0; height: 100%; background: #150a10; overflow: hidden; }
      #stage { position: absolute; inset: 0; }
      #stage canvas { display: block; }
    </style>
    <script type="importmap">
      { "imports": { "pixi.js": "/vendor/pixi.min.mjs" } }
    </script>
  </head>
  <body>
    <div id="stage"></div>
    <script type="module">
      import { connect } from "./js/ws.js";
      import { startPixiApp } from "./js/show/pixiApp.js";
      import { subscribeVizConfig } from "./js/vizConfigClient.js";

      const DEFAULT_PRESET = "node-graph";
      connect(); // live /ws -> shared store
      const pix = await startPixiApp({ mount: document.getElementById("stage"), chrome: false });
      subscribeVizConfig((config) => {
        if (config) pix.applyConfig(config);
        else pix.select(DEFAULT_PRESET);
      });
      window.pix = pix;
    </script>
  </body>
</html>
```

- [ ] **Step 3: Write `frontend/js/vizConfigClient.js`**

```js
// Subscribes to the server-authoritative active viz config over /ws/viz and hands each
// pushed config (or null) to a callback. Auto-reconnects like ws.js. The server sends the
// current config immediately on connect, so a late-joining Show still renders the latest.

export function subscribeVizConfig(onConfig) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}/ws/viz`;
  let retry = 500;
  function open() {
    const sock = new WebSocket(url);
    sock.onopen = () => { retry = 500; };
    sock.onmessage = (ev) => {
      try { onConfig(JSON.parse(ev.data)); } catch { /* ignore malformed */ }
    };
    sock.onclose = () => { setTimeout(open, retry); retry = Math.min(5000, retry * 2); };
    sock.onerror = () => sock.close();
  }
  open();
}
```

- [ ] **Step 4: Write `frontend/viz-settings.html`**

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Cuddle Currents · Viz Settings</title>
    <link rel="stylesheet" href="/theme.css" />
    <style>
      html, body { margin: 0; height: 100%; background: #150a10; overflow: hidden; }
      #stage { position: absolute; inset: 0; }
      #stage canvas { display: block; }
    </style>
    <script type="importmap">
      { "imports": { "pixi.js": "/vendor/pixi.min.mjs" } }
    </script>
  </head>
  <body>
    <div id="stage"></div>
    <script type="module">
      import { connect } from "./js/ws.js";
      import { startPixiApp } from "./js/show/pixiApp.js";

      connect(); // live /ws -> store (drive test scenarios from /ops)
      const pix = await startPixiApp({ mount: document.getElementById("stage"), chrome: true });

      // Adopt whatever the Show is currently displaying (server-authoritative), so a second
      // machine starts in sync rather than from this browser's local last-used preset.
      let last = "";
      try {
        const active = await (await fetch("/api/viz/active")).json();
        if (active) { pix.applyConfig(active); last = JSON.stringify(pix.getState()); }
      } catch {}

      // Auto-push edits to the server, debounced by diffing getState() each tick. The
      // `last` seed above prevents echoing the adopted config straight back.
      setInterval(() => {
        const st = pix.getState();
        if (!st) return;
        const s = JSON.stringify(st);
        if (s === last) return;
        last = s;
        fetch("/api/viz/active", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: s,
        }).catch(() => {});
      }, 300);

      window.pix = pix;
    </script>
  </body>
</html>
```

- [ ] **Step 5: Add the page routes**

In `src/cuddle/transport/ws_server.py`, next to the existing `@app.get("/ops")` route, add:

```python
    @app.get("/viz-settings")
    async def viz_settings() -> FileResponse:
        return FileResponse(FRONTEND / "viz-settings.html")

    @app.get("/puddle")
    async def puddle() -> FileResponse:
        return FileResponse(FRONTEND / "puddle.html")
```

(The existing `@app.get("/")` already serves `FRONTEND / "show.html"` — now the new Pixi Show — so it needs no change.)

- [ ] **Step 6: Add route-serving assertions**

```python
# tests/test_ws_viz_routes.py  (append)
def test_pages_serve(tmp_path):
    client = _client(tmp_path)
    for path in ("/", "/viz-settings", "/puddle", "/ops"):
        assert client.get(path).status_code == 200
```

- [ ] **Step 7: Run the backend suite**

Run: `python -m pytest tests/test_ws_viz_routes.py -v`
Expected: PASS, including `test_pages_serve`.

- [ ] **Step 8: Manual frontend verification (headless + eyeball)**

Start the app on the LAN so the vendored assets load:

```bash
cuddle --source sim --scenario drift_into_sync --people 6 --host 0.0.0.0
```

Headless console check (per HANDOFF.md) on both new pages:

```bash
for p in "" "viz-settings"; do
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --disable-gpu \
    --use-angle=swiftshader --no-sandbox --enable-logging=stderr --dump-dom \
    --virtual-time-budget=8000 "http://127.0.0.1:8770/$p" > /dev/null 2>"err-$p.log"
  echo "== /$p =="; grep -iE "Uncaught|TypeError|ReferenceError" "err-$p.log" | grep -viE "VERBOSE|Import Map" || echo "clean"
done
```

Expected: `clean` for both. Then eyeball in a real browser:
- `/` shows the Pixi visualizer full-screen, **no** switcher/control panel, dots reacting to the sim feed. Switch scenarios in `/ops` → the Show responds.
- `/viz-settings` shows the same stage **with** the switcher + control panel. Change a preset / drag a slider → within ~300 ms the change appears on `/` (open both in two windows).
- `/puddle` still shows the old force-directed puddle.

Delete the temp logs: `rm -f err-.log err-viz-settings.log`.

- [ ] **Step 9: Commit**

```bash
git add frontend/show.html frontend/viz-settings.html frontend/puddle.html \
        frontend/js/vizConfigClient.js src/cuddle/transport/ws_server.py tests/test_ws_viz_routes.py
git commit -m "feat(viz): Show off live feed at /, authoring at /viz-settings, old puddle at /puddle"
```

---

### Final: full suite + branch wrap-up

- [ ] **Run the entire test suite**

Run: `python -m pytest -q`
Expected: all pass (existing 245 + the new viz-config/route tests).

- [ ] **Confirm the design's acceptance criteria** (manual, from Step 8 of Task 6): `/` live + chrome-free, `/viz-settings` edits reach `/` across two windows, `/ops` scenarios drive both, `/puddle` intact.

- [ ] Report completion and offer to open a PR `feat/viz-integration -> main` (matches the repo's PR workflow).
