from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest

from zerg.catalogd.client import CatalogClient
from zerg.searchd.store import SCHEMA_GENERATION
from zerg.searchd.store import SCHEMA_VERSION
from zerg.services.searchd_supervisor import SearchdSupervisor


@pytest.fixture
def supervisor_paths():
    root = Path("/tmp") / f"lhsds-{uuid4().hex[:10]}"
    root.mkdir(mode=0o700)
    yield root / "search.db", root / "searchd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


async def _eventually_restarted(client: CatalogClient, old_pid: int, timeout: float = 10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            ping = await client.call("search.ping.v2")
            if ping["pid"] != old_pid:
                return ping
        except Exception:
            pass
        await asyncio.sleep(0.05)
    raise AssertionError("searchd supervisor did not replace the dead process")


@pytest.mark.asyncio
async def test_searchd_supervisor_owns_restarts_and_stops_child(supervisor_paths):
    database_path, socket_path = supervisor_paths
    supervisor = SearchdSupervisor(database_path=database_path, socket_path=socket_path)
    first = await supervisor.start(readiness_timeout_seconds=10)
    assert first is not None
    assert first["schema_version"] == SCHEMA_VERSION
    assert first["schema_generation"] == SCHEMA_GENERATION
    assert supervisor.ownership == "owned"
    assert supervisor._process is not None

    supervisor._process.kill()
    replacement = await _eventually_restarted(supervisor.client, first["pid"])
    assert replacement["pid"] != first["pid"]
    assert supervisor._restart_count == 1

    await supervisor.stop()
    assert supervisor._process is None
    assert supervisor._task is None
    assert not socket_path.exists()


@pytest.mark.asyncio
async def test_searchd_start_failure_is_nonfatal_and_supervision_keeps_retrying(supervisor_paths, monkeypatch):
    database_path, socket_path = supervisor_paths
    supervisor = SearchdSupervisor(database_path=database_path, socket_path=socket_path)

    async def fail_spawn():
        raise OSError("synthetic searchd spawn failure")

    monkeypatch.setattr(supervisor, "_spawn_process", fail_spawn)
    ping = await supervisor.start(readiness_timeout_seconds=0.12)
    assert ping is None
    assert supervisor._task is not None and not supervisor._task.done()
    assert supervisor._restart_count >= 1
    await supervisor.stop()
    assert supervisor._task is None
    assert supervisor._process is None


def test_searchd_paths_fall_back_to_short_private_runtime_dir(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from zerg.services import searchd_supervisor as supervisor_module

    catalog_path = tmp_path / ("deep" * 30) / "longhouse-live.db"
    monkeypatch.setattr(
        supervisor_module,
        "get_settings_unchecked",
        lambda: SimpleNamespace(live_database_url=f"sqlite:///{catalog_path}"),
    )

    database_path, socket_path = supervisor_module.searchd_paths()

    assert database_path == catalog_path.parent / "search.db"
    assert len(os.fsencode(socket_path.with_name(f".{socket_path.name}.tmp.{os.getpid()}"))) < 104
    assert socket_path.parent.stat().st_mode & 0o077 == 0


def test_search_projector_has_background_write_budget(supervisor_paths):
    database_path, socket_path = supervisor_paths
    supervisor = SearchdSupervisor(database_path=database_path, socket_path=socket_path)

    assert supervisor.client.default_timeout_seconds == 1.0
    assert supervisor.projector_client.default_timeout_seconds == 240.0
