"""A fake Machine Agent on the Runtime Host control WebSocket.

The product sends managed-control commands to a connected machine and waits for
its ``command_result``. A scenario that judges the served answer route needs
something on the other end, so this connects like the engine does (hello, wait
for hello_ack), records every command frame it is sent, and replies with
whatever the scenario's responder returns. Nothing from the product is
imported: the frames below are the wire contract the verifier holds the host to.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

Responder = Callable[[Mapping[str, Any]], Mapping[str, Any]]


class FakeMachine:
    def __init__(
        self,
        *,
        api_url: str,
        device_id: str,
        supports: Sequence[str],
        responder: Responder,
    ) -> None:
        self.url = api_url.replace("http://", "ws://", 1).rstrip("/") + "/api/agents/control/ws"
        self.device_id = device_id
        self.supports = list(supports)
        self.responder = responder
        self.commands: list[dict[str, Any]] = []
        self._connection: Any = None
        self._thread: threading.Thread | None = None

    def start(self, *, timeout_seconds: float = 20.0) -> None:
        self._connection = connect(self.url, open_timeout=timeout_seconds)
        self._connection.send(
            json.dumps(
                {
                    "type": "hello",
                    "device_id": self.device_id,
                    "machine_name": self.device_id,
                    "engine_build": "fake-machine",
                    "supports": self.supports,
                }
            )
        )
        while True:
            frame = json.loads(self._connection.recv(timeout=timeout_seconds))
            if frame.get("type") == "hello_ack":
                break
        self._thread = threading.Thread(target=self._serve, name="fake-machine", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        connection = self._connection
        try:
            while True:
                frame = json.loads(connection.recv())
                if frame.get("type") != "command":
                    continue
                self.commands.append(frame)
                reply = dict(self.responder(frame))
                reply.setdefault("type", "command_result")
                reply.setdefault("command_id", frame.get("command_id"))
                connection.send(json.dumps(reply))
        except (ConnectionClosed, OSError):
            return

    def stop(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:  # noqa: BLE001 - teardown must not mask the verdict
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)
