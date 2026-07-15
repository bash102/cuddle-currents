"""Scenario shaping + simulator smoke."""

import asyncio

import pytest

from cuddle.sources.scenarios import make_scenario
from cuddle.sources.sim_source import SimulatorSource


def test_independent_has_no_coupling():
    s = make_scenario("independent")
    assert s.coupling(0) == 0.0
    assert s.coupling(1000) == 0.0


def test_drift_ramps_up_monotonically():
    s = make_scenario("drift_into_sync")
    assert s.coupling(s.ramp_start - 1) == 0.0
    mid = s.coupling((s.ramp_start + s.ramp_end) / 2)
    assert 0 < mid < s.k_max
    assert s.coupling(s.ramp_end + 100) == pytest.approx(s.k_max)
    # monotonic non-decreasing across the ramp
    prev = -1.0
    for t in range(0, int(s.ramp_end) + 20, 2):
        k = s.coupling(t)
        assert k >= prev - 1e-9
        prev = k


def test_dropout_scenario_schedules_a_dropout():
    s = make_scenario("dropout")
    assert len(s.dropouts()) == 1


def test_unknown_scenario_raises():
    with pytest.raises(ValueError):
        make_scenario("nope")


def test_simulator_emits_plausible_beats():
    async def run():
        sim = SimulatorSource(n_people=3, scenario="independent", seed=1)
        await sim.start()
        got = []

        async def collect():
            async for s in sim.subscribe():
                got.append(s)

        task = asyncio.create_task(collect())
        await asyncio.sleep(2.0)
        task.cancel()
        await sim.stop()
        return got

    samples = asyncio.run(run())
    assert samples, "simulator produced no beats"
    hrs = [s.hr_bpm for s in samples]
    assert all(35 < hr < 200 for hr in hrs)
    assert all(s.rr_intervals and s.rr_intervals[0] > 0 for s in samples)
