# Viz Integration — live feed + server-authoritative config

Design spec. Integrate the PixiJS visualization layer (merged from `viz-reimagine`)
into the running app: render it off the live data feed, make it the `/` Show view,
and host its settings at `/viz-settings` with a **server-authoritative active preset**
so an operator on a *different machine* can change the Show live.

## Goal

- **`/`** — the clean, full-screen Pixi visualizer, driven by the live `/ws` `StateFrame`
  feed. No authoring chrome. Replaces the old force-directed puddle.
- **`/viz-settings`** — the authoring surface (preset switcher, control panels, save),
  also on live data. Reachable from any machine on the LAN.
- Changes made in `/viz-settings` propagate to every open `/` Show **live**, because the
  active preset is held **server-side** and broadcast.

Non-goal: reworking the visual engines themselves. The Pixi renderers/presets are taken
as-is; this is a wiring + hosting change plus a small `pixiApp` option.

## Background (what already exists)

The merged branch built the viz to read one object — `StateFrame` — from the shared
`frontend/js/store.js`, produced identically by the live `/ws` client (`frontend/js/ws.js`,
`connect()`) or by the offline harness simulator (`frontend/js/sim/*`, `mockSource.js`).
`frontend/js/show/pixiApp.js` mounts the Pixi `Application`, the preset switcher, the
per-preset control panels, and save/load. Presets are (a) built-ins in
`frontend/js/presets/registry.js`, (b) committed `frontend/presets/*.preset.json`, and
(c) a per-browser `localStorage` library.

Two facts that shape this design:

1. `startPixiApp({ mount })` **always** injects the switcher + control panel into
   `document.body`. The Show (`/`) must run without that chrome.
2. The committed `.preset.json` files load by parsing an **HTML directory listing** of
   `/presets/` (`listAssets` → `fetch("/presets/")`). FastAPI `StaticFiles` does not
   serve directory listings, so the running app needs an explicit manifest endpoint.

## Locked decisions

| # | Decision | Choice |
|---|---|---|
| 1 | Live-push transport | Dedicated `/ws/viz` WebSocket (mirrors the `/ws` StateFrame fan-out; keeps `StateFrame` pure) |
| 2 | Push granularity | Continuous **auto-push**, debounced ~250 ms, on every edit in `/viz-settings` |
| 3 | Old puddle | Kept at `/puddle` as a fallback; removable later |
| — | Settings data source | **Live `/ws`** — operators drive test scenarios from the existing `/ops` scenario controls (backend sim). The frontend sim (`js/sim/*`) survives only as the offline `dev.html` harness. |
| — | Auth | None (trusted-LAN POC, same posture as `/api/ota`) |

## Routes & responsibilities

| Route | Serves | Data | Chrome |
|---|---|---|---|
| `/` | `frontend/show.html` (rewritten) | live `/ws` | none — full-screen stage |
| `/viz-settings` | `frontend/viz-settings.html` (new) | live `/ws` | switcher + control panels + save |
| `/puddle` | `frontend/puddle.html` (old show markup) | live `/ws` | legacy fallback |
| `/ops` | unchanged | live `/ws` | drives sim scenarios via `/api/scenario` |
| `dev.html` | unchanged | **frontend sim** | offline, backend-free design harness (not an app route) |

## Data flow — two independent streams into the Show

1. **Physiology data (already built).** `ws.js connect()` → `store.js setFrame()` →
   presets `getFrame()`/`subscribe()`. Unchanged; identical to what the harness fakes
   with `mockSource`.
2. **Viz config (new).** The server holds the *active preset config* (the full
   `getState()` blob: renderer + params + filters + particles + events). `/viz-settings`
   pushes changes; `/` applies them.

```
/viz-settings (any machine)
   | edit (debounced ~250ms)
   v
POST /api/viz/active  --->  [server: VizConfigStore]  --persist-->  config/viz_active.json
                                   |
                                   | broadcast on change
                                   v
                            WS /ws/viz  --->  / (each Show client) --> pixiApp.applyConfig()
```

On `/ws/viz` connect, the server sends the current active config immediately, so a Show
opened after a change still renders the latest.

## Backend design (`src/cuddle/`)

### New module: `transport/viz_config.py` — `VizConfigStore`

Small, self-contained, no dependency on `Engine`/processing. Responsibilities:

- Hold the current active config (a plain `dict`; opaque to the backend — it is the
  frontend's `getState()` shape and the backend never interprets it).
- Persist to / restore from `config/viz_active.json` (gitignored runtime state, same
  pattern as `config/enrollment.yaml`). On first run with no file, `active` is `None`
  and the Show falls back to a built-in default preset id (see Frontend).
- Manage the set of connected `/ws/viz` subscriber sockets and broadcast the config to
  all of them on change (fan-out mirrors `Engine._broadcast`, dead sockets pruned).

Interface (sync core, testable without a broker/loop):

```python
class VizConfigStore:
    def __init__(self, path: str | Path): ...
    def load(self) -> None                      # restore from disk if present
    def get(self) -> dict | None                # current active config
    def set(self, config: dict) -> None         # replace + persist (does NOT broadcast)
    # async fan-out:
    def add_client(self, ws) -> None
    def remove_client(self, ws) -> None
    async def broadcast(self) -> None           # send get() to all clients
```

`set()` stays synchronous/side-effect-contained (persist only); the route calls
`set()` then `await broadcast()`, so the store is unit-testable with no event loop.

### `Engine` wiring (`app.py`)

`Engine` constructs a `VizConfigStore(path="config/viz_active.json")` and calls `load()`
in `start()`. Exposed as `engine.viz_config`. No frame-loop involvement — viz config is
event-driven, not per-tick.

### Routes & static mounts (`transport/ws_server.py`)

Static mounts (today only `/js` is mounted):

```python
for name in ("js", "vendor", "assets", "presets"):
    d = FRONTEND / name
    if d.exists():
        app.mount(f"/{name}", StaticFiles(directory=d), name=name)
```

Pages:

- `GET /`             → `FileResponse(frontend/show.html)`  (rewritten Show)
- `GET /viz-settings` → `FileResponse(frontend/viz-settings.html)`
- `GET /puddle`       → `FileResponse(frontend/puddle.html)`  (old markup)

Viz-config API:

- `GET  /api/viz/active` → `{"config": <dict|null>}`
- `POST /api/viz/active` → body is the config dict; `engine.viz_config.set(body)` then
  `await engine.viz_config.broadcast()`; returns `{"ok": true}`.
- `WS   /ws/viz` → on accept, register the socket, send the current config immediately,
  then hold open (client is receive-only) until disconnect; `remove_client` on close.
  Same accept/receive-to-keep-open/finally-remove shape as the existing `/ws` route.

Preset library API (replaces the harness's directory-listing dependency, and ports
`serve.py`'s save endpoints into the app so Save-to-repo works when hosted):

- `GET  /api/presets` → `["/presets/<file>.preset.json", ...]` — a manifest listing of
  `frontend/presets/*.preset.json`, the shape `listAssets` expects (so the existing
  `loadRepoPresets` path works unchanged once it calls this instead of a raw dir fetch).
- `POST /api/preset` → write `frontend/presets/<safe(id)>.preset.json` (body = a preset
  with `id`). Filename sanitized exactly as `serve.py.safe_name` (regex `[^a-z0-9_-]`),
  and the resolved path re-checked to be inside `frontend/presets/` (path-traversal
  guard, mirroring the OTA `/firmware` guard).
- `POST /api/preset/delete` → `{"id": ...}` → remove that file, same sanitization + guard.

`frontend/serve.py` stays for the offline `dev.html` workflow; the hosted app no longer
needs it.

## Frontend design (`frontend/`)

### `startPixiApp({ mount, chrome = true })` — new `chrome` flag (`js/show/pixiApp.js`)

- `chrome: true` (default; `/viz-settings`): today's behavior (openBtn/dialog/ctrlPanel),
  **plus** a debounced hook that `POST`s `current.getState()` to `/api/viz/active` on
  every committed edit (reuse the existing `commit()` seam).
- `chrome: false` (`/`): **do not** create the openBtn/dialog/ctrlPanel DOM and skip the
  controls wiring. Still build the library (needed to resolve a preset's content) and
  select a preset programmatically. Return an object exposing:
  - `applyConfig(config)` — if `config.renderer` differs from the current renderer,
    swap renderers (reuse the existing select/destroy/create path); else `setState`.
    Null/absent config → select the default preset id.
  - `destroy()`.

Both paths still `loadRepoPresets()` via `GET /api/presets`.

### New pages

- **`frontend/show.html`** (`/`) — full-screen `#stage`; module script:
  `connect()` (live `/ws` → store) + `startPixiApp({ mount, chrome:false })` +
  a `/ws/viz` client that calls `pix.applyConfig(config)` on the initial message and each
  broadcast. A configurable **default preset id** (constant near the top, e.g.
  `node-graph`) is used until/unless the server has an active config.
- **`frontend/viz-settings.html`** (`/viz-settings`) — `#stage`; module script:
  `connect()` + `startPixiApp({ mount, chrome:true })`. The authoring UI is the switcher +
  panels already in `pixiApp`; editing auto-pushes to `/api/viz/active`.
  **On load it fetches `GET /api/viz/active` and, if a config is present, adopts it as the
  starting state** — so an operator opening the page on a second machine begins from what
  the Show is *currently* displaying, not their own `localStorage` last-used. This initial
  adopt must NOT re-`POST` (a redundant echo, and with several settings clients open it
  would feedback-loop); only genuine user edits push. With no server config yet, fall back
  to the `localStorage`/last-used behavior `pixiApp` already has.
- **`frontend/puddle.html`** (`/puddle`) — the old `show.html` markup (mounts `puddle.js`),
  moved verbatim so the legacy view stays reachable.

### `/ws/viz` client

A ~30-line `frontend/js/vizConfigClient.js`: opens `ws(s)://host/ws/viz`, on each message
`JSON.parse` → `applyConfig`, with the same auto-reconnect/backoff shape as `ws.js`.

## Persistence

- **Active viz config** → `config/viz_active.json` (gitignored runtime state; the running
  server owns it, like `enrollment.yaml`). Survives restart.
- **Preset library (shared)** → committed `frontend/presets/*.preset.json` remain the
  source of truth; `/viz-settings` Save writes them via `POST /api/preset`.
- `.gitignore`: add `config/viz_active.json`.

## Testing

Backend (FastAPI `TestClient`, matching `tests/test_ws_orchestrator_routes.py` /
`tests/test_ws_ota.py` / `tests/test_ota.py`):

- `VizConfigStore`: `set` → `get` round-trip; persist → new instance `load` restores;
  `broadcast` fans out to registered fakes and prunes a dead one.
- `GET/POST /api/viz/active`: post a config, get it back; persistence file written.
- `GET /api/presets`: returns the committed files as `/presets/<name>` paths.
- `POST /api/preset` + `/api/preset/delete`: writes/removes under `frontend/presets/`;
  **path-traversal**: an `id` of `../evil` is sanitized and cannot escape the dir
  (assert the file lands inside `presets/`, mirroring the OTA traversal test).
- `/ws/viz`: on connect, the current config is sent first.

Frontend has no build/test harness (intentional — served live). Verify per the HANDOFF:
headless-Chrome `--dump-dom` with a zero-`Uncaught`/`TypeError` console check through
cohort formation on `/` and `/viz-settings`, plus a human eyeball. The plan will call out
frontend verification as manual, not automated.

## File-by-file change list

New:
- `src/cuddle/transport/viz_config.py` — `VizConfigStore`
- `frontend/viz-settings.html`
- `frontend/puddle.html` (old show markup)
- `frontend/js/vizConfigClient.js`
- `tests/test_viz_config.py`, `tests/test_ws_viz_routes.py`

Changed:
- `src/cuddle/transport/ws_server.py` — static mounts, pages, `/api/viz/*`, `/ws/viz`,
  `/api/presets`, `/api/preset`(+delete)
- `src/cuddle/app.py` — build + `load()` the `VizConfigStore`; expose `engine.viz_config`
- `frontend/show.html` — rewritten to mount the Pixi Show off live data + `/ws/viz`
- `frontend/js/show/pixiApp.js` — `chrome` flag; return `applyConfig`; settings auto-push
- `frontend/js/show/pixiApp.js` `loadRepoPresets` — use `GET /api/presets` manifest
- `.gitignore` — `config/viz_active.json`

Unchanged: `frontend/js/store.js`, `frontend/js/ws.js`, `frontend/js/sim/*`,
`frontend/dev.html`, `frontend/serve.py`, all `frontend/js/presets/*`, the backend
data pipeline (`sources`/`hub`/`processing`), `/ops`.

## Out of scope (YAGNI)

- Auth/TLS on settings endpoints (trusted LAN, same as `/api/ota`).
- Explicit "Apply to Show" button (auto-push chosen; trivial to add later).
- Multi-preset playlists, per-viewer independent configs, preset versioning/migration
  beyond what `pixiApp`'s existing self-heal already does.
