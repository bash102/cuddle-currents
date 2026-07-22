"""Pure stability-first orchestration planner.

`plan()` decides, given a `WorldModel` snapshot (Task 2) plus pinned/pending
state, which gateway should connect or release which band this tick. It is a
pure function: no I/O, no async, no wall-clock reads -- `now` is always
passed in by the caller, which keeps this module trivially unit testable.

Priority order (a dev handled by an earlier rule is never reconsidered):
1. Pinned devs get first claim on free slots (strongest RSSI, ties broken by
   fewest-connected, then gateway id for determinism).
2. Unpinned advertising devs fill whatever slots remain, same tie-break.
3. Only on the disruptive reconcile tick (`allow_rebalance=True`) does an
   unserved advertising dev get to bump an unpinned connected occupant off a
   *different* gateway that fresh coverage memory says can reach it -- one
   release per gateway per call, and a pinned dev is never a release target.

`plan()` returns `(cmds, unserved, evictions)`: `evictions` is a list of
`(dev, gw)` pairs, one per rebalance `release` Cmd emitted in step 3, so the
caller can bar that dev from immediately being placed back on that gw (see
the `evicted` parameter) -- without this, a released dev can be re-placed on
the very gateway it was just released from on the next tick, thrashing
forever instead of freeing the slot for the band that needed it.
"""

from __future__ import annotations

from dataclasses import dataclass

from cuddle.hub.orchestration.world import WorldModel

_WEAK_RSSI = -999


@dataclass(frozen=True)
class Cmd:
    gw: str
    action: str  # "connect" | "release"
    dev: str


@dataclass
class Pending:
    gw: str
    deadline: float  # now + pending_ttl at issue time


@dataclass
class PlanCfg:
    coverage_ttl: float = 60.0


def _rssi_key(rssi: int | None) -> int:
    """None (unknown signal) sorts as very weak."""
    return _WEAK_RSSI if rssi is None else rssi


def _best_gw(candidates: dict[str, int | None], connected_count: dict[str, int]) -> str:
    """Strongest RSSI wins; ties broken by fewest connected, then gw id."""
    return min(
        candidates,
        key=lambda gw: (-_rssi_key(candidates[gw]), connected_count[gw], gw),
    )


def _find_rebalance_target(
    world: WorldModel,
    dev: str,
    exclude_gw: str,
    eligible_gws: set[str],
    free_slots: dict[str, int],
    coverage_ttl: float,
    now: float,
) -> str | None:
    """Return a managed+online gw (other than `exclude_gw`) with a free slot
    and coverage memory of `dev` no older than `coverage_ttl`, or None."""
    for gw_id in sorted(world.coverage.get(dev, {})):
        if gw_id == exclude_gw or gw_id not in eligible_gws:
            continue
        if free_slots.get(gw_id, 0) <= 0:
            continue
        _rssi, ts = world.coverage[dev][gw_id]
        if now - ts > coverage_ttl:
            continue
        return gw_id
    return None


def plan(
    world: WorldModel,
    pinned: set[str],
    pending: dict[str, Pending],
    cfg: PlanCfg,
    now: float,
    *,
    allow_rebalance: bool,
    evicted: dict[str, set[str]] | None = None,
) -> tuple[list[Cmd], list[dict], list[tuple[str, str]]]:
    evicted = evicted or {}
    connected = world.connected_devs()
    adv = world.advertising()

    active_pending = {
        dev: p for dev, p in pending.items() if p.deadline > now and dev not in connected
    }
    pending_per_gw: dict[str, int] = {}
    for p in active_pending.values():
        pending_per_gw[p.gw] = pending_per_gw.get(p.gw, 0) + 1

    eligible_gws = {
        gw_id for gw_id, view in world.gateways.items() if view.mode == "managed" and view.online
    }

    free_slots: dict[str, int] = {}
    connected_count: dict[str, int] = {}
    for gw_id in eligible_gws:
        view = world.gateways[gw_id]
        connected_count[gw_id] = len(view.connected)
        free_slots[gw_id] = view.capacity - len(view.connected) - pending_per_gw.get(gw_id, 0)

    cmds: list[Cmd] = []
    unserved: list[dict] = []
    evictions: list[tuple[str, str]] = []
    deferred_pinned: list[str] = []
    deferred_unpinned: list[str] = []

    def try_place(dev: str) -> bool:
        """Connect `dev` to the strongest eligible gw with a free slot, if
        any -- excluding any gw `dev` is currently evicted from. Mutates
        `free_slots` and appends a Cmd on success."""
        barred = evicted.get(dev, set())
        candidates = {
            gw_id: rssi
            for gw_id, rssi in adv.get(dev, {}).items()
            if gw_id in eligible_gws and free_slots.get(gw_id, 0) > 0 and gw_id not in barred
        }
        if not candidates:
            return False
        gw_id = _best_gw(candidates, connected_count)
        cmds.append(Cmd(gw=gw_id, action="connect", dev=dev))
        free_slots[gw_id] -= 1
        return True

    # Step 1: resolve duplicate connections. A multi-connect band (e.g. the Scosche
    # Rhythm+, which keeps advertising while connected) gets grabbed by every in-range
    # gateway in opportunistic mode and ends up connected on more than one -- wasting a
    # slot and double-counting its HR at the source. Keep the strongest-RSSI gateway and
    # release it from the others (only from gateways we actually command: managed + online).
    # The kept link keeps streaming, so there is no data gap.
    holders: dict[str, dict[str, int | None]] = {}
    for gw_id, view in world.gateways.items():
        for dev, rssi in view.connected.items():
            holders.setdefault(dev, {})[gw_id] = rssi
    for dev in sorted(holders):
        gw_rssi = holders[dev]
        if len(gw_rssi) < 2:
            continue
        keep = min(gw_rssi, key=lambda g: (-_rssi_key(gw_rssi[g]), g))
        for gw_id in sorted(gw_rssi):
            if gw_id != keep and gw_id in eligible_gws:
                cmds.append(Cmd(gw=gw_id, action="release", dev=dev))

    # Step 2: pinned placement.
    for dev in sorted(pinned):
        if dev in connected or dev in active_pending:
            continue
        if dev not in adv:
            unserved.append({"dev": dev, "rssi": None, "reason": "waiting_to_advertise"})
            continue
        if not try_place(dev):
            deferred_pinned.append(dev)

    # Step 3: connect-time placement for unpinned devs, using what's left.
    for dev in sorted(adv.keys()):
        if dev in pinned or dev in active_pending:
            continue
        if not try_place(dev):
            deferred_unpinned.append(dev)

    # Step 4: unserved-band resolution -- pinned targets first, then unpinned.
    released_from: set[str] = set()  # one release per gateway per call
    for dev in deferred_pinned + sorted(deferred_unpinned):
        candidates = adv.get(dev, {})
        report_rssi = max(candidates.values(), key=_rssi_key) if candidates else None

        released = False
        if allow_rebalance:
            full_gws = sorted(
                gw_id
                for gw_id in candidates
                if gw_id in eligible_gws and gw_id not in released_from
            )
            for full_gw in full_gws:
                movable = sorted(
                    y for y in world.gateways[full_gw].connected if y not in pinned
                )
                for y in movable:
                    target = _find_rebalance_target(
                        world, y, full_gw, eligible_gws, free_slots, cfg.coverage_ttl, now
                    )
                    if target is not None:
                        cmds.append(Cmd(gw=full_gw, action="release", dev=y))
                        evictions.append((y, full_gw))
                        released_from.add(full_gw)
                        # Consume the target's free slot so a later dev in this
                        # same call can't cite it again to justify another
                        # release -- one reabsorb per real slot, not per slot
                        # *sighting*.
                        free_slots[target] -= 1
                        released = True
                        break
                if released:
                    break

        if not released:
            unserved.append({"dev": dev, "rssi": report_rssi, "reason": "no_capacity"})

    return cmds, unserved, evictions
