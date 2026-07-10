from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from uuid import uuid4

import pytest
from fastapi import Depends
from fastapi import FastAPI
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from starlette.requests import Request

from zerg.database import Base
from zerg.database import get_db
from zerg.database import initialize_live_database
from zerg.database import make_live_engine
from zerg.main import api_app
from zerg.services.archive_read_proxy import proxy_archive_request
from zerg.services.archive_read_proxy import should_proxy_archive_request
from zerg.services.archive_read_subprocess import _request_is_read_only
from zerg.services.archive_read_subprocess import _readonly_sqlite_url


def _request(path: str, query: str = "", method: str = "GET", body: bytes = b"") -> Request:
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": method,
            "scheme": "https",
            "server": ("test", 443),
            "client": ("127.0.0.1", 1),
            "root_path": "/api",
            "path": f"/api{path}",
            "raw_path": f"/api{path}".encode(),
            "query_string": query.encode(),
            "headers": [],
        },
        receive,
    )


def test_archive_read_proxy_routes_only_cold_get_surfaces():
    app = FastAPI()

    @app.get("/agents/worklog/day")
    def archive_backed_agent_read(_db=Depends(get_db)):
        return {}

    @app.get("/agents/sessions/{session_id}/archive-bundle")
    def archive_bundle_read(_db=Depends(get_db)):
        return {}

    @app.patch("/timeline/sessions/{session_id}/loop-mode")
    def archive_backed_mutation(_db=Depends(get_db)):
        return {}

    assert should_proxy_archive_request(_request("/timeline/sessions/00000000-0000-0000-0000-000000000001/workspace"))
    assert should_proxy_archive_request(_request("/agents/sessions", "query=sqlite"))
    assert should_proxy_archive_request(_request("/timeline/recall", "query=sqlite"))
    assert should_proxy_archive_request(_request("/agents/worklog/day"), routes=app.routes)
    assert should_proxy_archive_request(
        _request("/agents/sessions/00000000-0000-0000-0000-000000000001/archive-bundle"),
        routes=app.routes,
    )
    assert should_proxy_archive_request(
        _request(
            "/timeline/sessions/00000000-0000-0000-0000-000000000001/loop-mode",
            method="PATCH",
        ),
        routes=app.routes,
    )
    assert not should_proxy_archive_request(_request("/timeline/sessions"))
    assert not should_proxy_archive_request(_request("/timeline/sessions/stream"))
    assert not should_proxy_archive_request(_request("/agents/machines"))


@pytest.mark.parametrize(
    "path",
    (
        "/agents/worklog/day",
        "/agents/ingest-health",
        "/agents/usage-stats",
        "/agents/machines/health",
    ),
)
def test_archive_backed_machine_gets_are_discovered_from_route_dependencies(path):
    assert should_proxy_archive_request(_request(path), routes=api_app.routes)


@pytest.mark.parametrize(
    ("path", "method"),
    (
        ("/timeline/sessions/00000000-0000-0000-0000-000000000001/export", "GET"),
        ("/timeline/workflows/00000000-0000-0000-0000-000000000001", "GET"),
        ("/timeline/session-shares/example-token/resolve", "GET"),
    ),
)
def test_archive_backed_timeline_routes_are_discovered_from_dependencies(path, method):
    assert should_proxy_archive_request(_request(path, method=method), routes=api_app.routes)


def test_archive_child_opens_sqlite_files_read_only(tmp_path):
    database_path = tmp_path / "archive with spaces.db"
    database_path.touch()
    readonly = _readonly_sqlite_url(f"sqlite:///{database_path}")
    engine = create_engine(readonly)
    with engine.connect() as connection:
        with pytest.raises(OperationalError, match="readonly database"):
            connection.exec_driver_sql("CREATE TABLE forbidden (id INTEGER)")
    assert _readonly_sqlite_url(readonly) == readonly


def test_source_line_claim_post_uses_read_only_archive_connection():
    assert _request_is_read_only("POST", "/agents/source-lines/claims")
    assert _request_is_read_only("GET", "/agents/sessions/session/archive-bundle")
    assert not _request_is_read_only("POST", "/agents/ingest")


def test_explicit_database_urls_win_over_dotenv(tmp_path):
    explicit_archive = f"sqlite:///{tmp_path / 'explicit-archive.db'}"
    explicit_live = f"sqlite:///{tmp_path / 'explicit-live.db'}"
    (tmp_path / ".env").write_text(
        "DATABASE_URL=sqlite:///dotenv-archive.db\n"
        "LONGHOUSE_LIVE_DATABASE_URL=sqlite:///dotenv-live.db\n"
    )
    env = os.environ.copy()
    env["DATABASE_URL"] = explicit_archive
    env["LONGHOUSE_LIVE_DATABASE_URL"] = explicit_live
    env.pop("TESTING", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; import zerg.database as database; "
                "print(json.dumps([database._settings.database_url, database._settings.live_database_url]))"
            ),
        ],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert json.loads(completed.stdout) == [explicit_archive, explicit_live]


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
    response = await proxy_archive_request(_request("/agents/sessions/00000000-0000-0000-0000-000000000001"))
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
        await proxy_archive_request(_request("/agents/sessions/00000000-0000-0000-0000-000000000001"))
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "archive_request_unavailable"


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


def test_archive_child_serves_real_archive_mutation(tmp_path):
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
        }
    )
    from_session_id = uuid4()
    body = json.dumps(
        {
            "from_session_id": str(from_session_id),
            "to_session_id": str(uuid4()),
            "text": "hello",
        }
    ).encode("utf-8")
    completed = subprocess.run(
        [sys.executable, "-m", "zerg.services.archive_read_subprocess"],
        input=json.dumps(
            {
                "method": "POST",
                "path": "/agents/messages",
                "query": "",
                "headers": {"content-type": "application/json"},
                "body_b64": base64.b64encode(body).decode("ascii"),
            }
        ),
        text=True,
        capture_output=True,
        env=env,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    envelope = json.loads(completed.stdout)
    assert envelope["status_code"] == 404
