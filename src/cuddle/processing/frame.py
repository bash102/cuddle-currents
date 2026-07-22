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
    orchestrator=None,
    ota_url_base: str | None = None,
) -> StateFrame:
    source_states = source.connection_states
    proc = cfg["processing"]

    art = cfg.get("artifact")
    # Per person, derive the expensive intermediates ONCE per frame and reuse:
    # the smoothed-HR grid over the sync window feeds both hr_var here and the
    # synchrony correlation (hr_grids, passed below); RMSSD feeds both the readout
    # and its delta. Avoids the old ~5 artifact/resample passes per person.
    hr_grids: dict[str, tuple] = {}

    people: list[PersonState] = []
    for session in store.all():
        p = session.profile
        if p.enrollment_state == EnrollmentState.retired:
            continue

        connection = _connection_for(session, source_states, now, cfg)
        session.connection = connection
        quality, flags = signal_quality.assess(session, now, cfg)

        grid_s, smooth_s = abstract.smoothed_hr_grid(
            session, now - proc["sync_window"], now, proc["resample_hz"],
            proc["hr_smooth_tau"], art,
        )
        hr_grids[p.person_id] = (grid_s, smooth_s)
        rmssd_val = abstract.rolling_rmssd(session, now, proc["rmssd_window"], art)

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
                hr=_round(abstract.current_hr(session, proc["hr_smooth_tau"], art)),
                hr_var=_round(abstract.hr_std_from_grid(smooth_s)),
                rmssd=_round(rmssd_val),
                rmssd_delta=_round(abstract.rmssd_delta_from(rmssd_val, p.calibration)),
                phase=_round(abstract.phase_at(session, now)),
                last_seen=session.last_seen,
                uptime=session.uptime(now),
                baseline_progress=enrollment.baseline_progress(p.person_id, now),
                rr_tail=session.rr.tail_values(20),
                hr_trace_tail=[_round(x) for x in session.inst_hr.tail_values(60)],
            )
        )

    sync = synchrony.compute(store.all(), now, cfg, hr_grids=hr_grids)
    unassigned = source.unassigned_devices()

    gateways = orchestrator.gateway_states() if orchestrator else []
    unserved_bands = orchestrator.unserved() if orchestrator else []

    return StateFrame(
        t=now,
        people=people,
        unassigned=unassigned,
        synchrony=SynchronyState(**sync),
        scenario=scenario,
        source=source_type,
        gateways=gateways,
        unserved=unserved_bands,
        ota_url_base=ota_url_base,
    )


def _round(x, nd: int = 2):
    if x is None:
        return None
    return round(float(x), nd)
