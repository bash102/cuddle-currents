"""Assemble a ``StateFrame`` each tick — the single object both frontends render.

Pulls together per-person connection lifecycle, signal quality, the abstract signals,
and the cross-person synchrony into one broadcastable snapshot.
"""

from __future__ import annotations

import numpy as np

from cuddle.core.models import (
    ConnectionState,
    EnrollmentState,
    PersonState,
    Source,
    StateFrame,
    SynchronyState,
)
from cuddle.processing import abstract, signal_quality, synchrony


def _connection_for(session, source_states: dict, now: float, cfg: dict) -> ConnectionState:
    dev = session.profile.device_id
    base = source_states.get(dev, ConnectionState.disconnected) if dev else ConnectionState.disconnected
    # Overlay staleness: connected but no beat within factor * expected RR.
    if base == ConnectionState.connected and session.last_seen is not None:
        t, rr = session.rr.arrays()
        expected = float(np.median(rr)) if rr.size else 1.0
        if (now - session.last_seen) > cfg["processing"]["stale_after_rr_factor"] * expected:
            return ConnectionState.stale
    return base


def build_frame(
    store,
    source,
    enrollment,
    cfg: dict,
    now: float,
    *,
    scenario: str | None,
    source_type: Source,
) -> StateFrame:
    source_states = source.connection_states
    proc = cfg["processing"]

    people: list[PersonState] = []
    for session in store.all():
        p = session.profile
        if p.enrollment_state == EnrollmentState.retired:
            continue

        connection = _connection_for(session, source_states, now, cfg)
        session.connection = connection
        quality, flags = signal_quality.assess(session, now, cfg)

        people.append(
            PersonState(
                person_id=p.person_id,
                display_name=p.display_name,
                color=p.color,
                shape=p.shape,
                seat=p.seat,
                device_id=p.device_id,
                connection=connection,
                enrollment=p.enrollment_state,
                quality=round(quality, 3),
                quality_flags=flags,
                hr=_round(abstract.current_hr(session, proc["hr_smooth_tau"])),
                rmssd=_round(abstract.rolling_rmssd(session, now, proc["rmssd_window"])),
                rmssd_delta=_round(abstract.rmssd_delta(session, now, proc["rmssd_window"])),
                phase=_round(abstract.phase_at(session, now)),
                last_seen=session.last_seen,
                uptime=session.uptime(now),
                baseline_progress=enrollment.baseline_progress(p.person_id, now),
                rr_tail=session.rr.tail_values(20),
                hr_trace_tail=[_round(x) for x in session.inst_hr.tail_values(60)],
            )
        )

    sync = synchrony.compute(store.all(), now, cfg)
    unassigned = source.unassigned_devices()

    return StateFrame(
        t=now,
        people=people,
        unassigned=unassigned,
        synchrony=SynchronyState(**sync),
        scenario=scenario,
        source=source_type,
    )


def _round(x, nd: int = 2):
    if x is None:
        return None
    return round(float(x), nd)
