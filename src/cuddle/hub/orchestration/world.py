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
    # dev -> time (s) it was first seen advertising in its current advertising
    # spell. Stamped on the transition into `advertising()`, cleared the moment
    # it connects or stops advertising. `plan()` holds off placing a band until
    # `now - first_seen_adv >= settle_window`, giving every in-range gateway a
    # chance to report its RSSI before the strongest is chosen.
    first_seen_adv: dict[str, float] = field(default_factory=dict)

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

        self._refresh_adv_clock(now)

    def _refresh_adv_clock(self, now: float) -> None:
        """Keep `first_seen_adv` in step with the current advertising set:
        stamp a band the first tick it appears advertising, and drop the stamp
        once it stops advertising (connected, walked out of range, or its last
        gateway went offline). Called after any change to gateway views."""
        adv = self.advertising()
        for dev in adv:
            self.first_seen_adv.setdefault(dev, now)
        for dev in list(self.first_seen_adv):
            if dev not in adv:
                del self.first_seen_adv[dev]

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
        self._refresh_adv_clock(now)

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
        connected.

        Note: this freeze does NOT currently make old coverage usable for a
        later rebalance -- `_find_rebalance_target` (plan.py) independently
        rejects any entry with `now - ts > coverage_ttl`, and a frozen
        entry's `ts` never advances while connected, so once it would have
        aged out here it's also too old to serve as a rebalance target. The
        freeze is presently functionally inert; it only decouples pruning
        from that age gate so a future change (e.g. reusing frozen entries
        for something other than `_find_rebalance_target`) has "not deleted"
        as a distinct state from "not usable."
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
