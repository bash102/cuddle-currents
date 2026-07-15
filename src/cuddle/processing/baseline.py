"""Baseline rest capture -> per-person ``Calibration``.

During a short rest capture we learn the values that let us fairly compare people
whose resting physiology differs a lot: their resting HR and HR mean/std (to z-score
away the offset), their resting RMSSD (so HRV is later reported as a delta from their
own baseline, not an incomparable absolute), and their respiration rate.

The baseline is only accepted if enough clean beats were collected — otherwise the
operator is told to redo it.
"""

from __future__ import annotations

import numpy as np

from cuddle.core.models import Calibration, NormalizedSample


def rmssd(rr_seconds: list[float] | np.ndarray) -> float | None:
    """Root mean square of successive RR differences (ms). Standard short-term HRV."""
    rr = np.asarray(rr_seconds, dtype=float)
    if rr.size < 2:
        return None
    diffs_ms = np.diff(rr) * 1000.0
    return float(np.sqrt(np.mean(diffs_ms**2)))


def estimate_respiration_hz(
    t: np.ndarray, rr: np.ndarray, resample_hz: float = 4.0
) -> float | None:
    """Dominant frequency of the RR (RSA) signal, in Hz — a respiration estimate."""
    if rr.size < 8:
        return None
    # Build the RR tachogram on a uniform grid, then find the spectral peak in the
    # respiratory band (0.1-0.5 Hz).
    t0, t1 = t[0], t[-1]
    if t1 - t0 < 4.0:
        return None
    grid = np.arange(t0, t1, 1.0 / resample_hz)
    series = np.interp(grid, t, rr)
    series = series - series.mean()
    if np.allclose(series, 0):
        return None
    spec = np.abs(np.fft.rfft(series * np.hanning(len(series))))
    freqs = np.fft.rfftfreq(len(series), d=1.0 / resample_hz)
    band = (freqs >= 0.1) & (freqs <= 0.5)
    if not band.any():
        return None
    peak = freqs[band][int(np.argmax(spec[band]))]
    return float(peak)


class BaselineCollector:
    """Accumulates beats over a rest window and produces a ``Calibration``."""

    def __init__(
        self,
        *,
        duration: float,
        rr_min: float,
        rr_max: float,
        min_quality: float,
        min_beats: int,
    ) -> None:
        self.duration = duration
        self.rr_min = rr_min
        self.rr_max = rr_max
        self.min_quality = min_quality
        self.min_beats = min_beats
        self._start: float | None = None
        self._t: list[float] = []
        self._rr: list[float] = []
        self._total = 0  # all beats seen (clean + rejected)

    def start(self, now: float) -> None:
        self._start = now
        self._t.clear()
        self._rr.clear()
        self._total = 0

    def add(self, sample: NormalizedSample) -> None:
        if self._start is None:
            return
        for rr in sample.rr_intervals:
            self._total += 1
            if self.rr_min <= rr <= self.rr_max:
                self._t.append(sample.t_recv)
                self._rr.append(rr)

    def progress(self, now: float) -> float:
        if self._start is None:
            return 0.0
        return max(0.0, min(1.0, (now - self._start) / self.duration))

    def is_done(self, now: float) -> bool:
        return self._start is not None and (now - self._start) >= self.duration

    def result(self) -> tuple[Calibration | None, str]:
        """Return (calibration, reason). calibration is None if rejected."""
        clean = len(self._rr)
        if clean < self.min_beats:
            return None, f"only {clean} clean beats (need {self.min_beats})"
        quality = clean / max(1, self._total)
        if quality < self.min_quality:
            return None, f"signal quality {quality:.0%} below {self.min_quality:.0%}"

        rr = np.asarray(self._rr, dtype=float)
        t = np.asarray(self._t, dtype=float)
        inst_hr = 60.0 / rr
        cal = Calibration(
            resting_hr=float(np.median(inst_hr)),
            hr_mean=float(np.mean(inst_hr)),
            hr_std=float(np.std(inst_hr)) or 1.0,
            hrv_baseline=rmssd(rr),
            respiration_hz=estimate_respiration_hz(t, rr),
            baseline_quality=float(quality),
            baseline_at=float(t[-1]),
        )
        return cal, "ok"
