"""A served Runtime Host with one managed session and a fake machine, for control-route proofs.

The steer and interrupt proofs judge the routes a phone or browser calls: the
input route with ``intent=steer`` and ``interrupt-live``. They need a host that
answers them for real, so this starts the Runtime Host as a child, brings one
managed session into its catalog the way a launch does, attaches the control
lease an engine leaves, marks a turn as running through the same runtime events
a provider reports, and puts a fake machine on the control WebSocket to receive
the command frames. Nothing from the product is imported.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from zerg.provider_contract.catalogd_process import CatalogdHandle
from zerg.provider_contract.managed_provider_contracts import contract_for_provider
from zerg.provider_contract.managed_provider_contracts import machine_control_capability_for_command
from zerg.qa.fake_machine import FakeMachine
from zerg.qa.harness_catalog import HarnessCatalog
from zerg.qa.repo_root import default_repo_root
from zerg.qa.runtime_host import RuntimeHost

DEVICE_ID = "universal-harness"
# Auth is off, so the host serves as its one active owner; the catalog starts without one.
OWNER_ID = 1
# A small valid PNG: the multipart input route checks the file signature.
PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082"
)


@dataclass
class ServedControlSession:
    host: RuntimeHost
    catalog: HarnessCatalog
    machine: FakeMachine
    session_id: str
    run_id: str
    provider: str

    @property
    def commands(self) -> list[dict[str, Any]]:
        return self.machine.commands

    def activity_state(self) -> str | None:
        """The served activity state the input route gates a steer on."""

        result = self.catalog.rpc("session.input.activity.read.v2", {"session_id": self.session_id, "owner_id": OWNER_ID})
        return result.get("activity_state")

    def lease_rows(self) -> list[tuple[Any, ...]]:
        return self.catalog._catalog.query(  # noqa: SLF001 - the verifier's own catalogd handle
            "SELECT run_id, adapter_connection_id, lease_generation, control_plane FROM live_session_connections"
        )

    def post_input(self, *, text: str, intent: str, client_request_id: str) -> httpx.Response:
        return httpx.post(
            f"{self.host.api_url}/api/sessions/{self.session_id}/input",
            json={"text": text, "intent": intent, "client_request_id": client_request_id},
            timeout=60,
        )

    def post_input_with_image(self, *, text: str, intent: str, client_request_id: str, data: bytes = PNG_1X1) -> httpx.Response:
        return httpx.post(
            f"{self.host.api_url}/api/sessions/{self.session_id}/inputs-multipart",
            data={"text": text, "intent": intent, "client_request_id": client_request_id},
            files=[("attachments", ("proof.png", data, "image/png"))],
            timeout=60,
        )

    def post_interrupt(self) -> httpx.Response:
        return httpx.post(f"{self.host.api_url}/api/sessions/{self.session_id}/interrupt-live", timeout=60)


def grant_matches_lease(frame: Mapping[str, Any], lease_rows: list[tuple[Any, ...]]) -> bool:
    """Does the command carry the grant minted from the one bound lease?"""

    grant = (frame.get("payload") or {}).get("longhouse_control_grant") or {}
    return len(lease_rows) == 1 and (grant.get("run_id"), grant.get("connection_id"), grant.get("lease_generation")) == tuple(
        lease_rows[0][:3]
    )


def frame_without_grant(frame: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in (frame.get("payload") or {}).items() if key != "longhouse_control_grant"}


@contextmanager
def served_control_session(
    *,
    provider: str,
    workspace: Path,
    command_types: tuple[str, ...],
    responder: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    running_turn: bool = True,
) -> Iterator[ServedControlSession]:
    """Run the host, seed one attached managed session, and connect the fake machine.

    Everything started here is stopped, and the scratch root removed, on exit.
    """

    contract = contract_for_provider(provider)
    if contract is None or contract.managed_transport is None:
        raise ValueError(f"{provider} has no managed transport to serve")
    # A Unix socket path is limited to 104 bytes, so the host lives under /tmp.
    root = Path(tempfile.mkdtemp(prefix="lh-control-", dir="/tmp"))
    machine: FakeMachine | None = None
    host = RuntimeHost(
        repo_root=default_repo_root(), root=root, instance_id="universal-managed-control", extra_env={"ENVIRONMENT": "test:e2e"}
    )
    try:
        host.start()
        socket_path = host.catalogd_socket()
        handle = CatalogdHandle(database_path=host.database_path, socket_path=socket_path)
        handle.execute("INSERT INTO users (id, email, is_active) VALUES (?, ?, 1)", (OWNER_ID, "managed-control@longhouse.invalid"))
        catalog = HarnessCatalog(database_path=host.database_path, socket_path=socket_path, _catalog=handle)
        session_id = str(uuid4())
        now = datetime.now(UTC).replace(microsecond=0)
        run_id = catalog.create_managed_session(
            session_id=session_id,
            provider=provider,
            managed_transport=contract.managed_transport.value,
            device_id=DEVICE_ID,
            cwd=str(workspace),
            owner_id=OWNER_ID,
            now=now - timedelta(seconds=30),
        )
        catalog.attach_control_lease(session_id=session_id, provider=provider, now=now)
        if running_turn:
            # The phase a provider reports while a turn runs; the host projects it to `executing`.
            catalog.rpc(
                "session.runtime.apply.v2",
                {
                    "events": [
                        {
                            "runtime_key": f"{provider}:{session_id}:managed-control",
                            "session_id": session_id,
                            "run_id": run_id,
                            "provider": provider,
                            "device_id": DEVICE_ID,
                            "source": "universal_harness",
                            "kind": "phase_signal",
                            "phase": "running",
                            "tool_name": "Shell",
                            "occurred_at": now.isoformat(),
                            "freshness_ms": 10 * 60 * 1000,
                            "dedupe_key": "managed-control:running",
                            "payload": {},
                        }
                    ]
                },
            )
        capabilities = [cap for command in command_types if (cap := machine_control_capability_for_command(provider, command))]
        machine = FakeMachine(api_url=host.api_url, device_id=DEVICE_ID, supports=capabilities, responder=responder)
        machine.start()
        yield ServedControlSession(host=host, catalog=catalog, machine=machine, session_id=session_id, run_id=run_id, provider=provider)
    finally:
        if machine is not None:
            machine.stop()
        host.stop()
        shutil.rmtree(root, ignore_errors=True)
