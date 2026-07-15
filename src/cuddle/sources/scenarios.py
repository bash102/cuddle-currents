"""Scenarios drive the simulator's time-varying coupling structure.

A scenario shapes how the virtual hearts influence each other over elapsed session
time. The simulator asks a scenario:
- ``coupling(t)``          — global Kuramoto strength K(t) (up-ramp, hold, down-ramp).
- ``pair_weight(gi, gj)``  — relative coupling within vs across sub-groups (cliques).
- ``active_at(i, t)``      — whether person i participates yet (contagion cascade).
- ``pacer``/``pacer_hz``/``pacer_k`` — an external rhythm everyone couples toward.
- ``dropouts()``           — scheduled disconnect windows (connection lifecycle).

Scenarios are pure/deterministic given their construction args, so tests can assert
on their shape without running the simulator.
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
    # Global coupling strength K(t): 0 before ramp_start, linear up to k_max by
    # ramp_end, held, then (if a down-ramp is set) linear back to 0 by ramp_down_end.
    k_max: float = 0.0
    ramp_start: float = 0.0
    ramp_end: float = 0.0
    ramp_down_start: float = 0.0  # 0 disables the down-ramp
    ramp_down_end: float = 0.0

    # Sub-group (clique) structure: coupling within a group is full weight, across
    # groups is scaled by cross_factor. n_groups=1 => uniform all-to-all.
    # group_hr_spread biases each group's rate (bpm per group index) so distinct
    # cliques lock at distinct rates and stay visually separate instead of merging.
    n_groups: int = 1
    cross_factor: float = 1.0
    group_hr_spread: float = 0.0

    # Contagion: people activate one at a time (index 0 is the seed); inactive people
    # emit beats but do not couple yet.
    contagion: bool = False
    spread_start: float = 0.0
    spread_interval: float = 0.0

    # Pacer: an external oscillator (guided breathing) everyone couples toward.
    pacer: bool = False
    pacer_hz: float = 1.0
    pacer_k: float = 0.0

    _dropouts: list[Dropout] = field(default_factory=list)

    def coupling(self, t: float) -> float:
        """Global Kuramoto K at elapsed time t (seconds)."""
        if self.k_max <= 0.0:
            return 0.0
        # up-ramp
        if self.ramp_end > self.ramp_start:
            if t <= self.ramp_start:
                k = 0.0
            elif t < self.ramp_end:
                k = self.k_max * (t - self.ramp_start) / (self.ramp_end - self.ramp_start)
            else:
                k = self.k_max
        else:
            k = self.k_max if t >= self.ramp_start else 0.0
        # down-ramp (release)
        if self.ramp_down_end > self.ramp_down_start > 0.0:
            if t >= self.ramp_down_end:
                return 0.0
            if t > self.ramp_down_start:
                frac = (t - self.ramp_down_start) / (self.ramp_down_end - self.ramp_down_start)
                return self.k_max * (1.0 - frac)
        return k

    def group_of(self, index: int) -> int:
        if self.n_groups <= 1:
            return 0
        return index % self.n_groups

    def pair_weight(self, gi: int, gj: int) -> float:
        return 1.0 if gi == gj else self.cross_factor

    def active_at(self, index: int, t: float) -> bool:
        if not self.contagion:
            return True
        if index == 0:
            return True  # seed
        return t >= self.spread_start + index * self.spread_interval

    def pacer_strength(self, t: float) -> float:
        if not self.pacer or t < self.ramp_start:
            return 0.0
        return self.pacer_k

    def dropouts(self) -> list[Dropout]:
        return list(self._dropouts)


def make_scenario(name: str, *, n_people: int = 6) -> Scenario:
    if name == "independent":
        return Scenario(name="independent", k_max=0.0)

    if name == "drift_into_sync":
        # Baseline uncoupled, then ramp coupling well above the natural-frequency
        # spread so the puddle visibly locks.
        return Scenario(name="drift_into_sync", k_max=6.0, ramp_start=20.0, ramp_end=70.0)

    if name == "dropout":
        # Independent dynamics plus a device that roams out and comes back.
        return Scenario(
            name="dropout",
            k_max=0.0,
            _dropouts=[Dropout(device_index=0, start=15.0, duration=12.0)],
        )

    if name == "cliques":
        # Two sub-groups that each lock internally but barely couple across, so the
        # puddle forms separate clumps.
        return Scenario(
            name="cliques",
            k_max=6.0,
            ramp_start=15.0,
            ramp_end=45.0,
            n_groups=2,
            cross_factor=0.03,
            group_hr_spread=14.0,  # second clique locks ~14 bpm higher -> stays separate
        )

    if name == "sync_then_break":
        # Lock together, hold, then release coupling so the group drifts apart again.
        return Scenario(
            name="sync_then_break",
            k_max=6.0,
            ramp_start=15.0,
            ramp_end=40.0,
            ramp_down_start=70.0,
            ramp_down_end=95.0,
        )

    if name == "contagion":
        # Synchrony spreads from a seed: members join the locked group one at a time.
        return Scenario(
            name="contagion",
            k_max=6.0,
            ramp_start=6.0,
            ramp_end=18.0,
            contagion=True,
            spread_start=10.0,
            spread_interval=8.0,
        )

    if name == "pacer":
        # An external rhythm (guided breathing) everyone couples toward, pulling mixed
        # rates to a common ~63 bpm even with little person-to-person coupling.
        return Scenario(
            name="pacer",
            k_max=0.0,
            ramp_start=10.0,
            pacer=True,
            pacer_hz=1.05,
            pacer_k=1.5,
        )

    raise ValueError(f"unknown scenario: {name!r}")


SCENARIO_NAMES = [
    "independent",
    "drift_into_sync",
    "dropout",
    "cliques",
    "sync_then_break",
    "contagion",
    "pacer",
]
