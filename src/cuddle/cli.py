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

    # Fold --host/--port into the transport config BEFORE constructing the
    # Engine, so its OTA URL detection sees the SAME bind host uvicorn will use
    # (main() binds cfg["transport"]["host"]). Without this the Engine only ever
    # saw the app.yaml default and could never detect a LAN-reachable OTA URL.
    if args.host is not None:
        cfg["transport"]["host"] = args.host
    if args.port is not None:
        cfg["transport"]["port"] = args.port

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
            drop_after=rc["drop_after"],
            evict_after=rc["evict_after"],
            stale_after_rr_factor=cfg["processing"]["stale_after_rr_factor"],
        )
        source_type = Source.ble
    elif args.source == "replay":
        from cuddle.sources.sim_source import ReplaySource

        if not args.capture:
            raise SystemExit("--capture PATH is required for --source replay")
        source = ReplaySource(args.capture, loop=not args.no_loop)
        source_type = Source.sim
    elif args.source == "mqtt":
        from cuddle.sources.mqtt_source import GatewayMqttSource

        mq = cfg["mqtt"]
        rc = cfg["reconnect"]
        broker = args.broker or f"{mq['broker']}:{mq['port']}"
        host, _, port = broker.partition(":")
        # Fold the resolved broker back into cfg so the orchestrator (built from
        # cfg["mqtt"] inside the Engine) uses the SAME broker as the source. Without
        # this, --broker moved only the source and the orchestrator stayed on the
        # app.yaml default -- two clients on different brokers.
        mq["broker"] = host
        mq["port"] = int(port or mq["port"])
        source = GatewayMqttSource(
            broker=host,
            port=int(port or mq["port"]),
            topic_prefix=mq["topic_prefix"],
            drop_after=rc["drop_after"],
            evict_after=rc["evict_after"],
            stale_after_rr_factor=cfg["processing"]["stale_after_rr_factor"],
        )
        source_type = Source.mqtt
    else:  # pragma: no cover
        raise SystemExit(f"unknown source: {args.source}")

    orchestrate = args.orchestrate or cfg["orchestrator"]["enabled"]
    try:
        return Engine(
            source,
            source_type=source_type,
            scenario=args.scenario if args.source == "sim" else None,
            config=cfg,
            enrollment_path=args.enrollment,
            capture_path=args.record,
            orchestrate=orchestrate,
        )
    except ValueError:
        raise SystemExit("--orchestrate requires --source mqtt") from None


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="cuddle", description="Cuddle Currents POC")
    ap.add_argument("--source", choices=["sim", "ble", "replay", "mqtt"], default="sim")
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
    ap.add_argument("--broker", help="MQTT broker host:port (with --source mqtt)")
    ap.add_argument("--orchestrate", action="store_true",
                    help="run BLE->WiFi gateway orchestration (requires --source mqtt)")
    return ap


def main() -> None:
    args = build_parser().parse_args()

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
