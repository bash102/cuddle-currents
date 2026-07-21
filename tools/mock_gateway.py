"""Mock BLE->WiFi gateway: publishes the ESP32 gateway MQTT contract with no ESP32.

Modes:
  replay  — re-encode a recorded capture's RR into 0x2A37 frames and publish them
            on the capture's own timing (fully hardware-free).
  bleak   — a real BLE central that republishes raw notification bytes (Task 6).
  managed — N mock gateways in one process, each with a configurable coverage
            set, implementing the Level B orchestration contract end-to-end
            with no hardware (Task 10).

Level A contract (replay/bleak): cuddle/<gw>/hr/<dev> (raw 0x2A37 bytes),
cuddle/<gw>/status/<dev> (JSON {event, rssi}), cuddle/<gw>/online (retained
"1"/"0", "0" as LWT).

Level B contract (managed), additive to Level A:
  cuddle/<gw>/report (retained)  {"capacity": int, "mode": "managed"|"opportunistic",
                                   "connected": [{"dev": str, "rssi": int|None}],
                                   "seen": [{"dev": str, "rssi": int|None}], "ts": int_ms}
  cuddle/<gw>/cmd    (in, qos1)  {"action": "connect"|"release", "dev": str}
  cuddle/control/mode   (retained) "managed" | "opportunistic"
  cuddle/control/online (retained, LWT "0") "1" | "0"

`ManagedGatewayWorld` is the pure, broker-free model shared by every mock
gateway in one managed-mode process. It centralizes the one thing that must
be centralized: a BLE band stops advertising once connected, so a `connect`
on one gateway must remove that dev from EVERY gateway's `seen`, not just the
connecting gateway's -- modeling each mock gateway's coverage independently
would let the same dev "advertise" on two gateways while connected to one,
which cannot happen physically.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field

from cuddle.sources.ble_parser import encode_hr_measurement


def build_hr_topic(prefix: str, gw: str, dev: str) -> str:
    return f"{prefix}/{gw}/hr/{dev}"


def build_status_topic(prefix: str, gw: str, dev: str) -> str:
    return f"{prefix}/{gw}/status/{dev}"


def online_topic(prefix: str, gw: str) -> str:
    return f"{prefix}/{gw}/online"


def report_topic(prefix: str, gw: str) -> str:
    return f"{prefix}/{gw}/report"


def cmd_topic(prefix: str, gw: str) -> str:
    return f"{prefix}/{gw}/cmd"


def control_mode_topic(prefix: str) -> str:
    return f"{prefix}/control/mode"


def control_online_topic(prefix: str) -> str:
    return f"{prefix}/control/online"


def status_payload(event: str, rssi: int | None = None) -> bytes:
    return json.dumps({"event": event, "rssi": rssi}).encode()


# ---- managed mode: pure, broker-free multi-gateway model -----------------------

DEFAULT_GRACE_S = 15.0
DEFAULT_REPORT_INTERVAL_S = 2.0

# Payloads on `control/online` (and `<gw>/online`, its LWT) that mean "not
# online" -- mirrors `_OFFLINE_PAYLOADS` in
# `cuddle.hub.orchestration.orchestrator`.
_OFFLINE_ONLINE_PAYLOADS = (b"0", b"", b"false")


@dataclass
class MockGateway:
    """One mock gateway's static identity: what it can see and how many
    concurrent connections it can hold. `coverage` maps dev id -> the fixed
    RSSI this gateway would observe for that dev (a test-config fiction --
    real RSSI wobbles, but a fixed-per-(gateway,dev) value is enough to
    exercise placement/rebalance logic)."""

    id: str
    capacity: int
    coverage: dict[str, int | None] = field(default_factory=dict)


class ManagedGatewayWorld:
    """Shared state across every mock gateway in one `--mode managed`
    process. No I/O, no async, no wall-clock reads -- `now` is always passed
    in by the caller, same discipline as `cuddle.hub.orchestration.world`.

    Owns:
      - `connected`: dev -> holding gateway id, the ONE piece of state that
        must be centralized (see module docstring).
      - the commanded mode (`cuddle/control/mode`) and the orchestrator's
        online presence (`cuddle/control/online`), from which every mock
        gateway's `report.mode` derives via `effective_mode` -- including
        the auto-revert-to-opportunistic-after-grace behavior real gateway
        firmware is specified to implement (see
        docs/superpowers/specs/2026-07-20-level-b-orchestration-design.md §8).

        `control/online` is published RETAINED exactly once per orchestrator
        session ("1" on start, "0" on stop/LWT) -- it is NOT a heartbeat that
        repeats while the orchestrator is healthy. So auto-revert must trip
        on a genuine online->offline TRANSITION (or on having never seen an
        online message at all), never on mere silence while online: a single
        "1" with no further messages, ever, must hold `managed` forever.
    """

    def __init__(self, gateways: list[MockGateway], *, grace: float = DEFAULT_GRACE_S) -> None:
        self.gateways: dict[str, MockGateway] = {gw.id: gw for gw in gateways}
        self.connected: dict[str, str] = {}  # dev -> holding gateway id
        self.commanded_mode: str = "opportunistic"  # boot default per spec
        self.grace = grace
        self._online: bool = False
        # No control/online message has ever arrived -- treat as offline
        # since the dawn of time, so a gateway with no orchestrator present
        # reverts to opportunistic immediately after `grace` (the safe boot
        # default), same as an explicit "0" received infinitely long ago.
        self._offline_since: float = float("-inf")

    # ---- per-gateway view ---------------------------------------------------

    def seen_for(self, gw_id: str) -> dict[str, int | None]:
        gw = self.gateways[gw_id]
        return {dev: rssi for dev, rssi in gw.coverage.items() if dev not in self.connected}

    def connected_for(self, gw_id: str) -> dict[str, int | None]:
        gw = self.gateways[gw_id]
        return {dev: gw.coverage[dev] for dev, holder in self.connected.items() if holder == gw_id}

    def report(self, gw_id: str, now: float) -> dict:
        gw = self.gateways[gw_id]
        return {
            "capacity": gw.capacity,
            "mode": self.effective_mode(now),
            "connected": [{"dev": d, "rssi": r} for d, r in self.connected_for(gw_id).items()],
            "seen": [{"dev": d, "rssi": r} for d, r in self.seen_for(gw_id).items()],
            "ts": int(now * 1000),
        }

    # ---- cmd handling ---------------------------------------------------------

    def handle_cmd(self, gw_id: str, action: str, dev: str) -> bool:
        """Apply a `{"action": ..., "dev": ...}` cmd addressed to `gw_id`.

        Returns True iff connection state actually changed, so the caller
        knows whether to start/stop the dev's HR stream and publish a status
        event -- and False for no-ops (unknown gateway, dev outside its
        coverage, already in the requested state, or at capacity).
        """
        gw = self.gateways.get(gw_id)
        if gw is None or dev not in gw.coverage:
            return False
        if action == "connect":
            if dev in self.connected:
                return False  # already connected, here or elsewhere
            if len(self.connected_for(gw_id)) >= gw.capacity:
                return False  # at capacity
            self.connected[dev] = gw_id
            return True
        if action == "release":
            if self.connected.get(dev) != gw_id:
                return False
            del self.connected[dev]
            return True
        return False

    # ---- control/mode + control/online -----------------------------------------

    def on_control_mode(self, payload: bytes) -> None:
        self.commanded_mode = payload.decode().strip()

    def on_control_online(self, payload: bytes, now: float) -> None:
        """Update the boolean online state on a genuine transition only.

        `"1"` means online. Anything in `_OFFLINE_ONLINE_PAYLOADS` (mirrors
        the orchestrator's own LWT/offline payloads) means offline. Only the
        True->False edge stamps `_offline_since` -- repeated "1"s (there
        won't be any, since it's published once retained) or repeated
        offline-ish payloads while already offline are no-ops, so silence
        while online never starts a clock.
        """
        online = payload not in _OFFLINE_ONLINE_PAYLOADS
        if online:
            self._online = True
        elif self._online:
            self._online = False
            self._offline_since = now

    def effective_mode(self, now: float) -> str:
        if self.commanded_mode != "managed":
            return "opportunistic"
        if not self._online and now - self._offline_since >= self.grace:
            return "opportunistic"
        return "managed"


def load_gateways(config_path: str) -> tuple[list[MockGateway], float]:
    """Parse a multi-gateway coverage-set config:

    {"grace": 15.0,
     "gateways": [{"id": "gw1", "capacity": 4, "coverage": {"AA:BB": -60}}, ...]}

    `grace` is optional (defaults to `DEFAULT_GRACE_S`). Returns
    (gateways, grace).
    """
    with open(config_path) as fh:
        data = json.load(fh)
    grace = float(data.get("grace", DEFAULT_GRACE_S))
    gateways = [
        MockGateway(id=g["id"], capacity=int(g["capacity"]), coverage=dict(g["coverage"]))
        for g in data["gateways"]
    ]
    return gateways, grace


def device_frames_from_capture(path: str) -> dict[str, list[tuple[float, bytes]]]:
    """Per-device (relative_seconds, raw 0x2A37 payload) frames, grouped by
    device_id with each device's own first RR-bearing row as its zero time --
    unlike `frames_from_capture`, no gateway/topic is baked in, since in
    managed mode a device's HR stream is only known to belong to a gateway
    once a `connect` cmd assigns it there."""
    by_device: dict[str, list[tuple[float, bytes]]] = {}
    t0_by_device: dict[str, float] = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if not row.get("rr_intervals"):
                continue
            dev = row["device_id"]
            if dev not in t0_by_device:
                t0_by_device[dev] = row["t_recv"]
            payload = encode_hr_measurement(
                row["hr_bpm"],
                rr_intervals=row.get("rr_intervals"),
                contact=row.get("contact"),
            )
            by_device.setdefault(dev, []).append((row["t_recv"] - t0_by_device[dev], payload))
    return by_device


def frames_from_capture(
    path: str, *, prefix: str = "cuddle", gw: str = "mock"
) -> list[tuple[float, str, bytes]]:
    """(relative_seconds, topic, raw 0x2A37 payload) for each RR-bearing capture row."""
    frames: list[tuple[float, str, bytes]] = []
    t0: float | None = None
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if not row.get("rr_intervals"):
                continue
            if t0 is None:
                t0 = row["t_recv"]
            payload = encode_hr_measurement(
                row["hr_bpm"],
                rr_intervals=row.get("rr_intervals"),
                contact=row.get("contact"),
            )
            frames.append(
                (row["t_recv"] - t0, build_hr_topic(prefix, gw, row["device_id"]), payload)
            )
    return frames


async def run_replay(path: str, broker: str, port: int, prefix: str, gw: str) -> None:
    import aiomqtt

    frames = frames_from_capture(path, prefix=prefix, gw=gw)
    devices = sorted({topic.split("/")[-1] for _, topic, _ in frames})
    will = aiomqtt.Will(online_topic(prefix, gw), b"0", qos=1, retain=True)
    async with aiomqtt.Client(broker, port, will=will) as client:
        await client.publish(online_topic(prefix, gw), b"1", qos=1, retain=True)
        for dev in devices:
            await client.publish(
                build_status_topic(prefix, gw, dev),
                json.dumps({"event": "connected", "rssi": -55}).encode(),
            )
        prev = 0.0
        for rel, topic, payload in frames:
            await asyncio.sleep(max(0.0, rel - prev))
            prev = rel
            await client.publish(topic, payload)
        await client.publish(online_topic(prefix, gw), b"0", qos=1, retain=True)


async def run_bleak(broker: str, port: int, prefix: str, gw: str) -> None:
    """Real BLE central: forward raw 0x2A37 notifications to MQTT per the contract.

    Reuses DirectBleSource for scan/connect/backoff; we tap its normalized samples
    but re-emit the *raw* frame bytes so the app decodes with its own parser. Since
    DirectBleSource yields decoded NormalizedSample (not raw bytes), we re-encode
    with encode_hr_measurement — lossless for HR/RR/contact, the fields the app uses.
    """
    import aiomqtt

    from cuddle.sources.ble_source import DirectBleSource

    ble = DirectBleSource()
    will = aiomqtt.Will(online_topic(prefix, gw), b"0", qos=1, retain=True)
    async with aiomqtt.Client(broker, port, will=will) as client:
        await client.publish(online_topic(prefix, gw), b"1", qos=1, retain=True)
        await ble.start()
        seen: set[str] = set()
        try:
            async for s in ble.subscribe():
                dev = s.device_id
                if dev not in seen:
                    seen.add(dev)
                    await client.publish(
                        build_status_topic(prefix, gw, dev), status_payload("connected")
                    )
                payload = encode_hr_measurement(
                    s.hr_bpm, rr_intervals=s.rr_intervals, contact=s.contact
                )
                await client.publish(build_hr_topic(prefix, gw, dev), payload)
        finally:
            await ble.stop()
            await client.publish(online_topic(prefix, gw), b"0", qos=1, retain=True)


# ---- managed mode: async wiring around ManagedGatewayWorld -----------------------


async def _stream_hr(
    client, prefix: str, gw: str, dev: str, frames: list[tuple[float, bytes]], stop: asyncio.Event
) -> None:
    """Replay `dev`'s captured HR frames on loop until `stop` fires. A
    connected band keeps beating for as long as it's connected, but a
    capture is a finite recording, so we loop it rather than going silent
    (as the one-shot `run_replay` does) once it's exhausted."""
    if not frames:
        return
    topic = build_hr_topic(prefix, gw, dev)
    while not stop.is_set():
        prev = 0.0
        for rel, payload in frames:
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(0.0, rel - prev))
                return  # stop fired mid-frame
            except asyncio.TimeoutError:
                pass
            prev = rel
            await client.publish(topic, payload)


async def _report_loop(
    client, world: ManagedGatewayWorld, gw_id: str, prefix: str, interval: float
) -> None:
    while True:
        payload = json.dumps(world.report(gw_id, time.time())).encode()
        await client.publish(report_topic(prefix, gw_id), payload, qos=0, retain=True)
        await asyncio.sleep(interval)


async def _cmd_loop(
    client,
    world: ManagedGatewayWorld,
    gw: MockGateway,
    prefix: str,
    capture_frames: dict[str, list[tuple[float, bytes]]],
) -> None:
    my_cmd_topic = cmd_topic(prefix, gw.id)
    ctrl_mode_topic = control_mode_topic(prefix)
    ctrl_online_topic = control_online_topic(prefix)
    streams: dict[str, tuple[asyncio.Task, asyncio.Event]] = {}
    try:
        async for message in client.messages:
            topic = str(message.topic)
            payload = bytes(message.payload)
            now = time.time()

            if topic == ctrl_mode_topic:
                world.on_control_mode(payload)
                continue
            if topic == ctrl_online_topic:
                world.on_control_online(payload, now)
                continue
            if topic != my_cmd_topic:
                continue

            try:
                data = json.loads(payload)
            except (ValueError, TypeError):
                continue
            action, dev = data.get("action"), data.get("dev")
            if action not in ("connect", "release") or not dev:
                continue
            if not world.handle_cmd(gw.id, action, dev):
                continue

            if action == "connect":
                await client.publish(
                    build_status_topic(prefix, gw.id, dev),
                    status_payload("connected", gw.coverage.get(dev)),
                )
                stop = asyncio.Event()
                task = asyncio.create_task(
                    _stream_hr(client, prefix, gw.id, dev, capture_frames.get(dev, []), stop)
                )
                streams[dev] = (task, stop)
            else:
                entry = streams.pop(dev, None)
                if entry is not None:
                    task, stop = entry
                    stop.set()
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                await client.publish(
                    build_status_topic(prefix, gw.id, dev), status_payload("disconnected")
                )

            # Publish immediately on state change too, not just on the next
            # periodic tick, so the app sees connect/release without lag.
            await client.publish(
                report_topic(prefix, gw.id),
                json.dumps(world.report(gw.id, time.time())).encode(),
                qos=0,
                retain=True,
            )
    finally:
        for task, stop in streams.values():
            stop.set()
            task.cancel()


async def run_one_gateway(
    world: ManagedGatewayWorld,
    gw: MockGateway,
    capture_frames: dict[str, list[tuple[float, bytes]]],
    broker: str,
    port: int,
    prefix: str,
    report_interval: float,
) -> None:
    import aiomqtt

    will = aiomqtt.Will(online_topic(prefix, gw.id), b"0", qos=1, retain=True)
    async with aiomqtt.Client(broker, port, will=will) as client:
        await client.publish(online_topic(prefix, gw.id), b"1", qos=1, retain=True)
        await client.subscribe(cmd_topic(prefix, gw.id))
        await client.subscribe(control_mode_topic(prefix))
        await client.subscribe(control_online_topic(prefix))
        # Report at least once immediately so the app sees this gateway
        # without waiting a full `report_interval`.
        await client.publish(
            report_topic(prefix, gw.id),
            json.dumps(world.report(gw.id, time.time())).encode(),
            qos=0,
            retain=True,
        )

        reports = asyncio.create_task(_report_loop(client, world, gw.id, prefix, report_interval))
        try:
            await _cmd_loop(client, world, gw, prefix, capture_frames)
        finally:
            reports.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reports
            await client.publish(online_topic(prefix, gw.id), b"0", qos=1, retain=True)


async def run_managed(
    config_path: str,
    capture: str | None,
    broker: str,
    port: int,
    prefix: str,
    report_interval: float = DEFAULT_REPORT_INTERVAL_S,
) -> None:
    """Run every gateway in `config_path` concurrently, in one process,
    sharing one `ManagedGatewayWorld` (see module docstring for why the
    connected/seen state must be centralized). `capture` is optional: HR
    streaming is a bonus-realism feature, not required to exercise the
    orchestration contract itself (report/cmd/mode/online), so a dev with no
    matching row in the capture simply emits no HR while "connected"."""
    gateways, grace = load_gateways(config_path)
    world = ManagedGatewayWorld(gateways, grace=grace)
    capture_frames = device_frames_from_capture(capture) if capture else {}
    await asyncio.gather(
        *(
            run_one_gateway(world, gw, capture_frames, broker, port, prefix, report_interval)
            for gw in gateways
        )
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Mock BLE->WiFi gateway (MQTT publisher)")
    ap.add_argument("--mode", choices=["replay", "bleak", "managed"], default="replay")
    ap.add_argument("--capture", help="capture JSONL for --mode replay/managed")
    ap.add_argument("--config", help="coverage-set JSON config for --mode managed")
    ap.add_argument(
        "--report-interval",
        type=float,
        default=DEFAULT_REPORT_INTERVAL_S,
        help="seconds between periodic `report` publishes in --mode managed",
    )
    ap.add_argument("--broker", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--prefix", default="cuddle")
    ap.add_argument("--gateway", default="mock", help="gateway id for --mode replay/bleak")
    args = ap.parse_args()
    if args.mode == "replay":
        if not args.capture:
            raise SystemExit("--capture is required for --mode replay")
        asyncio.run(run_replay(args.capture, args.broker, args.port, args.prefix, args.gateway))
    elif args.mode == "managed":
        if not args.config:
            raise SystemExit("--config is required for --mode managed")
        asyncio.run(
            run_managed(
                args.config, args.capture, args.broker, args.port, args.prefix, args.report_interval
            )
        )
    else:
        asyncio.run(run_bleak(args.broker, args.port, args.prefix, args.gateway))


if __name__ == "__main__":
    main()
