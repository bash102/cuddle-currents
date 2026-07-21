"""Pure in-memory world model for gateway/band orchestration.

Fed from gateway `report` messages (JSON payloads shaped like
`{"capacity": int, "mode": "managed"|"opportunistic",
  "connected": [{"dev": str, "rssi": int|None}],
  "seen": [{"dev": str, "rssi": int|None}], "ts": int_ms}`).

Physical fact driving the design: a BLE band stops advertising once
connected, so a connected band never appears in any gateway's `seen`. Coverage
memory is therefore recorded only for `seen` (advertising) devices, and
`advertising()` excludes any dev that is connected on any gateway.

No I/O, no async, no wall-clock reads -- `now` and all timestamps are passed
in as floats (seconds) by the caller, which keeps this module trivially unit
testable.
"""

from dataclasses import dataclass, field


@dataclass
class GatewayView:
    id: str
    capacity: int
    mode: str  # "managed" | "opportunistic"
    online: bool
    connected: dict[str, int | None]  # dev -> rssi
    seen: dict[str, int | None]  # dev -> rssi (currently advertising)
    last_report_ts: float


@dataclass
class WorldModel:
    gateways: dict[str, GatewayView] = field(default_factory=dict)
    # dev -> gw -> (rssi, ts)
    coverage: dict[str, dict[str, tuple[int | None, float]]] = field(default_factory=dict)

    def apply_report(self, gw: str, payload: dict, now: float) -> None:
        """Replace `gw`'s GatewayView with the contents of `payload` and
        record coverage memory for every currently-advertising (`seen`) dev.

        Connected devs are NOT written to coverage: they aren't advertising,
        so any coverage entry for them is stale memory from before they
        connected -- left alone to age out via `prune_coverage`.
        """
        connected = {entry["dev"]: entry["rssi"] for entry in payload["connected"]}
        seen = {entry["dev"]: entry["rssi"] for entry in payload["seen"]}

        self.gateways[gw] = GatewayView(
            id=gw,
            capacity=payload["capacity"],
            mode=payload["mode"],
            online=True,
            connected=connected,
            seen=seen,
            last_report_ts=now,
        )

        for dev, rssi in seen.items():
            self.coverage.setdefault(dev, {})[gw] = (rssi, now)

    def set_offline(self, gw: str, now: float) -> None:
        """Mark `gw` offline and clear its connected/seen sets.

        A no-op if `gw` has never reported (there is no view to mark
        offline, and fabricating one would misrepresent capacity/mode we
        never observed).
        """
        view = self.gateways.get(gw)
        if view is None:
            return
        view.online = False
        view.connected = {}
        view.seen = {}

    def holder_of(self, dev: str) -> str | None:
        """Return the id of the gateway currently holding `dev` connected,
        or None if no gateway reports it connected."""
        for gw_id, view in self.gateways.items():
            if dev in view.connected:
                return gw_id
        return None

    def connected_devs(self) -> set[str]:
        """Union of devs connected across all gateways."""
        devs: set[str] = set()
        for view in self.gateways.values():
            devs.update(view.connected.keys())
        return devs

    def advertising(self) -> dict[str, dict[str, int | None]]:
        """dev -> {gw: rssi} for every unconnected dev currently seen
        advertising by at least one gateway. A dev connected on ANY gateway
        is excluded, even if it still lingers in another gateway's stale
        `seen` set."""
        connected = self.connected_devs()
        result: dict[str, dict[str, int | None]] = {}
        for view in self.gateways.values():
            for dev, rssi in view.seen.items():
                if dev in connected:
                    continue
                result.setdefault(dev, {})[view.id] = rssi
        return result

    def prune_coverage(self, now: float, ttl: float) -> None:
        """Drop coverage entries older than `ttl` seconds (relative to
        `now`); keeps entries at or within the ttl.

        Devs currently in `connected_devs()` are frozen and never pruned,
        however stale their entries: a connected band stops advertising, so
        it has no way to refresh its coverage memory via `seen` while
        connected. Dropping it would erase the only record of its alternate
        gateways, making it impossible to rebalance later.
        """
        connected = self.connected_devs()
        stale_devs: list[str] = []
        for dev, by_gw in self.coverage.items():
            if dev in connected:
                continue
            stale_gws = [gw for gw, (_, ts) in by_gw.items() if now - ts > ttl]
            for gw in stale_gws:
                del by_gw[gw]
            if not by_gw:
                stale_devs.append(dev)
        for dev in stale_devs:
            del self.coverage[dev]
