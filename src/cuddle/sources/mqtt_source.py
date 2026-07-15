"""Phase-2 placeholder: ingest from BLE->WiFi gateways over MQTT.

This is the whole point of the ``SampleSource`` abstraction: when the system grows
past the ~7-10 peripherals a Mac's CoreBluetooth can hold, bands route through BLE
gateways to the local network instead of connecting directly. Only this one class
changes — the hub, processing, and both frontends are untouched, because everything
downstream consumes ``NormalizedSample`` and never knows the origin.

Not implemented in Phase 1. Sketch of the intended shape:

    class GatewayMqttSource:
        # subscribe to e.g. cuddle/<gateway>/<device>/hr
        # decode the same 0x2A37 payload via sources.ble_parser
        # map gateway device ids -> person_id via the enrollment store
        # emit NormalizedSample(source="mqtt", ...) onto the queue
        # gateway-reported RSSI / last-seen drive ConnectionState

The class must implement the SampleSource Protocol (start/stop/subscribe/
connection_states/unassigned_devices/bind) exactly like DirectBleSource.
"""

from __future__ import annotations


class GatewayMqttSource:  # pragma: no cover - Phase 2 stub
    def __init__(self, *args, **kwargs) -> None:
        raise NotImplementedError(
            "GatewayMqttSource is a Phase 2 stub — Phase 1 uses direct BLE / sim."
        )
