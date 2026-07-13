from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

import zerg.database as database_module
from zerg.catalogd.client import CatalogClient
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.server import CatalogDaemon
from zerg.models.user import User
from zerg.tools.builtin import runner_tools


@pytest.fixture
def daemon_paths():
    root = Path("/tmp") / f"lhcd-runner-{uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    yield root / "live.db", root / "catalogd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


@pytest.mark.asyncio
async def test_catalogd_owns_runner_registration_and_job_history(daemon_paths):
    database_path, socket_path = daemon_paths
    engine = create_catalog_engine(database_path)
    initialize_catalog_schema(engine)
    with engine.begin() as connection:
        connection.execute(User.__table__.insert().values(id=7, email="runner-owner@example.com", role="USER"))
    engine.dispose()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        enrolled = await client.call(
            "runner.operation.v2",
            {"operation": "create_enroll_token", "params": {"owner_id": 7, "ttl_minutes": 10}},
        )
        registered = await client.call(
            "runner.operation.v2",
            {
                "operation": "register",
                "params": {
                    "enroll_token": enrolled["plaintext_token"],
                    "name": "cinder",
                    "availability_policy": "always_on",
                    "labels": {"role": "dev"},
                    "capabilities": ["exec.full"],
                    "metadata": {"hostname": "cinder"},
                },
            },
        )
        assert registered["status"] == "created"
        runner_id = registered["runner"]["id"]

        created = await client.call(
            "runner.operation.v2",
            {
                "operation": "job_create",
                "params": {
                    "owner_id": 7,
                    "runner_id": runner_id,
                    "command": "pwd",
                    "timeout_secs": 30,
                    "correlation_id": "corr-1",
                    "run_id": "run-1",
                },
            },
        )
        job_id = created["job"]["id"]
        await client.call(
            "runner.operation.v2",
            {"operation": "job_output", "params": {"job_id": job_id, "stream": "stdout", "data": "/work\n"}},
        )
        completed = await client.call(
            "runner.operation.v2",
            {"operation": "job_completed", "params": {"job_id": job_id, "exit_code": 0, "duration_ms": 12}},
        )

        assert completed["job"]["status"] == "success"
        assert completed["job"]["stdout_trunc"] == "/work\n"
        listed = await client.call(
            "runner.operation.v2",
            {"operation": "list", "params": {"owner_id": 7, "skip": 0, "limit": 100}},
        )
        assert [(row["id"], row["name"]) for row in listed["runners"]] == [(runner_id, "cinder")]
    finally:
        await client.close()
        await daemon.close()


def test_runner_tool_catalog_mode_never_constructs_runtime_engine(monkeypatch):
    monkeypatch.setattr(runner_tools, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(
        runner_tools,
        "get_catalog_session_factory",
        lambda: pytest.fail("Runtime Host must not construct a live-catalog SQLAlchemy engine"),
    )
    monkeypatch.setattr(
        "zerg.services.runner_catalog.operation",
        lambda operation_name, **_params: {
            "runner": {
                "id": 4,
                "owner_id": 7,
                "name": "cinder",
                "availability_policy": "always_on",
                "labels": None,
                "capabilities": ["exec.full"],
                "status": "offline",
                "last_seen_at": None,
                "auth_secret_hash": "hash",
                "runner_metadata": None,
                "created_at": "2026-07-12T00:00:00",
                "updated_at": "2026-07-12T00:00:00",
            }
        }
        if operation_name == "get_by_name"
        else {},
    )
    monkeypatch.setattr(runner_tools, "get_credential_resolver", lambda: SimpleNamespace(owner_id=7))

    assert runner_tools._resolve_target(7, "cinder") == (4, "cinder")
