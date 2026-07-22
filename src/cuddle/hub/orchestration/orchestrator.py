"""Async MQTT-driven orchestrator: wraps the pure `WorldModel`/`plan()` core
(Tasks 2+3) in a live client that ingests gateway reports and issues
connect/release commands.

`GatewayMqttSource` is unchanged and unaware of this module; the two own
separate MQTT clients and never share mutable state -- this module reasons
about *which* gateway should hold *which* band, not about decoding samples.

Mirrors `GatewayMqttSource`'s client-loop shape (`_run`, lazy `import
aiomqtt`, retry-on-exception) but the seams that matter for testing --
`_handle_report`, `_handle_online`, `_run_plan`, `force_connect`,
`force_release`, `_pinned` -- stay synchronous and side-effect-contained, so
the whole planning/pending lifecycle is unit-testable with no broker and no
event loop. Publishing is factored behind `_publish` so tests can substitute
a plain recorder in place of the real (async, queued) MQTT send path.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from cuddle.core import clock
from cuddle.core.models import (
    ConnectedBand,
    EnrollmentState,
    GatewayState,
    SeenBand,
    UnservedBand,
)
from cuddle.hub.orchestration.plan import Cmd, PlanCfg, Pending, plan
from cuddle.hub.orchestration.world import WorldModel
from cuddle.hub.registry import SessionStore

logger = logging.getLogger(__name__)

_PINNED_STATES = (
    EnrollmentState.assigned,
    EnrollmentState.baselining,
    EnrollmentState.active,
)
_OFFLINE_PAYLOADS = (b"0", b"", b"false")


class Orchestrator:
    def __init__(
        self,
        store: SessionStore,
        *,
        broker: str = "127.0.0.1",
        port: int = 1883,
        topic_prefix: str = "cuddle",
        report_debounce: float = 0.5,
        reconcile_interval: float = 5.0,
        pending_ttl: float = 8.0,
        coverage_ttl: float = 60.0,
        rebalance_cooldown: float = 10.0,
        evict_cooldown: float = 10.0,
    ) -> None:
        self._store = store
        self._broker = broker
        self._port = port
        self._prefix = topic_prefix
        self._report_debounce = report_debounce
        self._reconcile_interval = reconcile_interval
        self._pending_ttl = pending_ttl
        self._coverage_ttl = coverage_ttl
        self._rebalance_cooldown = rebalance_cooldown
        self._evict_cooldown = evict_cooldown

        self._world = WorldModel()
        self._pending: dict[str, Pending] = {}
        self._evicted: dict[str, dict[str, float]] = {}
        self._manual_pins: set[str] = set()
        self._unserved: list[UnservedBand] = []
        self._last_rebalance_at: float | None = None

        self._dirty_event = asyncio.Event()
        self._out_queue: asyncio.Queue = asyncio.Queue()
        self._client = None
        self._running = False
        self._mqtt_task: asyncio.Task | None = None
        self._debounce_task: asyncio.Task | None = None
        self._reconcile_task: asyncio.Task | None = None

    # ---- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        self._running = True
        self._mqtt_task = asyncio.create_task(self._run(), name="orchestrator-mqtt")
        self._debounce_task = asyncio.create_task(
            self._debounce_loop(), name="orchestrator-debounce"
        )
        self._reconcile_task = asyncio.create_task(
            self._reconcile_loop(), name="orchestrator-reconcile"
        )

    async def stop(self) -> None:
        self._publish(f"{self._prefix}/control/mode", b"opportunistic", qos=1, retain=True)
        self._publish(f"{self._prefix}/control/online", b"0", qos=1, retain=True)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._out_queue.join(), timeout=2.0)

        self._running = False
        tasks = [t for t in (self._debounce_task, self._reconcile_task, self._mqtt_task) if t]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._mqtt_task = self._debounce_task = self._reconcile_task = None

    # ---- message handling (sync, testable) --------------------------------

    def _handle_message(self, topic: str, payload: bytes, now: float) -> None:
        parts = topic.split("/")
        if len(parts) != 3 or parts[0] != self._prefix:
            return
        _, gw, kind = parts
        if kind == "report":
            self._handle_report(gw, payload, now)
        elif kind == "online":
            self._handle_online(gw, payload, now)

    def _handle_report(self, gw: str, payload: bytes, now: float) -> None:
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            return
        if not isinstance(data, dict):
            return
        try:
            self._world.apply_report(gw, data, now)
        except (KeyError, ValueError, TypeError):
            # Well-formed JSON but missing/invalid fields (e.g. no "seen").
            # world.apply_report stays strict -- guard here at the handler
            # boundary so a malformed retained payload can't crash this
            # loop and drive the client into a reconnect/redeliver churn
            # loop over the same bad message.
            logger.debug("orchestrator: malformed report from %s; skipping", gw, exc_info=True)
            return
        self._dirty_event.set()

    def _handle_online(self, gw: str, payload: bytes, now: float) -> None:
        """A gateway's own per-gateway online/LWT topic (`<prefix>/<gw>/online`)
        going to "0" (or firing its LWT) means that gateway dropped off the
        broker -- clear its connected/seen state in the world so `plan()`
        stops treating its slots as real and reassigns pinned/unserved bands
        elsewhere. This is distinct from the singular `<prefix>/control/online`
        the orchestrator itself publishes to announce its own presence.
        """
        if payload in _OFFLINE_PAYLOADS:
            self._world.set_offline(gw, now)
            self._dirty_event.set()

    # ---- pinning ------------------------------------------------------------

    def _pinned(self) -> set[str]:
        return {
            s.profile.device_id
            for s in self._store.all()
            if s.profile.enrollment_state in _PINNED_STATES and s.profile.device_id
        }

    def set_pin(self, dev: str, pinned: bool) -> None:
        if pinned:
            self._manual_pins.add(dev)
        else:
            self._manual_pins.discard(dev)

    # ---- planning (sync, testable) ------------------------------------------

    def _run_plan(self, now: float, *, allow_rebalance: bool) -> list[Cmd]:
        pinned = self._pinned() | self._manual_pins
        cfg = PlanCfg(coverage_ttl=self._coverage_ttl)

        # Prune expired evictions and build the still-live evicted map to
        # pass into plan() -- a gw whose eviction deadline has passed is
        # usable again.
        evicted: dict[str, set[str]] = {}
        for dev in list(self._evicted):
            live = {gw: deadline for gw, deadline in self._evicted[dev].items() if deadline > now}
            if live:
                self._evicted[dev] = live
                evicted[dev] = set(live)
            else:
                del self._evicted[dev]

        cmds, unserved, evictions = plan(
            self._world,
            pinned,
            self._pending,
            cfg,
            now,
            allow_rebalance=allow_rebalance,
            evicted=evicted,
        )

        # Bar every dev just released by a rebalance from immediately
        # returning to the gw it was released from, for `_evict_cooldown`
        # seconds -- this is what stops the release/reconnect thrash.
        for dev, gw in evictions:
            self._evicted.setdefault(dev, {})[gw] = now + self._evict_cooldown

        for cmd in cmds:
            if cmd.action == "connect":
                self._pending[cmd.dev] = Pending(gw=cmd.gw, deadline=now + self._pending_ttl)

        connected = self._world.connected_devs()
        for dev in list(self._pending):
            p = self._pending[dev]
            if dev in connected or p.deadline <= now:
                del self._pending[dev]

        # Optional cleanup: a dev that's connected somewhere other than the
        # gw(s) it's evicted from has already relocated successfully -- no
        # need to keep barring it, so drop its eviction record early rather
        # than waiting out the deadline.
        for dev in list(self._evicted):
            holder = self._world.holder_of(dev)
            if holder is not None and holder not in self._evicted[dev]:
                del self._evicted[dev]

        self._unserved = [
            UnservedBand(dev=u["dev"], rssi=u["rssi"], reason=u["reason"]) for u in unserved
        ]
        return cmds

    def _publish_cmd(self, cmd: Cmd) -> None:
        topic = f"{self._prefix}/{cmd.gw}/cmd"
        payload = json.dumps({"action": cmd.action, "dev": cmd.dev}).encode()
        self._publish(topic, payload, qos=1, retain=False)

    # ---- operator overrides ---------------------------------------------------

    def force_connect(self, dev: str, gw: str) -> None:
        self._manual_pins.add(dev)
        now = clock.now()
        self._pending[dev] = Pending(gw=gw, deadline=now + self._pending_ttl)
        self._publish(
            f"{self._prefix}/{gw}/cmd",
            json.dumps({"action": "connect", "dev": dev}).encode(),
            qos=1,
            retain=False,
        )

    def force_release(self, dev: str) -> None:
        gw = self._world.holder_of(dev)
        if gw is None:
            return
        self._pending.pop(dev, None)
        # Clear the manual override pin from force_connect -- otherwise the
        # band, still pinned + still advertising after it disconnects, gets
        # immediately re-placed by _run_plan on the next tick, making manual
        # "Release" a no-op. Enrollment-derived pins (_pinned()) are separate
        # and untouched here -- an actively-enrolled band stays protected.
        self._manual_pins.discard(dev)
        self._publish(
            f"{self._prefix}/{gw}/cmd",
            json.dumps({"action": "release", "dev": dev}).encode(),
            qos=1,
            retain=False,
        )

    def set_mode(self, mode: str) -> None:
        self._publish(f"{self._prefix}/control/mode", mode.encode(), qos=1, retain=True)

    # ---- state for build_frame -----------------------------------------------

    def gateway_states(self) -> list[GatewayState]:
        states = []
        for gw_id, view in self._world.gateways.items():
            connected = [
                ConnectedBand(dev=dev, person_id=self._store.person_for_device(dev), rssi=rssi)
                for dev, rssi in view.connected.items()
            ]
            seen = [
                SeenBand(dev=dev, person_id=self._store.person_for_device(dev), rssi=rssi)
                for dev, rssi in view.seen.items()
            ]
            states.append(
                GatewayState(
                    id=gw_id,
                    online=view.online,
                    mode=view.mode,
                    capacity=view.capacity,
                    connected=connected,
                    seen=seen,
                )
            )
        return states

    def unserved(self) -> list[UnservedBand]:
        return list(self._unserved)

    # ---- publishing (real send path; tests substitute a recorder) -----------

    def _publish(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False) -> None:
        """Enqueue a publish for the background client to send. Kept
        synchronous (rather than a coroutine) so `force_*`/`set_*`/the debounce
        and reconcile loops can call it without an `await`, and so tests can
        swap it for a plain recorder with no event loop involved."""
        self._out_queue.put_nowait((topic, payload, qos, retain))

    # ---- background loops ----------------------------------------------------

    async def _debounce_loop(self) -> None:
        while self._running:
            await self._dirty_event.wait()
            self._dirty_event.clear()
            await asyncio.sleep(self._report_debounce)
            if not self._running:
                break
            cmds = self._run_plan(clock.now(), allow_rebalance=False)
            for cmd in cmds:
                self._publish_cmd(cmd)

    async def _reconcile_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._reconcile_interval)
            if not self._running:
                break
            now = clock.now()
            self._world.prune_coverage(now, self._coverage_ttl)
            allow_rebalance = (
                self._last_rebalance_at is None
                or now - self._last_rebalance_at >= self._rebalance_cooldown
            )
            if allow_rebalance:
                self._last_rebalance_at = now
            cmds = self._run_plan(now, allow_rebalance=allow_rebalance)
            for cmd in cmds:
                self._publish_cmd(cmd)

    # ---- MQTT client loop (mirrors GatewayMqttSource._run) -------------------

    async def _run(self) -> None:
        import aiomqtt

        online_topic = f"{self._prefix}/control/online"
        while self._running:
            try:
                will = aiomqtt.Will(online_topic, b"0", qos=1, retain=True)
                async with aiomqtt.Client(self._broker, self._port, will=will) as client:
                    self._client = client
                    await client.publish(f"{self._prefix}/control/mode", b"managed", qos=1, retain=True)
                    await client.publish(online_topic, b"1", qos=1, retain=True)
                    await client.subscribe(f"{self._prefix}/+/report")
                    await client.subscribe(f"{self._prefix}/+/online")

                    consumer = asyncio.create_task(self._consume(client))
                    publisher = asyncio.create_task(self._drain(client))
                    try:
                        await asyncio.gather(consumer, publisher)
                    finally:
                        consumer.cancel()
                        publisher.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await consumer
                        with contextlib.suppress(asyncio.CancelledError):
                            await publisher
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("orchestrator mqtt loop error; retrying", exc_info=True)
                await asyncio.sleep(1.0)  # broker unreachable; retry
            finally:
                self._client = None

    async def _consume(self, client) -> None:
        async for message in client.messages:
            if not self._running:
                break
            self._handle_message(str(message.topic), bytes(message.payload), clock.now())

    async def _drain(self, client) -> None:
        while self._running:
            topic, payload, qos, retain = await self._out_queue.get()
            try:
                await client.publish(topic, payload, qos=qos, retain=retain)
            except Exception:
                logger.debug("orchestrator publish failed; dropping message", exc_info=True)
            finally:
                self._out_queue.task_done()
