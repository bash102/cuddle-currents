"""Enrollment lifecycle: bind a sensor to a person, then baseline them.

This is the one place a human ties hardware to a human. Everything downstream keys
on ``person_id``, so once a device is assigned the rest of the system is identity-
stable across reconnects and device swaps.

Lifecycle: DISCOVERED -> ASSIGNED -> BASELINING -> CALIBRATED -> ACTIVE (+ RETIRED).
Bindings and learned calibration are persisted to a runtime store so a restart or a
roam-out doesn't lose a session.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from cuddle.core.models import (
    Calibration,
    EnrollmentState,
    NormalizedSample,
    PersonProfile,
)
from cuddle.hub.registry import SessionStore
from cuddle.processing.baseline import BaselineCollector

# Jewel-tone categorical palette, warm/cool alternating so consecutively-enrolled
# people get maximally distinct colors. Validated with the dataviz skill's palette
# checker against the dark wine surface: lightness band, chroma, normal-vision
# separation, and surface contrast all PASS; CVD separation is a WARN in the legal
# 6-8 floor band, which the shape channel + name labels satisfy as secondary encoding.
_DEFAULT_COLORS = [
    "#3b6fe0",  # sapphire
    "#e8663f",  # coral
    "#17a2a2",  # teal
    "#e0245e",  # ruby
    "#b07914",  # gold
    "#9b5de5",  # amethyst
    "#1f9e6f",  # emerald
    "#c14fa0",  # orchid
]

# Shape channel — the second visual dimension so identity scales past the palette.
# 8 colors x 8 shapes = 64 unique (color, shape) combos, well over the 30-person cap.
# Color cycles fastest (differs for consecutive seats); shape advances once the
# palette wraps, so seats 1 and 9 share a color but differ in shape.
_SHAPES = ["disc", "ring", "triangle", "square", "diamond", "star", "hexagon", "plus"]


def identity_for_seat(seat: int) -> tuple[str, str]:
    """Map a 1-based seat number to a unique (color, shape) up to 64 people."""
    i = max(0, seat - 1)
    color = _DEFAULT_COLORS[i % len(_DEFAULT_COLORS)]
    shape = _SHAPES[(i // len(_DEFAULT_COLORS)) % len(_SHAPES)]
    return color, shape


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "person"


class EnrollmentManager:
    def __init__(
        self,
        store: SessionStore,
        source,
        *,
        config: dict,
        store_path: str | Path,
    ) -> None:
        self._store = store
        self._source = source
        self._cfg = config
        self._path = Path(store_path)
        self._baselines: dict[str, BaselineCollector] = {}
        self._baseline_status: dict[str, str] = {}  # person_id -> last reason
        self._next_seat = 1  # monotonic; never reused within a session

    # ---- enrollment actions ---------------------------------------------

    def assign(self, device_id: str, display_name: str, color: str | None = None) -> PersonProfile:
        """Bind a discovered device to a new (or renamed) person."""
        person_id = self._unique_person_id(display_name)
        seat = self._next_seat
        self._next_seat += 1
        seat_color, shape = identity_for_seat(seat)
        profile = PersonProfile(
            person_id=person_id,
            display_name=display_name,
            color=color or seat_color,
            shape=shape,
            seat=seat,
            device_id=device_id,
            enrollment_state=EnrollmentState.assigned,
        )
        self._store.create_person(profile)
        self._store.bind_device(device_id, person_id)
        self._source.bind(device_id, person_id)
        self.save()
        return profile

    def rebind(self, person_id: str, device_id: str) -> None:
        """Swap a person onto a different band (e.g. battery swap), keep identity."""
        sess = self._store.get(person_id)
        if not sess:
            raise KeyError(person_id)
        self._store.bind_device(device_id, person_id)
        self._source.bind(device_id, person_id)
        self.save()

    def start_baseline(self, person_id: str) -> None:
        sess = self._store.get(person_id)
        if not sess:
            raise KeyError(person_id)
        b = self._cfg["baseline"]
        q = self._cfg["quality"]
        scale = getattr(self._source, "baseline_scale", 1.0)
        collector = BaselineCollector(
            duration=b["duration"] * scale,
            rr_min=q["rr_min"],
            rr_max=q["rr_max"],
            min_quality=b["min_quality"],
            min_beats=int(b["min_beats"] * scale),
        )
        self._baselines[person_id] = collector
        sess.profile.enrollment_state = EnrollmentState.baselining
        self._baseline_status[person_id] = "collecting"
        # start() is stamped on the first sample's clock via tick()

    def retire(self, person_id: str) -> None:
        self._store.retire(person_id)
        self._baselines.pop(person_id, None)
        self.save()

    # ---- per-sample + per-tick hooks ------------------------------------

    def on_sample(self, sample: NormalizedSample) -> None:
        collector = self._baselines.get(sample.person_id)
        if collector is not None:
            if collector._start is None:  # lazily start on first real beat
                collector.start(sample.t_recv)
            collector.add(sample)

    def tick(self, now: float) -> None:
        """Advance baselining people; accept/reject completed captures."""
        for person_id, collector in list(self._baselines.items()):
            sess = self._store.get(person_id)
            if sess is None or collector._start is None:
                continue
            if collector.is_done(now):
                cal, reason = collector.result()
                self._baselines.pop(person_id, None)
                self._baseline_status[person_id] = reason
                if cal is not None:
                    sess.profile.calibration = cal
                    sess.profile.enrollment_state = EnrollmentState.active
                else:
                    # failed baseline: back to assigned, operator can retry
                    sess.profile.enrollment_state = EnrollmentState.assigned
                self.save()

    def baseline_progress(self, person_id: str, now: float) -> float | None:
        collector = self._baselines.get(person_id)
        if collector is None:
            return None
        return collector.progress(now)

    def baseline_reason(self, person_id: str) -> str | None:
        return self._baseline_status.get(person_id)

    # ---- persistence -----------------------------------------------------

    def load(self) -> None:
        if not self._path.exists():
            return
        data = yaml.safe_load(self._path.read_text()) or {}
        max_seat = 0
        for row in data.get("people", []):
            cal = Calibration(**row.get("calibration", {})) if row.get("calibration") else Calibration()
            seat = int(row.get("seat", 0))
            color = row.get("color")
            shape = row.get("shape")
            if seat and (not color or not shape):
                sc, ss = identity_for_seat(seat)
                color = color or sc
                shape = shape or ss
            profile = PersonProfile(
                person_id=row["person_id"],
                display_name=row["display_name"],
                color=color or "#888888",
                shape=shape or "disc",
                seat=seat,
                device_id=row.get("device_id"),
                enrollment_state=EnrollmentState(row.get("enrollment_state", "assigned")),
                calibration=cal,
            )
            self._store.create_person(profile)
            if profile.device_id:
                self._store.bind_device(profile.device_id, profile.person_id)
            max_seat = max(max_seat, seat)
        # Continue seat numbering after the highest restored seat (no reuse).
        self._next_seat = max(self._next_seat, max_seat + 1)

    def rebind_source(self) -> None:
        """Push all known device bindings into the source (call after load)."""
        for sess in self._store.all():
            if sess.profile.device_id:
                self._source.bind(sess.profile.device_id, sess.person_id)

    def save(self) -> None:
        rows = []
        for sess in self._store.all():
            p = sess.profile
            if p.enrollment_state == EnrollmentState.retired:
                continue
            rows.append(
                {
                    "person_id": p.person_id,
                    "display_name": p.display_name,
                    "color": p.color,
                    "shape": p.shape,
                    "seat": p.seat,
                    "device_id": p.device_id,
                    "enrollment_state": p.enrollment_state.value,
                    "calibration": p.calibration.model_dump(exclude_none=True),
                }
            )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(yaml.safe_dump({"people": rows}, sort_keys=False))

    # ---- helpers ---------------------------------------------------------

    def _unique_person_id(self, display_name: str) -> str:
        base = _slug(display_name)
        pid = base
        i = 2
        while self._store.get(pid) is not None:
            pid = f"{base}-{i}"
            i += 1
        return pid
