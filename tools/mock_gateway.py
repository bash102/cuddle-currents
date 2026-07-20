"""Mock BLE->WiFi gateway: publishes the ESP32 gateway MQTT contract with no ESP32.

Modes:
  replay  — re-encode a recorded capture's RR into 0x2A37 frames and publish them
            on the capture's own timing (fully hardware-free).
  bleak   — a real BLE central that republishes raw notification bytes (Task 6).

Contract: cuddle/<gw>/hr/<dev> (raw 0x2A37 bytes), cuddle/<gw>/status/<dev> (JSON
{event, rssi}), cuddle/<gw>/online (retained "1"/"0", "0" as LWT).
"""

from __future__ import annotations

import argparse
import asyncio
import json

from cuddle.sources.ble_parser import encode_hr_measurement


def build_hr_topic(prefix: str, gw: str, dev: str) -> str:
    return f"{prefix}/{gw}/hr/{dev}"


def build_status_topic(prefix: str, gw: str, dev: str) -> str:
    return f"{prefix}/{gw}/status/{dev}"


def online_topic(prefix: str, gw: str) -> str:
    return f"{prefix}/{gw}/online"


def status_payload(event: str, rssi: int | None = None) -> bytes:
    return json.dumps({"event": event, "rssi": rssi}).encode()


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


def main() -> None:
    ap = argparse.ArgumentParser(description="Mock BLE->WiFi gateway (MQTT publisher)")
    ap.add_argument("--mode", choices=["replay", "bleak"], default="replay")
    ap.add_argument("--capture", help="capture JSONL for --mode replay")
    ap.add_argument("--broker", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--prefix", default="cuddle")
    ap.add_argument("--gateway", default="mock")
    args = ap.parse_args()
    if args.mode == "replay":
        if not args.capture:
            raise SystemExit("--capture is required for --mode replay")
        asyncio.run(run_replay(args.capture, args.broker, args.port, args.prefix, args.gateway))
    else:
        asyncio.run(run_bleak(args.broker, args.port, args.prefix, args.gateway))


if __name__ == "__main__":
    main()
