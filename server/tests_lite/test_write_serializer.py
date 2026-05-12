from __future__ import annotations

import asyncio
import base64
import os
import subprocess
import sys
import threading
import time
from contextvars import ContextVar
from pathlib import Path
from textwrap import dedent

import pytest
from sqlalchemy import text as sa_text

from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.services.write_serializer import WriteSerializer


@pytest.mark.asyncio
async def test_high_priority_write_jumps_ahead_of_queued_background_work(tmp_path):
    db_path = tmp_path / "write-serializer.db"
    engine = make_engine(f"sqlite:///{db_path}")
    session_factory = make_sessionmaker(engine)

    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE writes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL)")

    serializer = WriteSerializer()
    serializer.configure(session_factory)

    started = threading.Event()
    run_order: list[str] = []

    def _make_write(label: str, delay: float = 0.0):
        def _write(db):
            run_order.append(label)
            if label == "first":
                started.set()
            if delay > 0:
                time.sleep(delay)
            db.execute(sa_text("INSERT INTO writes(label) VALUES (:label)"), {"label": label})

        return _write

    first = asyncio.create_task(serializer.execute(_make_write("first", delay=0.05), label="summary"))
    await asyncio.to_thread(started.wait, 1.0)

    background = asyncio.create_task(serializer.execute(_make_write("background"), label="commis-claim"))
    interactive = asyncio.create_task(serializer.execute(_make_write("interactive"), label="refresh-session"))

    await asyncio.gather(first, background, interactive)

    assert run_order == ["first", "interactive", "background"]

    with session_factory() as db:
        persisted = [row[0] for row in db.execute(sa_text("SELECT label FROM writes ORDER BY id")).fetchall()]

    assert persisted == ["first", "interactive", "background"]


@pytest.mark.asyncio
async def test_runtime_and_archive_writes_jump_ahead_of_presence_chatter(tmp_path):
    db_path = tmp_path / "write-serializer-priority.db"
    engine = make_engine(f"sqlite:///{db_path}")
    session_factory = make_sessionmaker(engine)

    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE writes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL)")

    serializer = WriteSerializer()
    serializer.configure(session_factory)

    started = threading.Event()
    run_order: list[str] = []

    def _make_write(label: str, delay: float = 0.0):
        def _write(db):
            run_order.append(label)
            if label == "first":
                started.set()
            if delay > 0:
                time.sleep(delay)
            db.execute(sa_text("INSERT INTO writes(label) VALUES (:label)"), {"label": label})

        return _write

    first = asyncio.create_task(serializer.execute(_make_write("first", delay=0.05), label="summary"))
    await asyncio.to_thread(started.wait, 1.0)

    presence = asyncio.create_task(serializer.execute(_make_write("presence"), label="presence"))
    ingest = asyncio.create_task(serializer.execute(_make_write("ingest"), label="ingest"))
    runtime = asyncio.create_task(serializer.execute(_make_write("runtime"), label="runtime-events"))

    await asyncio.gather(first, presence, ingest, runtime)

    assert run_order == ["first", "runtime", "ingest", "presence"]


@pytest.mark.asyncio
async def test_cancelled_write_keeps_writer_slot_until_worker_thread_finishes(tmp_path):
    db_path = tmp_path / "write-serializer-cancel.db"
    engine = make_engine(f"sqlite:///{db_path}")
    session_factory = make_sessionmaker(engine)

    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE writes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL)")

    serializer = WriteSerializer()
    serializer.configure(session_factory)

    first_started = threading.Event()
    timings: dict[str, float] = {}

    def _first_write(db):
        timings["first_start"] = time.monotonic()
        first_started.set()
        time.sleep(0.15)
        db.execute(sa_text("INSERT INTO writes(label) VALUES ('first')"))
        timings["first_end"] = time.monotonic()

    def _second_write(db):
        timings["second_start"] = time.monotonic()
        db.execute(sa_text("INSERT INTO writes(label) VALUES ('second')"))
        timings["second_end"] = time.monotonic()

    first = asyncio.create_task(serializer.execute(_first_write, label="summary"))
    await asyncio.to_thread(first_started.wait, 1.0)

    first.cancel()
    second = asyncio.create_task(serializer.execute(_second_write, label="refresh-session"))

    cancel_started = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await first
    cancel_elapsed = time.monotonic() - cancel_started
    await second

    assert cancel_elapsed < 0.05
    assert timings["second_start"] >= timings["first_end"]
    assert serializer.stats.total_writes == 2
    assert serializer.stats.errors == 0

    with session_factory() as db:
        persisted = [row[0] for row in db.execute(sa_text("SELECT label FROM writes ORDER BY id")).fetchall()]

    assert persisted == ["first", "second"]


@pytest.mark.asyncio
async def test_dynamic_session_factory_routes_writes_by_context(tmp_path):
    base_engine = make_engine(f"sqlite:///{tmp_path / 'base.db'}")
    base_factory = make_sessionmaker(base_engine)
    commis_engine = make_engine(f"sqlite:///{tmp_path / 'commis.db'}")
    commis_factory = make_sessionmaker(commis_engine)

    for engine in (base_engine, commis_engine):
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE writes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL)")

    current_target: ContextVar[str] = ContextVar("current_target", default="base")

    serializer = WriteSerializer()
    serializer.configure_resolver(lambda: commis_factory if current_target.get() == "commis" else base_factory)

    await serializer.execute(
        lambda db: db.execute(sa_text("INSERT INTO writes(label) VALUES ('base-write')")),
        label="summary",
    )

    token = current_target.set("commis")
    try:
        await serializer.execute(
            lambda db: db.execute(sa_text("INSERT INTO writes(label) VALUES ('commis-write')")),
            label="summary",
        )
    finally:
        current_target.reset(token)

    with base_factory() as db:
        base_rows = [row[0] for row in db.execute(sa_text("SELECT label FROM writes ORDER BY id")).fetchall()]
    with commis_factory() as db:
        commis_rows = [row[0] for row in db.execute(sa_text("SELECT label FROM writes ORDER BY id")).fetchall()]

    assert base_rows == ["base-write"]
    assert commis_rows == ["commis-write"]


@pytest.mark.asyncio
async def test_execute_or_direct_prefers_fallback_db_for_testing_request_overrides(tmp_path, monkeypatch):
    global_engine = make_engine(f"sqlite:///{tmp_path / 'global.db'}")
    global_factory = make_sessionmaker(global_engine)
    request_engine = make_engine(f"sqlite:///{tmp_path / 'request.db'}")
    request_factory = make_sessionmaker(request_engine)

    for engine in (global_engine, request_engine):
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE writes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL)")

    serializer = WriteSerializer()
    serializer.configure(global_factory)
    monkeypatch.setenv("TESTING", "1")

    with request_factory() as request_db:
        await serializer.execute_or_direct(
            lambda db: db.execute(sa_text("INSERT INTO writes(label) VALUES ('request-write')")),
            request_db,
            label="summary",
        )

    with global_factory() as db:
        global_rows = [row[0] for row in db.execute(sa_text("SELECT label FROM writes ORDER BY id")).fetchall()]
    with request_factory() as db:
        request_rows = [row[0] for row in db.execute(sa_text("SELECT label FROM writes ORDER BY id")).fetchall()]

    assert global_rows == []
    assert request_rows == ["request-write"]


def test_full_app_ingest_succeeds_in_subprocess_without_testing_flag(tmp_path):
    """Regression: full-app ingest must work on the production-style writer path."""

    db_path = tmp_path / "full-app-ingest.db"
    server_dir = Path(__file__).resolve().parents[1]
    payload_script = dedent(
        """
        import json
        from datetime import datetime, timezone
        from uuid import uuid4

        from fastapi.testclient import TestClient

        from zerg.main import app

        session_id = str(uuid4())
        payload = {
            "id": session_id,
            "provider": "claude",
            "environment": "development",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "events": [
                {
                    "role": "user",
                    "content_text": "hello",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "source_path": "/tmp/session.jsonl",
                    "source_offset": 1,
                    "raw_json": "{\\"type\\":\\"user\\",\\"message\\":\\"hello\\"}",
                }
            ],
            "source_lines": [],
        }

        with TestClient(app) as client:
            ingest = client.post("/api/agents/ingest", content=json.dumps(payload))
            print("ingest", ingest.status_code, ingest.text)
            if ingest.status_code != 200:
                raise SystemExit(1)

            session = client.get(f"/api/agents/sessions/{session_id}")
            print("session", session.status_code, session.text)
            if session.status_code != 200:
                raise SystemExit(2)
        """
    )

    env = os.environ.copy()
    env.pop("TESTING", None)
    env.update(
        {
            "DATABASE_URL": f"sqlite:///{db_path}",
            "AUTH_DISABLED": "1",
            "SKIP_DEMO_SEED": "1",
            "JOB_QUEUE_ENABLED": "0",
            "FERNET_SECRET": base64.urlsafe_b64encode(os.urandom(32)).decode(),
            "TRIGGER_SIGNING_SECRET": base64.urlsafe_b64encode(os.urandom(32)).decode(),
        }
    )

    result = subprocess.run(
        [sys.executable, "-c", payload_script],
        cwd=server_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, (
        f"subprocess exited {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
