import asyncio
import json

import pytest

from cuddle.transport.viz_config import VizConfigStore


def test_set_get_roundtrip(tmp_path):
    s = VizConfigStore(tmp_path / "viz.json")
    assert s.get() is None
    s.set({"id": "node-graph", "params": {"gravityK": 2}})
    assert s.get() == {"id": "node-graph", "params": {"gravityK": 2}}


def test_set_persists_and_load_restores(tmp_path):
    p = tmp_path / "viz.json"
    VizConfigStore(p).set({"id": "chord"})
    assert json.loads(p.read_text()) == {"id": "chord"}
    fresh = VizConfigStore(p)
    assert fresh.get() is None  # not loaded yet
    fresh.load()
    assert fresh.get() == {"id": "chord"}


def test_load_missing_file_is_noop(tmp_path):
    s = VizConfigStore(tmp_path / "absent.json")
    s.load()
    assert s.get() is None


class _FakeWS:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send_text(self, txt):
        if self.fail:
            raise RuntimeError("dead socket")
        self.sent.append(txt)


def test_broadcast_sends_current_config_and_prunes_dead(tmp_path):
    s = VizConfigStore(tmp_path / "viz.json")
    s.set({"id": "node-graph"})
    good, dead = _FakeWS(), _FakeWS(fail=True)
    s.add_client(good)
    s.add_client(dead)
    asyncio.run(s.broadcast())
    assert json.loads(good.sent[-1]) == {"id": "node-graph"}
    assert dead not in s._clients and good in s._clients
