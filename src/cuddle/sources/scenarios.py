"""Scenarios drive the simulator's time-varying parameters.

A scenario answers two questions over elapsed session time:
- ``coupling(t)`` — the Kuramoto coupling strength K between oscillators.
- ``dropouts()`` — scheduled disconnect windows to exercise the connection lifecycle.

Scenarios are pure/deterministic given their construction args, so tests can assert
on their shape (e.g. drift_into_sync ramps K up).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Dropout:
    device_index: int
    start: float  # elapsed seconds
    duration: float


@dataclass
class Scenario:
    name: str
    k_max: float = 0.0
    ramp_start: float = 0.0
    ramp_end: float = 0.0
    _dropouts: list[Dropout] = field(default_factory=list)

    def coupling(self, t: float) -> float:
        """Kuramoto K at elapsed time t (seconds)."""
        if self.k_max <= 0.0 or self.ramp_end <= self.ramp_start:
            return self.k_max if self.k_max > 0 and t >= self.ramp_start else 0.0
        if t <= self.ramp_start:
            return 0.0
        if t >= self.ramp_end:
            return self.k_max
        frac = (t - self.ramp_start) / (self.ramp_end - self.ramp_start)
        return self.k_max * frac

    def dropouts(self) -> list[Dropout]:
        return list(self._dropouts)


def make_scenario(name: str, *, n_people: int = 6) -> Scenario:
    if name == "independent":
        return Scenario(name="independent", k_max=0.0)

    if name == "drift_into_sync":
        # Baseline uncoupled for the first stretch, then ramp coupling well above the
        # natural-frequency spread so the puddle visibly locks.
        return Scenario(
            name="drift_into_sync",
            k_max=6.0,
            ramp_start=20.0,
            ramp_end=70.0,
        )

    if name == "dropout":
        # Independent dynamics plus a device that roams out and comes back.
        return Scenario(
            name="dropout",
            k_max=0.0,
            _dropouts=[Dropout(device_index=0, start=15.0, duration=12.0)],
        )

    raise ValueError(f"unknown scenario: {name!r}")
