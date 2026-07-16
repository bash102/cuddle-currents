"""Synthetic multi-person source — a first-class ``SampleSource``.

Each virtual person is a cardiac oscillator: a phase that advances at their heart
rate, modulated by respiratory sinus arrhythmia (so RR/RMSSD look real), and weakly
pulled toward the others by Kuramoto coupling (so a "drift into synchrony" can be
induced on demand). A beat — and therefore a ``NormalizedSample`` with its RR
interval — is emitted each time a phase wraps 2*pi, exactly as a real band notifies.

The sim also exposes fake *unassigned devices* and honours ``bind()``, so the whole
enroll -> baseline -> active setup flow can be developed and demoed without hardware.

``ReplaySource`` re-emits a recorded capture at wall-clock speed; it shares the same
Protocol so replay is just another source.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path

from cuddle.core import clock
from cuddle.core.models import ConnectionState, DeviceInfo, NormalizedSample, Source
from cuddle.sources.scenarios import Scenario, make_scenario

TWO_PI = 2.0 * math.pi

# When people are coupled they also co-regulate their overall arousal, so their HR
# *envelopes* (slow level swings) drift together, not just their beat timing. This
# shared component is what makes cross-person HR concordance (cohesion) rise with
# sync — without it, HR wiggles are pure per-person respiration and never correlate.
AROUSAL_HZ = 0.05  # ~20 s period
AROUSAL_AMP = 6.0  # bpm swing at full coupling


@dataclass
class Oscillator:
    device_id: str
    base_hr: float  # bpm
    resp_hz: float  # respiration frequency (RSA driver)
    resp_amp: float  # fractional rate modulation depth
    rsa_phase: float
    phase: float = 0.0
    last_beat_t: float | None = None
    hr_jitter: float = 0.0  # per-person slow drift amplitude (bpm)
    _drift_phase: float = 0.0


class SimulatorSource:
    def __init__(
        self,
        *,
        n_people: int = 6,
        scenario: str = "drift_into_sync",
        seed: int = 42,
        step: float = 0.02,
        baseline_scale: float = 1.0,
    ) -> None:
        self._rng = random.Random(seed)
        self._n = n_people
        self._scenario: Scenario = make_scenario(scenario, n_people=n_people)
        self._step = step
        self._baseline_scale = baseline_scale  # sim shortens baseline in demos

        self._queue: asyncio.Queue[NormalizedSample] = asyncio.Queue()
        self._oscillators: list[Oscillator] = self._build_oscillators()
        self._states: dict[str, ConnectionState] = {
            o.device_id: ConnectionState.connected for o in self._oscillators
        }
        self._bindings: dict[str, str] = {}
        self._seq: dict[str, int] = {}
        self._last_hr: dict[str, int] = {}
        self._dropout_until: dict[int, float] = {}  # osc index -> elapsed time to rejoin

        self._task: asyncio.Task | None = None
        self._running = False
        self._t0: float | None = None
        self._pacer_phase: float = 0.0
        self._arousal_phase: float = 0.0

    # ---- construction ----------------------------------------------------

    def _build_oscillators(self) -> list[Oscillator]:
        oscs = []
        for i in range(self._n):
            base_hr = self._rng.uniform(55.0, 80.0)
            oscs.append(
                Oscillator(
                    device_id=f"SIM-{i + 1:02d}",
                    base_hr=base_hr,
                    resp_hz=self._rng.uniform(0.18, 0.30),
                    resp_amp=self._rng.uniform(0.04, 0.09),
                    rsa_phase=self._rng.uniform(0, TWO_PI),
                    phase=self._rng.uniform(0, TWO_PI),
                    hr_jitter=self._rng.uniform(1.0, 3.0),
                    _drift_phase=self._rng.uniform(0, TWO_PI),
                )
            )
        return oscs

    # ---- SampleSource protocol ------------------------------------------

    async def start(self) -> None:
        self._running = True
        self._t0 = clock.now()
        self._pacer_phase = 0.0
        self._arousal_phase = 0.0
        self._schedule_dropouts()
        self._task = asyncio.create_task(self._run(), name="sim-run")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def subscribe(self):
        while True:
            yield await self._queue.get()

    @property
    def connection_states(self) -> dict[str, ConnectionState]:
        return dict(self._states)

    def unassigned_devices(self) -> list[DeviceInfo]:
        return [
            DeviceInfo(
                device_id=o.device_id,
                source=Source.sim,
                hr_bpm=self._last_hr.get(o.device_id),
                connection=self._states[o.device_id],
            )
            for o in self._oscillators
            if o.device_id not in self._bindings
        ]

    def bind(self, device_id: str, person_id: str) -> None:
        self._bindings[device_id] = person_id

    def unbind(self, device_id: str) -> None:
        self._bindings.pop(device_id, None)

    @property
    def scenario_name(self) -> str:
        return self._scenario.name

    def set_scenario(self, name: str) -> None:
        """Switch scenarios live; restarts the coupling-ramp / dropout timeline."""
        self._scenario = make_scenario(name, n_people=self._n)
        self._dropout_until.clear()
        for o in self._oscillators:
            if hasattr(o, "_drop_window"):
                delattr(o, "_drop_window")
            self._states[o.device_id] = ConnectionState.connected
        self._t0 = clock.now()
        self._pacer_phase = 0.0
        self._arousal_phase = 0.0
        self._schedule_dropouts()

    @property
    def baseline_scale(self) -> float:
        return self._baseline_scale

    # ---- simulation loop -------------------------------------------------

    def _schedule_dropouts(self) -> None:
        for d in self._scenario.dropouts():
            if 0 <= d.device_index < self._n:
                # store as (start, end) checked against elapsed time in the loop
                self._dropout_until[d.device_index] = -1.0  # armed, not yet fired
                self._oscillators[d.device_index]._drop_window = (  # type: ignore[attr-defined]
                    d.start,
                    d.start + d.duration,
                )

    async def _run(self) -> None:
        assert self._t0 is not None
        prev = clock.now()
        while self._running:
            await asyncio.sleep(self._step)
            now = clock.now()
            dt = now - prev
            prev = now
            elapsed = now - self._t0
            self._integrate(dt, elapsed, now)

    def _integrate(self, dt: float, elapsed: float, now: float) -> None:
        n = self._n
        sc = self._scenario
        k = sc.coupling(elapsed)
        pacer_k = sc.pacer_strength(elapsed)

        # Advance the external pacer (guided breathing) rhythm.
        if sc.pacer:
            self._pacer_phase = (self._pacer_phase + TWO_PI * sc.pacer_hz * dt) % TWO_PI

        # Shared arousal envelope: when people are coupled, blend a common slow HR
        # swing into everyone so their HR *levels* co-move (not just their beats),
        # scaled by how strongly they're coupled right now. The envelope is offset per
        # sub-group so distinct cliques co-move *within* but not *across* — otherwise a
        # single global envelope would correlate every group's HR and merge them.
        self._arousal_phase = (self._arousal_phase + TWO_PI * AROUSAL_HZ * dt) % TWO_PI
        arousal_gain = min(1.0, k / 4.0)

        phases = [o.phase for o in self._oscillators]
        groups = [sc.group_of(i) for i in range(n)]
        active = [sc.active_at(i, elapsed) for i in range(n)]

        for i, o in enumerate(self._oscillators):
            # Dropout handling: a roaming band emits nothing while out of range.
            drop = getattr(o, "_drop_window", None)
            if drop is not None:
                d_start, d_end = drop
                if d_start <= elapsed < d_end:
                    self._states[o.device_id] = (
                        ConnectionState.reconnecting
                        if elapsed > d_start + 2.0
                        else ConnectionState.stale
                    )
                    continue
                elif self._states[o.device_id] != ConnectionState.connected:
                    self._states[o.device_id] = ConnectionState.connected

            # Slow HR drift + respiratory sinus arrhythmia modulate instantaneous rate.
            # A per-group rate bias (cliques) keeps distinct sub-groups at distinct rates.
            o._drift_phase += TWO_PI * 0.02 * dt
            hr = o.base_hr + o.hr_jitter * math.sin(o._drift_phase)
            hr += groups[i] * sc.group_hr_spread
            if active[i] and arousal_gain > 0:
                # per-group phase offset -> each clique shares its own HR envelope
                gp = self._arousal_phase + TWO_PI * groups[i] / max(1, sc.n_groups)
                hr += arousal_gain * AROUSAL_AMP * math.sin(gp)
            rsa = 1.0 + o.resp_amp * math.sin(TWO_PI * o.resp_hz * now + o.rsa_phase)
            omega = TWO_PI * (hr / 60.0) * rsa

            coupling = 0.0
            # Mutual (person-to-person) coupling, weighted by clique structure and
            # gated by contagion activation.
            if k > 0.0 and n > 1 and active[i]:
                s = 0.0
                for j in range(n):
                    if j != i and active[j]:
                        s += sc.pair_weight(groups[i], groups[j]) * math.sin(phases[j] - o.phase)
                coupling += (k / n) * s
            # External pacer coupling.
            if pacer_k > 0.0:
                coupling += pacer_k * math.sin(self._pacer_phase - o.phase)

            o.phase += (omega + coupling) * dt

            if o.phase >= TWO_PI:
                o.phase -= TWO_PI
                self._emit_beat(o, now)

    def _emit_beat(self, o: Oscillator, now: float) -> None:
        rr = None
        if o.last_beat_t is not None:
            rr = now - o.last_beat_t
        o.last_beat_t = now
        if rr is None or rr <= 0:
            return
        hr_bpm = int(round(60.0 / rr))
        self._last_hr[o.device_id] = hr_bpm
        self._seq[o.device_id] = self._seq.get(o.device_id, 0) + 1
        person_id = self._bindings.get(o.device_id, o.device_id)
        sample = NormalizedSample(
            person_id=person_id,
            device_id=o.device_id,
            source=Source.sim,
            t_recv=now,
            hr_bpm=hr_bpm,
            rr_intervals=[rr],
            contact=True,
            raw_flags=0x10,
            seq=self._seq[o.device_id],
        )
        self._queue.put_nowait(sample)


class ReplaySource:
    """Re-emit a recorded capture (JSONL of NormalizedSample) at wall-clock speed."""

    def __init__(self, path: str, *, loop: bool = True) -> None:
        self._path = Path(path)
        self._loop = loop
        self._queue: asyncio.Queue[NormalizedSample] = asyncio.Queue()
        self._states: dict[str, ConnectionState] = {}
        self._bindings: dict[str, str] = {}
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run(), name="replay-run")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def subscribe(self):
        while True:
            yield await self._queue.get()

    @property
    def connection_states(self) -> dict[str, ConnectionState]:
        return dict(self._states)

    def unassigned_devices(self) -> list[DeviceInfo]:
        return []

    def bind(self, device_id: str, person_id: str) -> None:
        self._bindings[device_id] = person_id

    def unbind(self, device_id: str) -> None:
        self._bindings.pop(device_id, None)

    async def _run(self) -> None:
        while self._running:
            rows = self._path.read_text().splitlines()
            prev_t: float | None = None
            base = clock.now()
            for line in rows:
                if not self._running or not line.strip():
                    break
                raw = json.loads(line)
                s = NormalizedSample(**raw)
                if prev_t is not None:
                    await asyncio.sleep(max(0.0, s.t_recv - prev_t))
                prev_t = s.t_recv
                # restamp to current host clock so downstream windows are fresh
                s = s.model_copy(update={"t_recv": clock.now(), "source": Source.sim})
                if s.device_id in self._bindings:
                    s = s.model_copy(update={"person_id": self._bindings[s.device_id]})
                self._states[s.device_id] = ConnectionState.connected
                self._queue.put_nowait(s)
            if not self._loop:
                break
