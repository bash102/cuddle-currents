"""Engine: wires a source through the hub and processing into a broadcast loop.

Holds the whole running system (source, session store, enrollment, ingest) and, on a
fixed cadence, ticks enrollment, builds a ``StateFrame``, and hands it to any
connected WebSocket clients. Source-agnostic: swap the source in the constructor and
nothing else changes.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from cuddle.core import clock
from cuddle.core.config import load_config
from cuddle.core.models import Source, StateFrame
from cuddle.hub import ota as ota_helpers
from cuddle.hub.enrollment import EnrollmentManager
from cuddle.hub.ingest import IngestHub
from cuddle.hub.orchestration.orchestrator import Orchestrator
from cuddle.hub.registry import SessionStore
from cuddle.processing import frame as frame_builder

FIRMWARE_DIR = Path(__file__).resolve().parents[2] / "firmware_ota"

_ORCHESTRATOR_TIMING_KEYS = (
    "report_debounce",
    "reconcile_interval",
    "pending_ttl",
    "coverage_ttl",
    "rebalance_cooldown",
    "evict_cooldown",
    "settle_window",
)


class Engine:
    def __init__(
        self,
        source,
        *,
        source_type: Source,
        scenario: str | None = None,
        config: dict | None = None,
        enrollment_path: str = "config/enrollment.yaml",
        capture_path: str | None = None,
        orchestrate: bool = False,
    ) -> None:
        self.cfg = config or load_config()
        self.source = source
        self.source_type = source_type
        self.scenario = scenario
        self.store = SessionStore()
        self.enrollment = EnrollmentManager(
            self.store, source, config=self.cfg, store_path=enrollment_path
        )
        self.ingest = IngestHub(source, self.store, self.enrollment, capture_path=capture_path)
        self.latest: StateFrame | None = None
        self._clients: set = set()
        self._frame_task: asyncio.Task | None = None
        self._running = False

        # The Engine owns the SessionStore, so it also builds the Orchestrator
        # (keeps the store single-owned) -- see hub/orchestration/orchestrator.py.
        if orchestrate and source_type != Source.mqtt:
            raise ValueError("orchestration requires the mqtt source")
        if orchestrate:
            mq = self.cfg["mqtt"]
            orch_cfg = self.cfg.get("orchestrator", {})
            timings = {k: orch_cfg[k] for k in _ORCHESTRATOR_TIMING_KEYS if k in orch_cfg}
            self.orchestrator = Orchestrator(
                self.store,
                broker=mq["broker"],
                port=mq["port"],
                topic_prefix=mq["topic_prefix"],
                **timings,
            )
        else:
            self.orchestrator = None

        # Firmware storage + the LAN address gateways can reach it at (OTA).
        # `firmware_dir` is runtime state (gitignored), created eagerly so the
        # first `/api/ota` upload doesn't race directory creation.
        self.firmware_dir = FIRMWARE_DIR
        self.firmware_dir.mkdir(parents=True, exist_ok=True)
        self.ota_url_base = self._detect_ota_url_base()

    def _detect_ota_url_base(self) -> str | None:
        # `cfg` may be a partial dict handed straight to a test (bypassing
        # load_config's defaults) -- fall back to the same defaults
        # core/config.py uses rather than KeyError on a missing section.
        mq = self.cfg.get("mqtt", {})
        transport = self.cfg.get("transport", {})
        broker = mq.get("broker", "127.0.0.1")
        broker_port = mq.get("port", 1883)
        transport_host = transport.get("host", "127.0.0.1")
        transport_port = transport.get("port", 8770)

        host = ota_helpers.detect_lan_ip((broker, broker_port))
        if host is None and ota_helpers.is_routable_host(transport_host):
            host = transport_host
        if host is None:
            return None
        return f"http://{host}:{transport_port}"

    async def start(self) -> None:
        self.enrollment.load()
        self.enrollment.rebind_source()
        await self.source.start()
        await self.ingest.start()
        if self.orchestrator:
            await self.orchestrator.start()
        self._running = True
        self._frame_task = asyncio.create_task(self._frame_loop(), name="frames")

    async def stop(self) -> None:
        self._running = False
        if self._frame_task:
            self._frame_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._frame_task
        if self.orchestrator:
            await self.orchestrator.stop()
        await self.ingest.stop()
        await self.source.stop()

    async def _frame_loop(self) -> None:
        period = 1.0 / self.cfg["transport"]["frame_hz"]
        while self._running:
            now = clock.now()
            self.enrollment.tick(now)
            self.latest = frame_builder.build_frame(
                self.store,
                self.source,
                self.enrollment,
                self.cfg,
                now,
                scenario=self._scenario_name(),
                source_type=self.source_type,
                orchestrator=self.orchestrator,
                ota_url_base=self.ota_url_base,
            )
            await self._broadcast(self.latest)
            await asyncio.sleep(period)

    def _scenario_name(self) -> str | None:
        return getattr(self.source, "scenario_name", None) or self.scenario

    # ---- websocket fan-out ----------------------------------------------

    def add_client(self, ws) -> None:
        self._clients.add(ws)

    def remove_client(self, ws) -> None:
        self._clients.discard(ws)

    async def _broadcast(self, frame: StateFrame) -> None:
        if not self._clients:
            return
        payload = frame.model_dump_json()
        dead = []
        for ws in list(self._clients):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    # ---- actions (called by REST routes) --------------------------------

    def enroll(self, device_id: str, display_name: str, color: str | None = None):
        return self.enrollment.assign(device_id, display_name, color)

    def rebind(self, person_id: str, device_id: str) -> None:
        self.enrollment.rebind(person_id, device_id)

    def reassign(self, device_id: str, person_id: str) -> None:
        self.enrollment.assign_device(device_id, person_id)

    def release(self, person_id: str) -> None:
        self.enrollment.release_device(person_id)

    def start_baseline(self, person_id: str) -> None:
        self.enrollment.start_baseline(person_id)

    def retire(self, person_id: str) -> None:
        self.enrollment.retire(person_id)

    def set_sync_mode(self, mode: str) -> None:
        if mode not in ("raw", "zscore", "baseline_delta"):
            raise ValueError(f"bad sync mode: {mode}")
        self.cfg["processing"]["sync_mode"] = mode

    def set_scenario(self, name: str) -> None:
        setter = getattr(self.source, "set_scenario", None)
        if setter is None:
            raise ValueError("scenario control is only available with the simulator")
        setter(name)
        self.scenario = name

    # ---- orchestration actions (called by REST routes) -------------------

    def orch_set_mode(self, mode: str) -> None:
        self._require_orchestrator().set_mode(mode)

    def orch_connect(self, device_id: str, gateway_id: str) -> None:
        self._require_orchestrator().force_connect(device_id, gateway_id)

    def orch_release(self, device_id: str) -> None:
        self._require_orchestrator().force_release(device_id)

    def orch_pin(self, device_id: str, pinned: bool) -> None:
        self._require_orchestrator().set_pin(device_id, pinned)

    def _require_orchestrator(self) -> Orchestrator:
        if self.orchestrator is None:
            raise ValueError("orchestration not enabled")
        return self.orchestrator
