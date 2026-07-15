"""Per-person signal quality: is the raw beat-to-beat signal actually good?

Turns the recent RR stream into a 0..1 score plus human-readable flags so the Raw
panel can show good / marginal / bad at a glance. HRV and synchrony use the corrected
(artifact-excluded) beats; the Raw view shows the uncorrected trace with flags.

Flags:
  stale         no beat within stale_after_rr_factor * expected RR
  implausible   an RR outside [rr_min, rr_max] (HR outside ~30-200)
  ectopic       |RR_n - RR_{n-1}| > ectopic_pct * local median (artifact/ectopic)
  dropped       RR ~ dropped_ratio * local median (a missed beat)
  no_contact    sensor-contact bit reports off-skin
  low_coverage  received well under the expected number of beats recently
"""

from __future__ import annotations

import numpy as np


def assess(session, now: float, cfg: dict) -> tuple[float, list[str]]:
    q = cfg["quality"]
    proc = cfg["processing"]
    flags: list[str] = []
    score = 1.0

    if session.last_seen is None:
        return 0.0, ["no_data"]

    t, rr = session.rr.window(now - q["coverage_window"], now)

    # Expected RR from recent data (fallback ~1s if unknown).
    expected_rr = float(np.median(rr)) if rr.size else 1.0
    silence = now - session.last_seen
    if silence > proc["stale_after_rr_factor"] * expected_rr:
        flags.append("stale")
        score -= 0.5

    if session.contact is False:
        flags.append("no_contact")
        score -= 0.4

    if rr.size:
        implausible = np.sum((rr < q["rr_min"]) | (rr > q["rr_max"]))
        if implausible:
            flags.append("implausible")
            score -= min(0.4, 0.1 * implausible)

        if rr.size >= 3:
            med = float(np.median(rr))
            succ = np.abs(np.diff(rr))
            ectopic = np.sum(succ > q["ectopic_pct"] * med)
            if ectopic:
                flags.append("ectopic")
                score -= min(0.3, 0.05 * ectopic)
            dropped = np.sum(rr > q["dropped_ratio"] * med)
            if dropped:
                flags.append("dropped")
                score -= min(0.3, 0.1 * dropped)

        # Coverage: beats received vs expected. Use the actual elapsed time since
        # connect (capped at the window) so a freshly-connected band isn't wrongly
        # flagged low_coverage before it has had a full window to fill.
        elapsed = q["coverage_window"]
        if session.connect_since is not None:
            elapsed = min(q["coverage_window"], max(expected_rr, now - session.connect_since))
        expected_beats = elapsed / expected_rr
        got = rr.size
        cov = got / expected_beats if expected_beats > 0 else 0.0
        if cov < 0.6:
            flags.append("low_coverage")
            score -= 0.3 * (0.6 - cov) / 0.6
    else:
        flags.append("low_coverage")
        score -= 0.5

    score = max(0.0, min(1.0, score))

    # Hysteresis: smooth the score so the indicator doesn't flicker.
    prev = session.scratch.get("quality_score")
    if prev is not None:
        score = 0.6 * prev + 0.4 * score
    session.scratch["quality_score"] = score

    return score, flags


def clean_rr(t: np.ndarray, rr: np.ndarray, cfg: dict) -> tuple[np.ndarray, np.ndarray]:
    """Return (t, rr) with implausible and ectopic beats removed, for HRV/synchrony."""
    q = cfg["quality"]
    if rr.size == 0:
        return t, rr
    mask = (rr >= q["rr_min"]) & (rr <= q["rr_max"])
    t, rr = t[mask], rr[mask]
    if rr.size >= 3:
        med = float(np.median(rr))
        keep = np.ones(rr.size, dtype=bool)
        keep[1:] = np.abs(np.diff(rr)) <= q["ectopic_pct"] * med
        t, rr = t[keep], rr[keep]
    return t, rr
