"""A real catalogd for harness scenarios that drive served routes in-process.

Held interactions -- the permission gate, the pause-answer route -- live in the
live catalog, so a scenario whose claim is "the real endpoint ran" needs a
daemon behind that endpoint rather than a stand-in. It is the production
daemon on a real Unix socket, reached by the same ``get_catalogd_client()`` the
routes call.

The daemon is a child process of the harness, reached over its Unix socket. The
routes find it through ``LONGHOUSE_CATALOGD_EXTERNAL_SOCKET``, which gives them a
fresh client per call (the harness calls from several threads, each with its
own event loop). Nothing from the product is imported here.
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any
from typing import Iterator
from unittest.mock import patch
from uuid import uuid4

from zerg.provider_contract.catalogd_process import DEFAULT_RPC_TIMEOUT_SECONDS
from zerg.provider_contract.catalogd_process import CatalogdHandle
from zerg.provider_contract.catalogd_process import run_catalogd
from zerg.qa.repo_root import default_repo_root

# Factory correctness is not the catalog latency gate; a loaded CI host needs
# room to finish a real transaction and its concurrent expiry writer. Production
# clients keep the 1 s default.
RPC_TIMEOUT_SECONDS = DEFAULT_RPC_TIMEOUT_SECONDS
HARNESS_OWNER_ID = 7
HARNESS_OWNER_EMAIL = "harness-catalog@longhouse.invalid"
# Names the catalogd the served routes should use (zerg.services.catalogd_supervisor).
_EXTERNAL_SOCKET_ENV = "LONGHOUSE_CATALOGD_EXTERNAL_SOCKET"


@dataclass
class HarnessCatalog:
    """A running catalogd, plus the launch RPCs a scenario needs to seed one."""

    database_path: Path
    socket_path: Path
    _catalog: CatalogdHandle

    def rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return self._catalog.rpc(method, params, timeout_seconds=RPC_TIMEOUT_SECONDS)

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
        owner_id: int = HARNESS_OWNER_ID,
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
                    "owner_id": owner_id,
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
                    "owner_id": owner_id,
                    "device_id": device_id,
                    "state": "adopted",
                    "error_code": None,
                    "error_message": None,
                    "observed_at": (now + timedelta(seconds=2)).isoformat(),
                }
            },
        )
        return run_id

    def attach_control_lease(
        self,
        *,
        session_id: str,
        provider: str,
        granted_operations: tuple[str, ...] = ("send_input", "interrupt", "terminate"),
        now: datetime | None = None,
    ) -> None:
        """Put the session's control lease in the state a connected engine leaves it in.

        Providers with a lease observer launch detached and attach once their
        engine reports health, and the engine's control fact is what authorizes
        a command. The verifier writes the same two things the engine's reports
        land as: an attached, health-stamped connection bound to an adapter
        identity, and a control fact head naming exactly that identity.
        """

        now = (now or datetime.now(UTC)).replace(microsecond=0)
        stamp = now.strftime("%Y-%m-%d %H:%M:%S.%f")
        self._catalog.execute(
            "UPDATE live_session_connections SET state = 'attached', can_send_input = 1, can_interrupt = 1, can_terminate = 1, "
            "last_health_at = ?, adapter_connection_id = COALESCE(adapter_connection_id, ?), "
            "lease_generation = COALESCE(lease_generation, ?)",
            (stamp, str(uuid4()), str(uuid4())),
        )
        rows = self._catalog.query(
            "SELECT run_id, adapter_connection_id, lease_generation FROM live_session_connections ORDER BY id DESC LIMIT 1"
        )
        run_id, connection_id, lease_generation = rows[0]
        source = f"{provider}_control_scan"
        value = {
            "authority_class": "provider_control",
            "provider": provider,
            "session_id": session_id,
            "run_id": run_id,
            "connection_id": connection_id,
            "lease_generation": lease_generation,
            "granted_operations": sorted(set(granted_operations)),
            "state": "attached",
            "lease_ttl_ms": 900_000,
            "source": source,
            "observed_at": now.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        }
        evidence_hash = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        self._catalog.execute(
            "INSERT INTO fact_heads (family, subject_key, source, source_epoch, session_id, ordering_mode, source_seq, evidence_hash, "
            "observed_at, valid_until, value_json, raw_locator, updated_commit_seq, received_at) "
            "VALUES ('control', ?, ?, ?, ?, 'observed_at', NULL, ?, ?, NULL, ?, NULL, 1, ?)",
            (
                f"connection:{connection_id}:{lease_generation}",
                source,
                lease_generation,
                session_id,
                evidence_hash,
                stamp,
                json.dumps(value),
                stamp,
            ),
        )

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

    with run_catalogd(server_root=default_repo_root() / "server") as catalog:
        catalog.execute(
            "INSERT INTO users (id, email, is_active) VALUES (?, ?, 1)",
            (HARNESS_OWNER_ID, HARNESS_OWNER_EMAIL),
        )
        with patch.dict(os.environ, {_EXTERNAL_SOCKET_ENV: str(catalog.socket_path)}):
            yield HarnessCatalog(database_path=catalog.database_path, socket_path=catalog.socket_path, _catalog=catalog)
