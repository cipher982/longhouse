from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from starlette.requests import Request

from zerg.database import Base
from zerg.database import initialize_live_database
from zerg.database import make_live_engine
from zerg.services.archive_read_proxy import proxy_archive_read
from zerg.services.archive_read_proxy import should_proxy_archive_read
from zerg.services.archive_read_subprocess import _readonly_sqlite_url


def _request(path: str, query: str = "") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "server": ("test", 443),
            "client": ("127.0.0.1", 1),
            "root_path": "/api",
            "path": f"/api{path}",
            "raw_path": f"/api{path}".encode(),
            "query_string": query.encode(),
            "headers": [],
        }
    )


def test_archive_read_proxy_routes_only_cold_get_surfaces():
    assert should_proxy_archive_read(_request("/timeline/sessions/00000000-0000-0000-0000-000000000001/workspace"))
    assert should_proxy_archive_read(_request("/agents/sessions", "query=sqlite"))
    assert should_proxy_archive_read(_request("/timeline/recall", "query=sqlite"))
    assert not should_proxy_archive_read(_request("/timeline/sessions"))
    assert not should_proxy_archive_read(_request("/timeline/sessions/stream"))
    assert not should_proxy_archive_read(_request("/agents/machines"))


def test_archive_child_opens_sqlite_files_read_only(tmp_path):
    database_path = tmp_path / "archive with spaces.db"
    database_path.touch()
    readonly = _readonly_sqlite_url(f"sqlite:///{database_path}")
    engine = create_engine(readonly)
    with engine.connect() as connection:
        with pytest.raises(OperationalError, match="readonly database"):
            connection.exec_driver_sql("CREATE TABLE forbidden (id INTEGER)")
    assert _readonly_sqlite_url(readonly) == readonly


@pytest.mark.asyncio
async def test_archive_read_proxy_preserves_child_response(monkeypatch):
    class Child:
        returncode = 0

        async def communicate(self, _payload):
            envelope = {
                "status_code": 404,
                "headers": {"content-type": "application/json", "connection": "close"},
                "body_b64": base64.b64encode(b'{"detail":"missing"}').decode(),
            }
            return json.dumps(envelope).encode(), b""

    async def spawn(*_args, **_kwargs):
        return Child()

    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
    response = await proxy_archive_read(_request("/agents/sessions/00000000-0000-0000-0000-000000000001"))
    assert response.status_code == 404
    assert response.body == b'{"detail":"missing"}'
    assert "connection" not in response.headers


@pytest.mark.asyncio
async def test_archive_read_native_exit_degrades_one_request(monkeypatch):
    class Child:
        returncode = 139

        async def communicate(self, _payload):
            return b"", b"segmentation fault"

    async def spawn(*_args, **_kwargs):
        return Child()

    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
    with pytest.raises(HTTPException) as exc_info:
        await proxy_archive_read(_request("/agents/sessions/00000000-0000-0000-0000-000000000001"))
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "archive_read_unavailable"


def test_archive_read_child_serves_real_archive_route(tmp_path):
    database_path = tmp_path / "archive.db"
    live_path = tmp_path / "archive-live.db"
    Base.metadata.create_all(create_engine(f"sqlite:///{database_path}"))
    initialize_live_database(make_live_engine(f"sqlite:///{live_path}"))
    env = dict(os.environ)
    env.update(
        {
            "AUTH_DISABLED": "1",
                "DATABASE_URL": f"sqlite:///{database_path}",
                "LONGHOUSE_LIVE_DATABASE_URL": f"sqlite:///{live_path}",
                "LONGHOUSE_LIVE_DB_PATH": str(live_path),
            "LONGHOUSE_LIVE_CATALOG_ENABLED": "0",
            "LONGHOUSE_ARCHIVE_WORKER_ENABLED": "0",
        }
    )
    completed = subprocess.run(
        [sys.executable, "-m", "zerg.services.archive_read_subprocess"],
        input=json.dumps({"method": "GET", "path": f"/agents/sessions/{uuid4()}", "query": ""}),
        text=True,
        capture_output=True,
        env=env,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    envelope = json.loads(completed.stdout)
    assert envelope["status_code"] == 404
