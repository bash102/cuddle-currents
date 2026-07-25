# Handoff — `viz-reimagine` (visualization layer)

This branch reimagines the **frontend visualization layer**. The backend `StateFrame` data
contract is unchanged. If you're an AI assistant picking this up: **read
`frontend/VIZ_DATA_REFERENCE.md` first** — it's the data contract + full feature reference.

## Run it

```bash
cd frontend
python3 serve.py            # http://127.0.0.1:8081/dev.html
```

- **No build, no `npm install`** — Pixi / pixi-filters / particle-emitter are vendored under
  `frontend/vendor/` and loaded via the import map in `dev.html`.
- `serve.py` = static serving **plus** Save-to-repo (a `POST /api/preset` endpoint). Plain
  `python3 -m http.server 8081` also works, but then **Save** falls back to browser `localStorage`
  only (the **Export** button still downloads a `.preset.json`).
- ES modules require `http://`, not `file://`.

**The page:** left = the live PixiJS stage; right = dev controls. The dev controls (numbered) drive
a **simulator** (add/remove people, pick a sync scenario, edit per-person variables) — this is *not*
the backend. Under the preset title: Save / Save As / Rename / Export / Reset + grouped sliders and
the Filter / Particle / Event editors.

## Architecture

Everything renders from one object — **`StateFrame`** (`getFrame()` from `js/store.js`), produced
identically by the sim or the live `/ws` feed. Two axes:

- **Renderers** (`RENDERERS` in `js/presets/registry.js`) — the engines. Each is a
  `createX(app)` factory returning `{ container, update(frame,dt), destroy, params, controls,
  getState, setState, ... }`.
  - `nodeGraph.js` — the main engine: cohort-formation physics, metaball/PNG edges, particles,
    a **data-driven event/reaction system**, and a **Layout** mode (free / ring / scatter / column)
    so it can be organic *or* a structured chart.
  - `chord.js`, `scatter.js`, `distribution.js` — standalone chart renderers (axis-labeled).
- **Presets** (`PRESETS` in `registry.js` + files in `frontend/presets/*.preset.json`) — named
  bundles of settings applied to a renderer. Add a preset by committing a `*.preset.json`; add an
  engine by writing a factory + registering it.

The **event → reaction** system is central: the renderer emits events (activated, hr, joined, left,
disconnected, beat, removed); a preset binds reactions (particle / filter / property) with a
location + trigger. The heartbeat pulse and the cohort-join lifecycle (fade-to-master, scale-up) are
**reactions now, not hardcoded** — editable in the Events panel.

## Things that self-heal on load (so old state never hides new features)

- **Preset library**, **filter stack**, and **events** merge in anything missing on load. A preset
  saved before a feature existed (the HR event, Blur/Bevel filters, new renderers) gets it added
  automatically — no manual Reset needed.
- **`frontend/presets/*.preset.json`** are loaded into the library on startup (repo = source of
  truth). Loading works with **either** server; only **saving** needs `serve.py`.

## Gotchas (please read before debugging)

1. **Presets** save to `localStorage` by default; the **repo** copy (`frontend/presets/`) is what's
   shared. A page reload picks up newly-pulled preset files. Save writes the file but does **not**
   git-commit it — `git add frontend/presets/ && commit && push` to share.
2. **Verification limitation:** software-WebGL **screenshots hang** on particle-heavy renders. Verify
   by headless *JS* runs (dump-dom, check for zero console errors through cohort formation) **plus a
   human eyeballing the live page** — not pixel screenshots. Example headless check:
   ```bash
   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --disable-gpu \
     --use-angle=swiftshader --no-sandbox --enable-logging=stderr --dump-dom \
     --virtual-time-budget=8000 http://127.0.0.1:8081/dev.html > /dev/null 2>err.log
   grep -iE "Uncaught|TypeError|ReferenceError" err.log | grep -viE "VERBOSE|Import Map"
   ```
   (Don't wrap Chrome in `timeout` on macOS — it returns 127. Let `--virtual-time-budget` exit it.)
3. **Assets** are referenced by **path** (particle/node PNGs, emitter JSON), served from under
   `frontend/`. Node PNGs can be a single file or a **folder** (each node a random PNG). `.psd`
   sources are gitignored; commit the `.png` exports.

## Key files

| Path | What |
|---|---|
| `frontend/VIZ_DATA_REFERENCE.md` | **Start here** — StateFrame contract + full feature reference |
| `frontend/serve.py` | Dev server (static + Save-to-repo) |
| `frontend/js/presets/registry.js` | `RENDERERS` + `PRESETS` |
| `frontend/js/presets/nodeGraph.js` | Main engine (physics, layout, events, node/edge/glow visuals) |
| `frontend/js/presets/{chord,scatter,distribution}.js` | Standalone chart renderers |
| `frontend/js/presets/{filters,particles,events,dispatch}.js` | Filter stack, particle systems, event schema, reaction runtime |
| `frontend/js/show/pixiApp.js` | App shell: preset switcher, controls UI, save/load |
| `frontend/js/sim/*` | The data simulator + dev control panel |
| `frontend/presets/*.preset.json` | Shared presets (loaded on startup) |
| `frontend/assets/` | PNGs, emitter JSON, node-icon folders |
