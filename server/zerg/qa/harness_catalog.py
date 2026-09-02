"""A real catalogd for harness scenarios that drive served routes in-process.

Held interactions -- the permission gate, the pause-answer route -- live in the
live catalog, so a scenario whose claim is "the real endpoint ran" needs a
daemon behind that endpoint rather than a stand-in. Everything below is the
production object: a real ``CatalogDaemon`` over a real Unix socket, reached
through the real ``CatalogClient`` by the same ``get_catalogd_client()`` the
routes call.

The daemon runs on its own event loop thread. Harness scenarios call
``asyncio.run`` from several threads -- the stub HTTP handler, the background
answerer -- and each of those builds a fresh loop; a daemon sharing one of them
would have to answer a call that is blocking the loop it runs on. One client
per call keeps the same honesty for ``CatalogClient``, which binds its
admission semaphore to the first loop that uses it.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any
from typing import Iterator
from uuid import uuid4

from sqlalchemy.orm import Session

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import CatalogDaemon
from zerg.models.live_store import LiveUser
from zerg.services import catalogd_supervisor

# Factory correctness is not the catalog latency gate; a loaded CI host needs
# room to finish a real transaction. Production clients keep the 1 s default.
RPC_TIMEOUT_SECONDS = 5.0
HARNESS_OWNER_ID = 7
HARNESS_OWNER_EMAIL = "harness-catalog@longhouse.invalid"


class _DaemonLoop:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, name="harness-catalogd", daemon=True)
        self._thread.start()

    def run(self, coro, *, timeout: float = 60.0):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=timeout)

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=10.0)
        self.loop.close()


class _PerCallSupervisor:
    """Supervisor stand-in handing out a fresh client per call."""

    def __init__(self, socket_path: Path) -> None:
        self._socket_path = socket_path

    @property
    def client(self) -> CatalogClient:
        return CatalogClient(self._socket_path, default_timeout_seconds=RPC_TIMEOUT_SECONDS)

    @property
    def projector_client(self) -> CatalogClient:
        return self.client


@dataclass
class HarnessCatalog:
    """A running catalogd, plus the launch RPCs a scenario needs to seed one."""

    database_path: Path
    socket_path: Path
    _loop: _DaemonLoop

    def rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        client = CatalogClient(self.socket_path, default_timeout_seconds=RPC_TIMEOUT_SECONDS)
        return self._loop.run(client.call(method, params))

    def create_managed_session(
        self,
        *,
        session_id: str,
        provider: str,
        managed_transport: str,
        device_id: str,
        cwd: str,
        project: str = "universal-agent-harness",
        now: datetime | None = None,
    ) -> str:
        """Bring one managed session into the catalog the way a launch does.

        ``interaction.register.v2`` refuses a session the catalog has never
        seen, so the scenario registers through the same launch transaction a
        Runtime Host runs rather than inserting a catalog row behind it.
        """

        now = now or datetime.now(UTC).replace(microsecond=0)
        created = self.rpc(
            "session.launch.local.create.v2",
            {
                "launch": {
                    "owner_id": HARNESS_OWNER_ID,
                    "git_repo": "cipher982/longhouse",
                    "git_branch": "main",
                    "started_at": now.isoformat(),
                    "expires_at": (now + timedelta(minutes=5)).isoformat(),
                    "plan": {
                        "session_id": session_id,
                        "provider": provider,
                        "provider_session_id": str(uuid4()),
                        "source_name": device_id,
                        "source_runner_id": None,
                        "cwd": cwd,
                        "project": project,
                        "display_name": "Universal agent harness",
                        "managed_session_name": f"{provider}-universal-harness",
                        "permission_mode": "bypass",
                        "launch_actor": "automation",
                        "launch_surface": "test",
                        "environment": "test",
                        "origin_kind": "test_or_canary",
                        "hidden_from_default_timeline": 1,
                        "managed_transport": managed_transport,
                        "attach_command": "",
                        "provider_config": {},
                    },
                }
            },
        )
        run_id = str(created["run_id"])
        self.rpc(
            "session.launch.local.finish.v2",
            {
                "outcome": {
                    "session_id": session_id,
                    "run_id": run_id,
                    "owner_id": HARNESS_OWNER_ID,
                    "device_id": device_id,
                    "state": "adopted",
                    "error_code": None,
                    "error_message": None,
                    "observed_at": (now + timedelta(seconds=2)).isoformat(),
                }
            },
        )
        return run_id

    def pending_interaction(self, *, session_id: str) -> dict[str, Any] | None:
        """Return the one pending interaction for a session, if any."""

        result = self.rpc(
            "interaction.list.v2",
            {"session_id": session_id, "status": "pending", "limit": 20},
        )
        interactions = [item for item in result.get("interactions") or [] if isinstance(item, dict)]
        return interactions[0] if interactions else None


@contextmanager
def provision_harness_catalog() -> Iterator[HarnessCatalog]:
    """Run a catalogd for the duration of the block and route the routes at it.

    ``/tmp`` stays short after macOS resolves it, which keeps the Unix socket
    inside the 104-byte limit that evidence-package paths would otherwise blow
    through.
    """

    root = Path(tempfile.mkdtemp(prefix="lh-harness-catalog-", dir="/tmp"))
    database_path = root / "live.db"
    socket_path = root / "catalogd.sock"
    loop = _DaemonLoop()
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    loop.run(daemon.start())

    setup_engine = create_catalog_engine(database_path)
    try:
        initialize_catalog_schema(setup_engine)
        with Session(setup_engine) as db:
            db.add(LiveUser(id=HARNESS_OWNER_ID, email=HARNESS_OWNER_EMAIL, is_active=True))
            db.commit()
    finally:
        setup_engine.dispose()

    previous_supervisor = catalogd_supervisor._supervisor
    catalogd_supervisor._supervisor = _PerCallSupervisor(socket_path)
    try:
        yield HarnessCatalog(database_path=database_path, socket_path=socket_path, _loop=loop)
    finally:
        catalogd_supervisor._supervisor = previous_supervisor
        loop.run(daemon.close())
        loop.close()
        shutil.rmtree(root, ignore_errors=True)
