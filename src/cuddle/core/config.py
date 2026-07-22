"""Load ``config/app.yaml`` into a plain dict with sane defaults.

One tiny module so every layer reads the same tuning constants and tests can pass an
override dict instead of touching disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_DEFAULTS: dict[str, Any] = {
    "processing": {
        "resample_hz": 4.0,
        "hr_smooth_tau": 3.0,
        "rmssd_window": 45.0,
        "sync_window": 30.0,
        "sync_max_lag": 2.0,
        "sync_mode": "zscore",
        "stale_after_rr_factor": 2.5,
        "sync_grace": 10.0,
    },
    "quality": {
        "rr_min": 0.30,
        "rr_max": 2.00,
        "ectopic_pct": 0.20,
        "dropped_ratio": 1.75,
        "coverage_window": 20.0,
    },
    "artifact": {
        "enabled": True,
        "rr_min": 0.30,
        "rr_max": 2.00,
        "hampel_window": 5,
        "hampel_sigma": 3.0,
        "min_frac": 0.20,
        "repair": True,
    },
    "baseline": {
        "duration": 120.0,
        "min_quality": 0.6,
        "min_beats": 60,
    },
    "transport": {
        "host": "127.0.0.1",
        "port": 8770,
        "frame_hz": 10.0,
    },
    "reconnect": {
        "backoff_start": 1.0,
        "backoff_max": 16.0,
        "jitter": 0.3,
        "drop_after": 20.0,
        "evict_after": 120.0,
    },
    "mqtt": {
        "broker": "127.0.0.1",
        "port": 1883,
        "topic_prefix": "cuddle",
        "max_connections": 4,  # per-gateway cap (gateway/firmware hint; validated on hardware)
    },
    "orchestrator": {
        "enabled": False,
        "report_debounce": 0.5,
        "reconcile_interval": 5.0,
        "pending_ttl": 8.0,
        "coverage_ttl": 60.0,
        "rebalance_cooldown": 10.0,
        "evict_cooldown": 10.0,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    cfg = _DEFAULTS
    if path is None:
        path = Path(__file__).resolve().parents[3] / "config" / "app.yaml"
    p = Path(path)
    if p.exists():
        loaded = yaml.safe_load(p.read_text()) or {}
        cfg = _deep_merge(_DEFAULTS, loaded)
    return cfg
