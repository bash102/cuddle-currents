"""Command-line entry point.

    cuddle --source sim --scenario drift_into_sync --people 6
    cuddle --source ble
    cuddle --source replay --capture captures/session.jsonl

Builds the right source, wraps it in an Engine, and serves both frontends over HTTP.
"""

from __future__ import annotations

import argparse

import uvicorn

from cuddle.app import Engine
from cuddle.core.config import load_config
from cuddle.core.models import Source


def build_engine(args) -> Engine:
    cfg = load_config(args.config)

    if args.source == "sim":
        from cuddle.sources.sim_source import SimulatorSource

        source = SimulatorSource(
            n_people=args.people,
            scenario=args.scenario,
            seed=args.seed,
            baseline_scale=args.baseline_scale,
        )
        source_type = Source.sim
    elif args.source == "ble":
        from cuddle.sources.ble_source import DirectBleSource

        rc = cfg["reconnect"]
        source = DirectBleSource(
            backoff_start=rc["backoff_start"],
            backoff_max=rc["backoff_max"],
            jitter=rc["jitter"],
        )
        source_type = Source.ble
    elif args.source == "replay":
        from cuddle.sources.sim_source import ReplaySource

        if not args.capture:
            raise SystemExit("--capture PATH is required for --source replay")
        source = ReplaySource(args.capture, loop=not args.no_loop)
        source_type = Source.sim
    else:  # pragma: no cover
        raise SystemExit(f"unknown source: {args.source}")

    return Engine(
        source,
        source_type=source_type,
        scenario=args.scenario if args.source == "sim" else None,
        config=cfg,
        enrollment_path=args.enrollment,
        capture_path=args.record,
    )


def main() -> None:
    ap = argparse.ArgumentParser(prog="cuddle", description="Cuddle Currents POC")
    ap.add_argument("--source", choices=["sim", "ble", "replay"], default="sim")
    ap.add_argument("--scenario", default="drift_into_sync",
                    help="sim scenario: independent | drift_into_sync | dropout | "
                         "cliques | sync_then_break | contagion | pacer")
    ap.add_argument("--people", type=int, default=6, help="number of simulated people")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--baseline-scale", type=float, default=0.1,
                    help="shorten the baseline capture for sim demos (1.0 = full 2 min)")
    ap.add_argument("--capture", help="JSONL file to replay (with --source replay)")
    ap.add_argument("--no-loop", action="store_true", help="don't loop the replay")
    ap.add_argument("--record", help="write every sample to this JSONL capture file")
    ap.add_argument("--config", help="path to app.yaml")
    ap.add_argument("--enrollment", default="config/enrollment.yaml")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()

    engine = build_engine(args)
    host = args.host or engine.cfg["transport"]["host"]
    port = args.port or engine.cfg["transport"]["port"]

    app = _make_app(engine)
    print(f"Cuddle Currents — Show: http://{host}:{port}/   Ops: http://{host}:{port}/ops")
    uvicorn.run(app, host=host, port=port, log_level="warning")


def _make_app(engine):
    from cuddle.transport.ws_server import create_app

    return create_app(engine)


if __name__ == "__main__":
    main()
