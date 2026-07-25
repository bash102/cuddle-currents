"""Server-authoritative active visualization preset.

Holds one opaque config dict (the frontend's ``getState()`` blob — the backend never
interprets it), persists it to a runtime JSON file, and fans it out to ``/ws/viz``
subscribers so every open Show updates live when an operator edits ``/viz-settings``.
The sync core (``get``/``set``/``load``) is side-effect-contained (persist only); the
route calls ``set`` then ``await broadcast``, keeping this unit testable with no loop.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class VizConfigStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._config: dict | None = None
        self._clients: set = set()

    # ---- state ----------------------------------------------------------
    def load(self) -> None:
        if not self._path.exists():
            return
        try:
            self._config = json.loads(self._path.read_text())
        except (ValueError, OSError):
            logger.warning("viz config unreadable at %s; ignoring", self._path)

    def get(self) -> dict | None:
        return self._config

    def set(self, config: dict) -> None:
        self._config = config
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(config))

    # ---- fan-out --------------------------------------------------------
    def add_client(self, ws) -> None:
        self._clients.add(ws)

    def remove_client(self, ws) -> None:
        self._clients.discard(ws)

    async def broadcast(self) -> None:
        payload = json.dumps(self._config)
        dead = []
        for ws in list(self._clients):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)
