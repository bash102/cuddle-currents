# Level B — App-Orchestrated Gateway Assignment — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to
> implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. Read the design spec
> `docs/superpowers/specs/2026-07-20-level-b-orchestration-design.md` first — it is the source
> of truth for behavior; this plan is the build sequence.

**Goal:** Give the app full authority over which gateway connects which band — a stability-first
orchestrator driven by per-gateway MQTT `report`s, issuing `connect`/`release` `cmd`s — so a
multi-gateway deployment serves ~30 people, with enrollment able to force-connect a band.

**Architecture:** A new pure `plan(world) -> cmds` core (world model + coverage memory) wrapped
by an async orchestrator (its own aiomqtt client: subscribes `cuddle/+/report`, publishes
`cmd`/`control`). The `Engine` constructs it when `--orchestrate` is set, feeds it the pinned set
from `SessionStore`, and exposes its state via new `StateFrame` fields the Ops page renders. The
gateway firmware gains a `managed` mode. All additive to the frozen Level A contract.

**Tech Stack:** Python 3 (asyncio, aiomqtt, pydantic, pytest), FastAPI, vanilla JS frontend,
ESP-IDF/C++ firmware.

## Global Constraints

- **Additive only.** Level A topics (`cuddle/<gw>/hr|status/<dev>`, `cuddle/<gw>/online`) are
  unchanged. New topics exactly: `cuddle/<gw>/report` (retained JSON), `cuddle/<gw>/cmd`
  (`{"action":"connect"|"release","dev":"<addr>"}`, QoS 1, not retained), `cuddle/control/mode`
  (`"managed"|"opportunistic"`, retained), `cuddle/control/online` (`"1"|"0"`, retained, LWT `0`).
- **`report` payload:** `{"capacity":int,"mode":"managed"|"opportunistic","connected":[{"dev":str,"rssi":int|null}],"seen":[{"dev":str,"rssi":int|null}],"ts":int_ms}`.
- **`plan()` is pure** — `(world, pinned, pending, cfg, now) -> list[Cmd]`, no I/O, no wall-clock
  reads inside (time comes from `now`/`ts` args). This is what makes it unit-testable.
- **Stability-first:** connected bands are never moved except the bounded unserved-band rebalance.
  A **pinned** band is never released to make room and is placed with priority.
- **`StateFrame` is the only backend→frontend contract** — orchestrator state reaches Ops only
  through new `StateFrame` fields.
- **`GatewayMqttSource` is unchanged** — the orchestrator is a separate module with its own MQTT
  client; the two never share mutable state.
- **Default off.** Without `orchestrator.enabled`/`--orchestrate`, behavior is exactly today's
  (no orchestrator constructed, gateways stay opportunistic). Existing tests must stay green.
- Config defaults (seconds unless noted): `report_debounce: 0.5`, `reconcile_interval: 5.0`,
  `pending_ttl: 8.0`, `coverage_ttl: 60.0`, `rebalance_cooldown: 10.0`, `seen_ttl: 10.0`
  (firmware), `orchestrator_grace: 15.0` (firmware).

## File Structure

- `src/cuddle/core/models.py` — add `ConnectedBand`, `SeenBand`, `GatewayState`, `UnservedBand`;
  add `gateways`/`unserved` fields to `StateFrame`.
- `src/cuddle/hub/orchestration/__init__.py` — new package.
- `src/cuddle/hub/orchestration/world.py` — `WorldModel` (dataclasses) + coverage memory (pure).
- `src/cuddle/hub/orchestration/plan.py` — `Cmd`, `Pending`, `PlanCfg`, `plan()` (pure).
- `src/cuddle/hub/orchestration/orchestrator.py` — `Orchestrator` (async, aiomqtt client, loops).
- `src/cuddle/app.py` — construct/start/stop orchestrator when enabled; expose state; action methods.
- `src/cuddle/processing/frame.py` — populate `gateways`/`unserved` from the orchestrator.
- `src/cuddle/transport/ws_server.py` — `/api/orchestrator/{mode,connect,release,pin}` routes.
- `frontend/ops.html`, `frontend/js/ops/ops.js` (+ maybe `gateways.js`) — roster/unserved/override UI.
- `config/app.yaml`, `src/cuddle/core/config.py` — `orchestrator:` section + defaults.
- `src/cuddle/cli.py` — `--orchestrate` flag; wire orchestrator into the Engine.
- `tools/mock_gateway.py` — multi-gateway mode (coverage, `cmd`, `report`, `control`, auto-revert).
- `firmware/gateway-idf/main/main.cpp` — managed/opportunistic mode, scan cache, `report`, `cmd`.
- Tests: `tests/test_orchestration_world.py`, `tests/test_orchestration_plan.py`,
  `tests/test_orchestrator.py`, `tests/test_orchestration_frame.py`, `tests/test_mock_gateway_multi.py`.

---

### Task 1: StateFrame contract additions

**Files:**
- Modify: `src/cuddle/core/models.py`
- Test: `tests/test_orchestration_frame.py` (create; model construction sanity here, frame wiring in Task 6)

**Interfaces — Produces:**
```python
class ConnectedBand(BaseModel):
    dev: str
    person_id: str | None = None
    rssi: int | None = None

class SeenBand(BaseModel):
    dev: str
    rssi: int | None = None

class GatewayState(BaseModel):
    id: str
    online: bool = True
    mode: str = "opportunistic"          # "managed" | "opportunistic"
    capacity: int = 0
    connected: list[ConnectedBand] = Field(default_factory=list)
    seen: list[SeenBand] = Field(default_factory=list)

class UnservedBand(BaseModel):
    dev: str
    rssi: int | None = None
    reason: str                          # "no_capacity" | "waiting_to_advertise"
```
Add to `StateFrame`: `gateways: list[GatewayState] = Field(default_factory=list)` and
`unserved: list[UnservedBand] = Field(default_factory=list)`.

- [ ] **Step 1:** Write `tests/test_orchestration_frame.py::test_stateframe_defaults_empty_gateways` — a bare `StateFrame(t=0.0)` has `gateways == []` and `unserved == []`.
- [ ] **Step 2:** Run it; expect FAIL (fields don't exist).
- [ ] **Step 3:** Add the four models above and the two `StateFrame` fields in `models.py`.
- [ ] **Step 4:** Run; expect PASS. Also run full suite — no existing test should break (fields are optional).
- [ ] **Step 5:** Commit `feat(models): StateFrame gateways/unserved for orchestration`.

---

### Task 2: World model + coverage memory (pure)

**Files:**
- Create: `src/cuddle/hub/orchestration/__init__.py` (empty), `src/cuddle/hub/orchestration/world.py`
- Test: `tests/test_orchestration_world.py`

**Interfaces — Produces:**
```python
@dataclass
class GatewayView:
    id: str
    capacity: int
    mode: str                 # "managed" | "opportunistic"
    online: bool
    connected: dict[str, int | None]   # dev -> rssi
    seen: dict[str, int | None]        # dev -> rssi (currently advertising)
    last_report_ts: float

@dataclass
class WorldModel:
    gateways: dict[str, GatewayView] = field(default_factory=dict)
    coverage: dict[str, dict[str, tuple[int | None, float]]] = field(default_factory=dict)  # dev->gw->(rssi,ts)

    def apply_report(self, gw: str, payload: dict, now: float) -> None: ...
    def set_offline(self, gw: str, now: float) -> None: ...   # from control/online 0 or LWT
    def holder_of(self, dev: str) -> str | None: ...          # gw holding a connected dev, else None
    def connected_devs(self) -> set[str]: ...
    def advertising(self) -> dict[str, dict[str, int | None]]: ...  # dev -> {gw: rssi} for UNconnected seen bands
    def prune_coverage(self, now: float, ttl: float) -> None: ...   # drop coverage entries older than ttl
```

**Behavior:**
- `apply_report` replaces that gateway's `GatewayView` (connected/seen/capacity/mode/online=True,
  `last_report_ts=now`), and for every dev in `seen` records `coverage[dev][gw] = (rssi, now)`.
  Connected devs are NOT written to coverage (they aren't advertising; their coverage entry, if
  any, is the stale memory from before they connected — leave it to age out).
- `advertising()` returns, per unconnected dev, the map of gateways that currently `seen` it —
  used for connect-time placement. A dev connected on ANY gateway is excluded.
- `holder_of` scans `connected` maps.

- [ ] **Step 1:** Write failing tests: (a) `apply_report` populates a `GatewayView` and coverage;
  (b) a dev in two gateways' `seen` appears under both in `advertising()` with each RSSI;
  (c) once a dev is `connected` on gw A, it is excluded from `advertising()` even if still in
  another gateway's stale `seen`; (d) `holder_of` returns the connecting gateway; (e)
  `prune_coverage` drops entries older than ttl, keeps fresh; (f) `set_offline` marks the
  gateway `online=False` and clears its connected/seen.
- [ ] **Step 2:** Run; expect FAIL (module missing).
- [ ] **Step 3:** Implement `world.py`.
- [ ] **Step 4:** Run; expect PASS.
- [ ] **Step 5:** Commit `feat(orchestration): world model + coverage memory`.

---

### Task 3: The `plan()` core (pure) — the crux

**Files:**
- Create: `src/cuddle/hub/orchestration/plan.py`
- Test: `tests/test_orchestration_plan.py`

**Interfaces — Consumes:** `WorldModel` (Task 2). **Produces:**
```python
@dataclass(frozen=True)
class Cmd:
    gw: str
    action: str      # "connect" | "release"
    dev: str

@dataclass
class Pending:
    gw: str
    deadline: float  # now + pending_ttl at issue time

@dataclass
class PlanCfg:
    coverage_ttl: float = 60.0

def plan(
    world: WorldModel,
    pinned: set[str],                 # devs that must be connected (enrollment)
    pending: dict[str, Pending],      # dev -> outstanding connect
    cfg: PlanCfg,
    now: float,
    *,
    allow_rebalance: bool,            # True only on the reconcile tick (disruptive)
) -> tuple[list[Cmd], list[dict]]:    # (cmds, unserved) ; unserved item: {"dev","rssi","reason"}
```

**Algorithm (in order; a dev handled by an earlier rule is not reconsidered):**
1. Compute `connected = world.connected_devs()`, `adv = world.advertising()`.
   `active_pending = {dev: p for dev,p in pending.items() if p.deadline > now and dev not in connected}`.
   A gateway's free slots = `capacity - len(connected_on_gw) - (pending targeting it)`.
   Only `managed` + `online` gateways are eligible to receive `connect`.
2. **Pinned placement** — for each `dev in pinned` not in `connected` and not in `active_pending`:
   candidate gws = those in `adv[dev]` that are managed+online with a free slot; pick max RSSI
   (None treated as -999), tie-break fewest connected. Emit `Cmd(gw,"connect",dev)`; decrement
   that gw's free slots locally. If `dev` not in `adv` → unserved `{"waiting_to_advertise"}`. If
   in `adv` but no candidate had a slot → defer to step 4 as a pinned rebalance target.
3. **Connect-time placement** — same as step 2 for each unconnected, unpinned `dev in adv` not in
   `active_pending`, using remaining free slots.
4. **Unserved-band resolution** — for each still-unserved advertising dev (pinned first, then
   unpinned): all gws seeing it are full. Only if `allow_rebalance`: search those full gws for an
   unpinned connected `Y` such that coverage memory (entry age ≤ `coverage_ttl`) shows another
   managed+online gw (≠ the full one) with a free slot that has seen `Y`. If found, emit
   `Cmd(full_gw,"release",Y)` (one release per gateway per call) and mark this dev served-pending
   (its slot will free next tick). Else record unserved `{"no_capacity"}`.
5. Return `(cmds, unserved)`. Never emit a `connect` for a dev already connected or with live
   pending; never emit a `release` for a pinned dev.

**Tests (assert exact `Cmd` lists + unserved):**
- [ ] **Step 1:** Write failing tests:
  1. single advertising band, one managed gw with capacity → one `connect` to it.
  2. band seen by two gws, different RSSI → `connect` to the stronger; tie → fewer-connected.
  3. band already `pending` (deadline future) → no duplicate `connect`.
  4. band `connected` → no `connect`, no `release` (stability).
  5. `opportunistic`/`offline` gateways are never issued `connect`.
  6. pinned band placed before an unpinned band competing for the last slot.
  7. pinned band never released as the `Y` in a rebalance.
  8. unserved band, `allow_rebalance=False` → no cmd, unserved `no_capacity`.
  9. unserved band, `allow_rebalance=True`, a movable `Y` exists (coverage fresh) → one `release Y`.
  10. same as 9 but coverage stale (> ttl) → no release, unserved `no_capacity`.
  11. pinned band with no gateway seeing it → unserved `waiting_to_advertise`, no cmd.
- [ ] **Step 2:** Run; expect FAIL.
- [ ] **Step 3:** Implement `plan.py`.
- [ ] **Step 4:** Run; expect all PASS.
- [ ] **Step 5:** Commit `feat(orchestration): stability-first plan() core`.

---

### Task 4: Orchestrator async module

**Files:**
- Create: `src/cuddle/hub/orchestration/orchestrator.py`
- Test: `tests/test_orchestrator.py` (drive handlers directly, no broker — mirror `test_ble_source` style)

**Interfaces — Consumes:** `WorldModel`, `plan()`, `Pending`, `SessionStore`. **Produces:**
```python
class Orchestrator:
    def __init__(self, store, *, broker="127.0.0.1", port=1883, topic_prefix="cuddle",
                 report_debounce=0.5, reconcile_interval=5.0, pending_ttl=8.0,
                 coverage_ttl=60.0, rebalance_cooldown=10.0): ...
    # lifecycle
    async def start(self) -> None: ...     # publish control/mode=managed + control/online=1(LWT 0); subscribe report; spawn loops
    async def stop(self) -> None: ...      # publish control/mode=opportunistic + online 0; cancel loops
    # pure-ish testable seams:
    def _handle_report(self, gw: str, payload: bytes, now: float) -> None:   # update world, mark dirty
    def _pinned(self) -> set[str]:         # devs of sessions in ASSIGNED/BASELINING/ACTIVE with a device_id
    def _run_plan(self, now: float, *, allow_rebalance: bool) -> list[Cmd]:  # calls plan(), updates pending, returns cmds
    def force_connect(self, dev: str, gw: str) -> None: ...   # operator override -> immediate cmd
    def force_release(self, dev: str) -> None: ...
    def set_pin(self, dev: str, pinned: bool) -> None: ...     # manual pin override set (union with enrollment-derived)
    def set_mode(self, mode: str) -> None: ...                 # publish control/mode
    # state for build_frame:
    def gateway_states(self) -> list[GatewayState]: ...        # from world, mapping connected dev->person via store
    def unserved(self) -> list[UnservedBand]: ...              # last plan() unserved
```

**Behavior:**
- Own aiomqtt client loop (mirror `GatewayMqttSource._run`): subscribe `f"{prefix}/+/report"` and
  `f"{prefix}/control/online"`; on message route to `_handle_report` / world.set_offline.
- Two loops: a debounce loop (when a report marked the world dirty, wait `report_debounce`, then
  `_run_plan(allow_rebalance=False)`, publish cmds) and a reconcile loop (every
  `reconcile_interval`: `world.prune_coverage`, `_run_plan(allow_rebalance=True)` gated by
  `rebalance_cooldown`, publish cmds).
- `_run_plan`: `pinned = self._pinned() | self._manual_pins`; call `plan(...)`; register a
  `Pending(gw, now+pending_ttl)` for each connect emitted; drop pending that are now connected or
  past deadline; return cmds. Publishing `connect`/`release` is `f"{prefix}/{gw}/cmd"` with
  `json.dumps({"action":..,"dev":..})`, QoS 1.
- `force_connect`/`force_release`: publish the cmd immediately (no debounce) and, for connect,
  register pending; `force_connect` also implies a manual pin so the reconcile won't undo it.
- `_pinned` reads `store.all()`, selecting `s.profile.device_id` where `enrollment_state in
  {assigned, baselining, active}` and `device_id` is set.
- `gateway_states`/`unserved` are snapshots for `build_frame`; map `connected.dev ->
  store.person_for_device(dev)`.

**Tests (inject messages / call seams; no broker):**
- [ ] **Step 1:** Failing tests: (a) `_handle_report` builds world → `gateway_states()` reflects
  it; (b) `_run_plan` emits a connect for a lone advertising band and records pending; (c) a
  second `_run_plan` before the report confirms does not re-issue (pending guard); (d) `_pinned`
  returns only assigned/baselining/active devices with a device_id; (e) `force_connect` returns a
  cmd + marks pinned so `_run_plan` won't release it; (f) `set_offline` via control/online 0
  clears that gateway.
- [ ] **Step 2:** Run; expect FAIL.
- [ ] **Step 3:** Implement `orchestrator.py`. Factor MQTT publish behind a small `_publish(topic,
  payload, qos, retain)` so tests can substitute a recorder.
- [ ] **Step 4:** Run; expect PASS.
- [ ] **Step 5:** Commit `feat(orchestration): async orchestrator (mqtt + loops + pending)`.

---

### Task 5: Engine integration

**Files:**
- Modify: `src/cuddle/app.py`
- Test: `tests/test_orchestrator.py` (add an Engine-wiring test with a fake source + fake orchestrator)

**Interfaces — Consumes:** `Orchestrator`. **Produces (Engine):** `self.orchestrator` (an
`Orchestrator` or `None`); action methods `orch_set_mode(mode)`, `orch_connect(dev, gw)`,
`orch_release(dev)`, `orch_pin(dev, pinned)`; `_frame_loop` passes `self.orchestrator` into
`build_frame`.

**Behavior — the Engine OWNS the `SessionStore`, so it also BUILDS the orchestrator** (keeps the
store single-owned; this is the resolution of the Task 9 note):
- Constructor gains `orchestrate: bool = False`. When `orchestrate and source_type == Source.mqtt`,
  build `self.orchestrator = Orchestrator(self.store, broker=cfg["mqtt"]["broker"],
  port=cfg["mqtt"]["port"], topic_prefix=cfg["mqtt"]["topic_prefix"], **cfg["orchestrator"]-timings)`;
  else `self.orchestrator = None`. If `orchestrate` is True with a non-mqtt source, raise
  `ValueError("orchestration requires the mqtt source")`.
- `start()`: if `self.orchestrator`, `await self.orchestrator.start()` after source start.
  `stop()`: if set, `await self.orchestrator.stop()` before source stop.
- Action methods raise `ValueError("orchestration not enabled")` when `self.orchestrator is None`,
  else delegate (`orch_connect`→`force_connect`, `orch_release`→`force_release`,
  `orch_pin`→`set_pin`, `orch_set_mode`→`set_mode`).
- `_frame_loop` calls `build_frame(..., orchestrator=self.orchestrator)` (new kwarg, Task 6).

- [ ] **Step 1:** Failing tests: an Engine built with `orchestrate=True`, `source_type=mqtt`, and a
  stub mqtt-like source has `self.orchestrator` an `Orchestrator`; `orch_connect` delegates to it
  (spy on `force_connect`); `orchestrate=False` → `self.orchestrator is None` and `orch_connect`
  raises; `orchestrate=True` with `source_type=sim` raises `ValueError`. (Do not call `start()` in
  the test — it needs a broker.)
- [ ] **Step 2:** Run; FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run; PASS. Full suite green (`orchestrate` defaults False → no orchestrator).
- [ ] **Step 5:** Commit `feat(app): Engine builds + drives the orchestrator`.

---

### Task 6: build_frame gateways/unserved

**Files:**
- Modify: `src/cuddle/processing/frame.py`
- Test: `tests/test_orchestration_frame.py`

**Interfaces:** `build_frame(..., orchestrator=None)`. When non-None, set
`gateways=orchestrator.gateway_states()`, `unserved=orchestrator.unserved()`; else leave defaults.

- [ ] **Step 1:** Failing test: `build_frame` with a fake orchestrator returning one
  `GatewayState` + one `UnservedBand` puts them on the frame; with `orchestrator=None` both are `[]`.
- [ ] **Step 2:** Run; FAIL. **Step 3:** Add the kwarg + population. **Step 4:** Run; PASS + suite green.
- [ ] **Step 5:** Commit `feat(frame): surface gateway roster + unserved bands`.

---

### Task 7: REST endpoints

**Files:**
- Modify: `src/cuddle/transport/ws_server.py`
- Test: `tests/test_ws_orchestrator_routes.py` (FastAPI `TestClient` + a fake engine)

**Produces routes** (all POST, JSON `{ok:true}`; map to Engine methods from Task 5):
`/api/orchestrator/mode` `{mode}` → `orch_set_mode`; `/api/orchestrator/connect` `{dev, gw}` →
`orch_connect`; `/api/orchestrator/release` `{dev}` → `orch_release`; `/api/orchestrator/pin`
`{dev, pinned}` → `orch_pin`. A `ValueError` (orchestration disabled / bad mode) → 400.

- [ ] **Step 1:** Failing tests hitting each route against a fake engine recording the calls; and
  a 400 when the fake raises `ValueError`.
- [ ] **Step 2:** Run; FAIL. **Step 3:** Add `OrchModeBody`/`OrchConnectBody`/`OrchReleaseBody`/
  `OrchPinBody` + four routes with try/except ValueError→400. **Step 4:** Run; PASS.
- [ ] **Step 5:** Commit `feat(transport): orchestrator control routes`.

---

### Task 8: Ops UI — gateway roster, unserved, override, mode

**Files:**
- Modify: `frontend/ops.html`, `frontend/js/ops/ops.js` (add a `renderGateways(frame)` section;
  a new `frontend/js/ops/gateways.js` module is fine if ops.js is large)
- Test: manual (frontend has no test harness); verified in Task 10 end-to-end.

**Behavior (render off `frame.gateways` / `frame.unserved`):**
- A **Gateways** panel: one card per `frame.gateways[]` — `id`, online dot, `mode` badge,
  `connected.length/capacity`, a list of connected bands (`person_id` name if known else `dev`, +
  rssi), and a `seen` list. Reuse existing Ops card styling/`theme.css`.
- An **Unserved** callout listing `frame.unserved[]` (dev, rssi, reason) — visually prominent.
- **Manual override**: on a `seen` band, a "connect →" control with a gateway picker → POST
  `/api/orchestrator/connect`; on a connected band, "release" → `/api/orchestrator/release`; a
  pin toggle → `/api/orchestrator/pin`. Use the existing two-click-confirm pattern from the
  Remove button for `release`.
- **Mode toggle** at the panel header: managed ⇄ opportunistic → POST `/api/orchestrator/mode`.
- Hide the whole panel when `frame.gateways` is empty (orchestration off) so single-gateway runs
  look unchanged.

- [ ] **Step 1:** Add the panel markup to `ops.html` (hidden by default).
- [ ] **Step 2:** Implement `renderGateways(frame)` + the POST handlers in `ops.js`; call it from
  the existing frame handler.
- [ ] **Step 3:** Hard-refresh Ops against a running orchestrated session (deferred to Task 10);
  confirm roster renders, override buttons POST, mode toggles.
- [ ] **Step 4:** Commit `feat(ops): gateway roster, unserved bands, manual override`.

---

### Task 9: Config + CLI

**Files:**
- Modify: `config/app.yaml`, `src/cuddle/core/config.py` (defaults merge), `src/cuddle/cli.py`
- Test: `tests/test_config.py` (extend if present) / `tests/test_cli.py`

**Behavior:**
- `app.yaml` new section:
  ```yaml
  orchestrator:
    enabled: false
    report_debounce: 0.5
    reconcile_interval: 5.0
    pending_ttl: 8.0
    coverage_ttl: 60.0
    rebalance_cooldown: 10.0
  ```
  Ensure `core/config.py` default-merges it (match how existing sections default).
- `cli.py`: add `--orchestrate` (store_true). In `build_engine`, compute
  `orchestrate = args.orchestrate or cfg["orchestrator"]["enabled"]` and pass `orchestrate=` to the
  `Engine` constructor (the Engine builds the orchestrator from `self.store` + cfg — Task 5). Do
  NOT construct `Orchestrator` in the CLI.
- `--orchestrate` with a non-mqtt source: the `Engine` raises `ValueError` (Task 5); catch it in
  `build_engine` and re-raise as `SystemExit("--orchestrate requires --source mqtt")`.

- [ ] **Step 1:** Failing test: `load_config` exposes `orchestrator.enabled == False` by default;
  building an engine with `--orchestrate --source mqtt` attaches an orchestrator; with
  `--source sim` it exits.
- [ ] **Step 2:** Run; FAIL. **Step 3:** Implement. **Step 4:** Run; PASS + suite green.
- [ ] **Step 5:** Commit `feat(cli): --orchestrate flag + orchestrator config`.

---

### Task 10: Multi-mock-gateway harness + end-to-end

**Files:**
- Modify: `tools/mock_gateway.py` (add a multi-gateway managed mode)
- Test: `tests/test_mock_gateway_multi.py` (unit-level: the mock's report/cmd/coverage logic)

**Behavior:** a mode that runs N mock gateways against a broker, each with a configurable
**coverage set** (which band ids it "sees" + a fixed RSSI), each: publishing `report`
(connected+seen+capacity+mode) periodically, subscribing `cuddle/<gw>/cmd` (on `connect`, move a
band from seen→connected and start emitting its HR; on `release`, reverse), subscribing
`cuddle/control/mode` (managed vs opportunistic) and `cuddle/control/online` (auto-revert after
grace). A connected band is removed from ALL gateways' `seen` (advertising stops) — model this
centrally so the physical constraint holds.

- [ ] **Step 1:** Failing unit tests on the mock's pure bits: a `connect` cmd moves a band
  seen→connected and it disappears from every gateway's `seen`; `release` returns it to `seen` on
  the gateways whose coverage includes it; auto-revert flips mode after grace.
- [ ] **Step 2:** Run; FAIL. **Step 3:** Implement the multi-gateway mode + extract the pure
  bits for testing. **Step 4:** Run; PASS.
- [ ] **Step 5 (manual, human/hardware-free):** Run mosquitto + `mock_gateway` multi (e.g. 3
  gateways, 6 bands, overlapping coverage) + `cuddle --source mqtt --orchestrate`. Verify in Ops:
  all servable bands connect, an unserved band (coverage-only-on-full-gateways) is surfaced then
  served after a rebalance, enrollment force-connect pins+connects a chosen band, and killing the
  app flips gateways back to opportunistic (auto-revert). Record results.
- [ ] **Step 6:** Commit `test(orchestration): multi-mock-gateway harness + e2e`.

---

### Task 11: Firmware managed mode (ESP-IDF gateway)

**Files:**
- Modify: `firmware/gateway-idf/main/main.cpp`
- Test: on-hardware (human/hardware task).

**Behavior (extends the existing sketch; keep Level A intact as the `opportunistic` branch):**
- Add `g_mode` (`MANAGED`/`OPPORTUNISTIC`), default `OPPORTUNISTIC` at boot.
- Maintain a **scan cache**: for each advertised HR band keep `{addr, type, rssi, last_seen}`,
  expiring entries older than `seen_ttl` (~10 s). In the scan callback, update the cache instead
  of (in managed mode) auto-queuing a connect.
- **`report`**: publish retained `cuddle/<gw>/report` on change + ~2 s heartbeat with
  `capacity`, `mode`, `connected[]` (held addrs + last rssi), `seen[]` (scan cache minus
  connected), `ts`.
- Subscribe `cuddle/<gw>/cmd`: `connect` → look addr+type up in the scan cache and call
  `connectTo()`; `release` → disconnect that client. Subscribe `cuddle/control/mode` (set
  `g_mode`) and `cuddle/control/online` (track orchestrator liveness).
- **managed**: scan to fill the cache but do NOT auto-connect; only obey `cmd`.
  **opportunistic**: today's behavior (auto-connect up to `MAX_CONNECTIONS`), still publishing
  `report`.
- **auto-revert**: if `g_mode==MANAGED` and `control/online` has been `0`/absent for
  `orchestrator_grace` (~15 s), locally switch to `OPPORTUNISTIC`; snap back when it returns.

- [ ] **Step 1:** Implement the above in `main.cpp` (no host test harness for firmware).
- [ ] **Step 2:** `idf.py build` (use `firmware/gateway-idf/activate.sh` + `setup-components.sh`);
  fix compile errors.
- [ ] **Step 3 (human/hardware):** Flash; with the broker + `cuddle --orchestrate` running,
  verify: gateway boots opportunistic, flips to managed on `control/mode`, publishes `report`
  with correct `seen`/`connected`, obeys `connect`/`release` `cmd`, and auto-reverts when the app
  is killed. Validate ≥4 bands placed by the orchestrator across the gateway.
- [ ] **Step 4:** Commit `feat(firmware): managed mode — report + cmd + auto-revert`.

---

### Task 12: Docs + roadmap

**Files:**
- Modify: `docs/superpowers/roadmap.md` (mark Level B done + measured results),
  `firmware/gateway-idf/README.md` (managed-mode note), `README.md` (orchestration overview if
  the top-level README documents modes), `CLAUDE.md` (note the orchestrator module + `report`/
  `cmd`/`control` topics in the architecture section).

- [ ] **Step 1:** Update the docs to reflect shipped Level B (topics, `--orchestrate`, managed
  mode, results from Tasks 10/11).
- [ ] **Step 2:** Commit `docs: Level B orchestration shipped`.

---

## Notes for the executor

- Tasks 1-7, 9 are host-only and fully testable; do them first and keep `pytest` green throughout.
- Task 8 (Ops UI) and Task 10 step 5 need a running stack — batch the manual verification.
- Tasks 10 step 5 and 11 step 3 are the **human/hardware checkpoints** — stop and hand off there.
- If Task 9's store-ownership note pushes orchestrator construction into `Engine`, reconcile
  Task 5's interface before implementing Task 9 (single owner of `SessionStore`).
