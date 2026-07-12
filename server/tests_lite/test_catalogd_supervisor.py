from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from zerg.catalogd.client import CatalogClient
from zerg.services.catalogd_supervisor import CatalogdSupervisor


@pytest.fixture
def supervisor_paths():
    root = Path("/tmp") / f"lhcds-{uuid4().hex[:10]}"
    root.mkdir(mode=0o700)
    yield root / "live.db", root / "catalogd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


def _external_catalogd(database_path: Path, socket_path: Path, *, schema_generation: str | None = None):
    if schema_generation is None:
        command = [
            sys.executable,
            "-m",
            "zerg.catalogd",
            "--database",
            str(database_path),
            "--socket",
            str(socket_path),
        ]
    else:
        command = [
            sys.executable,
            "-c",
            (
                "import asyncio,sys; from pathlib import Path; "
                "from zerg.catalogd.server import CatalogDaemon; "
                "d=CatalogDaemon(database_path=Path(sys.argv[1]),socket_path=Path(sys.argv[2]),"
                "schema_generation=sys.argv[3]); "
                "exec('async def run():\\n await d.start()\\n await d.serve_forever()'); "
                "asyncio.run(run())"
            ),
            str(database_path),
            str(socket_path),
            schema_generation,
        ]
    return subprocess.Popen(
        command,
        cwd=Path(__file__).parents[1],
        env=os.environ.copy(),
    )


async def _eventually_new_ping(client: CatalogClient, old_pid: int, timeout: float = 10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            ping = await client.call("ping.v2")
            if ping["pid"] != old_pid:
                return ping
        except Exception:
            pass
        await asyncio.sleep(0.05)
    raise AssertionError("catalogd supervisor did not replace the dead process")


async def _eventually_running_status(path: Path, expected_pid: int, timeout: float = 10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            payload = json.loads(path.read_text())
            if payload.get("status") == "running" and payload.get("ping", {}).get("pid") == expected_pid:
                return payload
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        await asyncio.sleep(0.05)
    raise AssertionError("catalogd supervisor status did not converge to running")


@pytest.mark.asyncio
async def test_supervisor_owns_restarts_and_stops_child(supervisor_paths):
    database_path, socket_path = supervisor_paths
    supervisor = CatalogdSupervisor(database_path=database_path, socket_path=socket_path)
    first = await supervisor.start()
    assert supervisor.ownership == "owned"
    assert supervisor._process is not None
    supervisor._process.kill()
    replacement = await _eventually_new_ping(supervisor.client, first["pid"])
    assert replacement["catalog_id"] == first["catalog_id"]
    status = await _eventually_running_status(supervisor.status_path, replacement["pid"])
    assert status["restart_count"] == 1
    await supervisor.stop()
    assert not socket_path.exists()


@pytest.mark.asyncio
async def test_supervisor_adopts_compatible_existing_daemon_without_killing_it(supervisor_paths):
    database_path, socket_path = supervisor_paths
    external = _external_catalogd(database_path, socket_path)
    probe = CatalogClient(socket_path)
    try:
        deadline = asyncio.get_running_loop().time() + 10
        while True:
            try:
                external_ping = await probe.call("ping.v2")
                break
            except Exception:
                if asyncio.get_running_loop().time() >= deadline:
                    raise
                await asyncio.sleep(0.05)
        supervisor = CatalogdSupervisor(database_path=database_path, socket_path=socket_path)
        adopted_ping = await supervisor.start()
        assert adopted_ping["pid"] == external_ping["pid"]
        assert supervisor.ownership == "adopted"
        await supervisor.stop()
        assert external.poll() is None
        assert (await probe.call("ping.v2"))["pid"] == external.pid
    finally:
        await probe.close()
        if external.poll() is None:
            external.terminate()
            external.wait(timeout=10)


@pytest.mark.asyncio
async def test_supervisor_waits_for_incompatible_owner_then_takes_over(supervisor_paths):
    database_path, socket_path = supervisor_paths
    external = _external_catalogd(database_path, socket_path, schema_generation="old-generation")
    probe = CatalogClient(socket_path)
    supervisor = CatalogdSupervisor(database_path=database_path, socket_path=socket_path)
    start_task = None
    try:
        deadline = asyncio.get_running_loop().time() + 5
        while True:
            try:
                old_ping = await probe.call("ping.v2")
                if old_ping["schema_generation"] == "old-generation":
                    break
            except Exception:
                pass
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("incompatible catalogd never became visible")
            await asyncio.sleep(0.05)
        start_task = asyncio.create_task(supervisor.start(readiness_timeout_seconds=10))
        await asyncio.sleep(0.15)
        assert not start_task.done()
        assert external.poll() is None

        external.terminate()
        external.wait(timeout=10)
        replacement = await start_task
        assert replacement["pid"] != old_ping["pid"]
        assert supervisor.ownership == "owned"
    finally:
        await probe.close()
        if external.poll() is None:
            external.terminate()
            external.wait(timeout=10)
        if start_task is not None and not start_task.done():
            start_task.cancel()
        await supervisor.stop()


@pytest.mark.asyncio
async def test_api_readiness_survives_catalogd_kill_and_recovers(supervisor_paths, monkeypatch):
    from types import SimpleNamespace

    from zerg.catalogd.schema import CATALOG_SCHEMA_GENERATION
    from zerg.catalogd.schema import CATALOG_SCHEMA_VERSION
    from zerg.database import make_engine
    from zerg.routers import health as health_router

    database_path, socket_path = supervisor_paths
    supervisor = CatalogdSupervisor(database_path=database_path, socket_path=socket_path)
    first = await supervisor.start()
    engine = make_engine(f"sqlite:///{database_path}")

    import zerg.database as database_module

    monkeypatch.setattr(health_router, "get_settings", lambda: SimpleNamespace(testing=False))
    monkeypatch.setattr(database_module, "default_engine", engine)
    monkeypatch.setattr(database_module, "get_live_engine", lambda: engine)
    monkeypatch.setattr(database_module, "live_catalog_enabled", lambda: True)
    monkeypatch.setattr(database_module, "live_store_configured", lambda: True)
    monkeypatch.setattr(database_module, "get_wal_bytes", lambda: 0)
    monkeypatch.setattr(
        "zerg.services.catalogd_supervisor.catalogd_paths",
        lambda: (database_path, socket_path),
    )
    monkeypatch.setattr(health_router, "_write_serializer_stall_check", lambda: (False, {}))
    monkeypatch.setattr(health_router, "_live_write_serializer_check", lambda: (False, {}))
    monkeypatch.setattr(health_router, "_archive_worker_check", lambda: {"enabled": False})

    app = FastAPI()
    app.get("/readyz")(health_router.readyz_check)
    client = TestClient(app)
    try:
        healthy = client.get("/readyz")
        assert healthy.status_code == 200
        assert healthy.json() == {"status": "ok"}

        assert supervisor._process is not None
        supervisor._process.kill()
        await supervisor._process.wait()
        unavailable = client.get("/readyz")
        assert unavailable.status_code == 503
        assert unavailable.json()["reason"] == "catalog_unavailable"

        replacement = await _eventually_new_ping(supervisor.client, first["pid"])
        assert replacement["schema_version"] == CATALOG_SCHEMA_VERSION
        assert replacement["schema_generation"] == CATALOG_SCHEMA_GENERATION
        recovered = client.get("/readyz")
        assert recovered.status_code == 200
    finally:
        client.close()
        await supervisor.stop()
        engine.dispose()


def test_catalogd_paths_falls_back_to_short_private_runtime_dir(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from zerg.services import catalogd_supervisor as supervisor_module

    database_path = tmp_path / ("deep" * 30) / "longhouse-live.db"
    monkeypatch.setattr(
        supervisor_module,
        "get_settings_unchecked",
        lambda: SimpleNamespace(live_database_url=f"sqlite:///{database_path}"),
    )

    selected_database, socket_path = supervisor_module.catalogd_paths()

    assert selected_database == database_path
    assert len(os.fsencode(socket_path.with_name(f".{socket_path.name}.tmp.{os.getpid()}"))) < 104
    assert socket_path.parent.stat().st_mode & 0o077 == 0
