"""Ingest hub: fan-in from the source into per-person sessions.

Source-agnostic. Pulls ``NormalizedSample``s off whichever ``SampleSource`` is
running, routes each to its ``PersonSession`` (if the device has been enrolled),
feeds any in-progress baseline, and optionally taps every sample to a JSONL capture
for later replay.

Unbound devices (person_id == device_id, not yet enrolled) are intentionally not
routed to a session — they surface only in the source's unassigned-devices list for
the Ops enrollment panel.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from cuddle.core.models import NormalizedSample
from cuddle.hub.enrollment import EnrollmentManager
from cuddle.hub.registry import SessionStore


class IngestHub:
    def __init__(
        self,
        source,
        store: SessionStore,
        enrollment: EnrollmentManager,
        *,
        capture_path: str | Path | None = None,
    ) -> None:
        self._source = source
        self._store = store
        self._enrollment = enrollment
        self._capture = Path(capture_path) if capture_path else None
        self._capture_fh = None
        self._task = None
        self._running = False
        self.samples_seen = 0

    async def start(self) -> None:
        if self._capture:
            self._capture.parent.mkdir(parents=True, exist_ok=True)
            self._capture_fh = self._capture.open("a")
        self._running = True
        import asyncio

        self._task = asyncio.create_task(self._run(), name="ingest")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            import asyncio

            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._capture_fh:
            self._capture_fh.close()

    async def _run(self) -> None:
        async for sample in self._source.subscribe():
            if not self._running:
                break
            self._route(sample)

    def _route(self, sample: NormalizedSample) -> None:
        self.samples_seen += 1
        if self._capture_fh:
            self._capture_fh.write(sample.model_dump_json() + "\n")

        session = self._store.get(sample.person_id)
        if session is not None:
            session.add_beat(sample)
            self._enrollment.on_sample(sample)
