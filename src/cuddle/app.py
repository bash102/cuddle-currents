"""Engine: wires a source through the hub and processing into a broadcast loop.

Holds the whole running system (source, session store, enrollment, ingest) and, on a
fixed cadence, ticks enrollment, builds a ``StateFrame``, and hands it to any
connected WebSocket clients. Source-agnostic: swap the source in the constructor and
nothing else changes.
"""

from __future__ import annotations

import asyncio
import contextlib

from cuddle.core import clock
from cuddle.core.config import load_config
from cuddle.core.models import Source, StateFrame
from cuddle.hub.enrollment import EnrollmentManager
from cuddle.hub.ingest import IngestHub
from cuddle.hub.registry import SessionStore
from cuddle.processing import frame as frame_builder


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

    async def start(self) -> None:
        self.enrollment.load()
        self.enrollment.rebind_source()
        await self.source.start()
        await self.ingest.start()
        self._running = True
        self._frame_task = asyncio.create_task(self._frame_loop(), name="frames")

    async def stop(self) -> None:
        self._running = False
        if self._frame_task:
            self._frame_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._frame_task
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
