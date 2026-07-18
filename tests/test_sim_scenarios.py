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


def test_cliques_has_subgroups_with_weak_cross_coupling():
    s = make_scenario("cliques")
    assert s.n_groups == 2
    assert s.cross_factor < 1.0
    # within-group weight is full, cross-group is scaled down
    assert s.pair_weight(0, 0) == 1.0
    assert s.pair_weight(0, 1) == pytest.approx(s.cross_factor)
    # people are split across groups
    assert s.group_of(0) != s.group_of(1)


def test_anti_phase_two_groups_with_amplified_envelope():
    s = make_scenario("anti_phase")
    assert s.n_groups == 2
    assert s.cross_factor == 0.0  # groups do not beat-sync across
    assert s.arousal_scale > 1.0  # envelope amplified so anti-correlation dominates RSA
    assert s.group_hr_spread == 0.0  # anti lives in the shared envelope, not the rate
    assert s.group_of(0) != s.group_of(1)  # interleaved into two groups
    # coupling ramps up so the shared arousal engages
    assert s.coupling(s.ramp_start - 1) == 0.0
    assert s.coupling(s.ramp_end + 1) == pytest.approx(s.k_max)
    # other scenarios keep the neutral envelope scale (behaviour unchanged)
    assert make_scenario("drift_into_sync").arousal_scale == 1.0


def test_sync_then_break_ramps_up_then_back_to_zero():
    s = make_scenario("sync_then_break")
    assert s.coupling(s.ramp_start - 1) == 0.0
    assert s.coupling(s.ramp_end + 1) == pytest.approx(s.k_max)  # locked/held
    assert s.coupling(s.ramp_down_end + 5) == 0.0  # released
    mid_release = s.coupling((s.ramp_down_start + s.ramp_down_end) / 2)
    assert 0 < mid_release < s.k_max  # partway through the release


def test_contagion_activates_members_progressively():
    s = make_scenario("contagion")
    assert s.active_at(0, 0.0)  # seed always active
    # a later member is inactive early, active after its scheduled time
    t_join = s.spread_start + 3 * s.spread_interval
    assert not s.active_at(3, t_join - 1)
    assert s.active_at(3, t_join + 1)


def test_pacer_sets_external_rhythm():
    s = make_scenario("pacer")
    assert s.pacer is True
    assert s.pacer_k > 0
    assert s.pacer_strength(s.ramp_start - 1) == 0.0  # engages after ramp_start
    assert s.pacer_strength(s.ramp_start + 1) == pytest.approx(s.pacer_k)


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
