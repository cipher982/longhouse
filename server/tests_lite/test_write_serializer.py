from __future__ import annotations

import asyncio
import base64
import logging
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
from zerg.services.write_serializer import execute_post_write
from zerg.services.write_serializer import InterruptedWriteError
from zerg.services.write_serializer import post_write_fallback_db
from zerg.services.write_serializer import request_session_released_by_serializer
from zerg.services.write_serializer import WriteSerializer
from zerg.services.write_serializer import WriteQueueTimeoutError


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

    background = asyncio.create_task(serializer.execute(_make_write("background"), label="background-claim"))
    interactive = asyncio.create_task(serializer.execute(_make_write("interactive"), label="refresh-session"))

    await asyncio.gather(first, background, interactive)

    assert run_order == ["first", "interactive", "background"]

    with session_factory() as db:
        persisted = [row[0] for row in db.execute(sa_text("SELECT label FROM writes ORDER BY id")).fetchall()]

    assert persisted == ["first", "interactive", "background"]


@pytest.mark.asyncio
async def test_runtime_writes_jump_ahead_of_presence_and_archive_chatter(tmp_path):
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
    runtime = asyncio.create_task(serializer.execute(_make_write("runtime"), label="runtime-observations"))

    await asyncio.gather(first, presence, ingest, runtime)

    assert run_order == ["first", "runtime", "presence", "ingest"]


@pytest.mark.asyncio
async def test_queue_timeout_removes_waiting_write_and_unblocks_followup(tmp_path):
    db_path = tmp_path / "write-serializer-queue-timeout.db"
    engine = make_engine(f"sqlite:///{db_path}")
    session_factory = make_sessionmaker(engine)

    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE writes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL)")

    serializer = WriteSerializer()
    serializer.configure(session_factory)

    started = threading.Event()
    run_order: list[str] = []

    def _write(label: str, delay: float = 0.0):
        def _inner(db):
            run_order.append(label)
            if label == "first":
                started.set()
            if delay > 0:
                time.sleep(delay)
            db.execute(sa_text("INSERT INTO writes(label) VALUES (:label)"), {"label": label})

        return _inner

    first = asyncio.create_task(serializer.execute(_write("first", delay=0.1), label="ingest"))
    await asyncio.to_thread(started.wait, 1.0)

    with pytest.raises(WriteQueueTimeoutError):
        await serializer.execute(
            _write("timed-out"),
            label="runtime-observations",
            queue_timeout_seconds=0.01,
        )

    await first
    await serializer.execute(_write("followup"), label="runtime-observations")

    assert run_order == ["first", "followup"]
    assert serializer.queue_depth == 0
    assert not serializer.writer_active

    with session_factory() as db:
        persisted = [row[0] for row in db.execute(sa_text("SELECT label FROM writes ORDER BY id")).fetchall()]

    assert persisted == ["first", "followup"]


@pytest.mark.asyncio
async def test_background_ingest_repair_stays_behind_machine_health_signals(tmp_path):
    db_path = tmp_path / "write-serializer-background-ingest-priority.db"
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

    replay = asyncio.create_task(serializer.execute(_make_write("replay"), label="ingest-replay"))
    scan = asyncio.create_task(serializer.execute(_make_write("scan"), label="ingest-scan"))
    presence = asyncio.create_task(serializer.execute(_make_write("presence"), label="presence"))
    heartbeat = asyncio.create_task(serializer.execute(_make_write("heartbeat"), label="heartbeat"))
    runtime = asyncio.create_task(serializer.execute(_make_write("runtime"), label="runtime-observations"))
    live_operation = asyncio.create_task(
        serializer.execute(_make_write("live-operation"), label="live-machine-control-operation")
    )
    live_result = asyncio.create_task(
        serializer.execute(_make_write("live-result"), label="live-machine-control-result")
    )
    live_fail = asyncio.create_task(serializer.execute(_make_write("live-fail"), label="live-machine-control-fail"))
    result = asyncio.create_task(serializer.execute(_make_write("result"), label="machine-control-result"))
    reaper = asyncio.create_task(serializer.execute(_make_write("reaper"), label="machine-control-reaper"))

    await asyncio.gather(
        first,
        replay,
        scan,
        presence,
        heartbeat,
        runtime,
        live_operation,
        live_result,
        live_fail,
        result,
        reaper,
    )

    assert run_order == [
        "first",
        "live-operation",
        "live-result",
        "live-fail",
        "runtime",
        "result",
        "reaper",
        "presence",
        "heartbeat",
        "replay",
        "scan",
    ]


@pytest.mark.asyncio
async def test_live_ingest_jumps_ahead_of_archive_ingest(tmp_path):
    db_path = tmp_path / "write-serializer-live-ingest-priority.db"
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

    archive = asyncio.create_task(serializer.execute(_make_write("archive"), label="ingest"))
    live = asyncio.create_task(serializer.execute(_make_write("live"), label="ingest-live"))
    presence = asyncio.create_task(serializer.execute(_make_write("presence"), label="presence"))

    await asyncio.gather(first, archive, live, presence)

    assert run_order == ["first", "live", "presence", "archive"]


@pytest.mark.asyncio
async def test_summary_task_bookkeeping_jumps_ahead_of_archive_ingest(tmp_path):
    db_path = tmp_path / "write-serializer-summary-task-priority.db"
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

    first = asyncio.create_task(serializer.execute(_make_write("first", delay=0.05), label="presence"))
    await asyncio.to_thread(started.wait, 1.0)

    archive = asyncio.create_task(serializer.execute(_make_write("archive"), label="ingest"))
    managed_launch = asyncio.create_task(serializer.execute(_make_write("managed-launch"), label="managed-launch"))
    task_timeout = asyncio.create_task(serializer.execute(_make_write("task-timeout"), label="task-timeout"))
    summary = asyncio.create_task(serializer.execute(_make_write("summary"), label="summary"))
    task_done = asyncio.create_task(serializer.execute(_make_write("task-done"), label="task-done"))
    runtime = asyncio.create_task(serializer.execute(_make_write("runtime"), label="runtime-observations"))
    live = asyncio.create_task(serializer.execute(_make_write("live"), label="ingest-live"))
    fanout = asyncio.create_task(serializer.execute(_make_write("server-fanout"), label="server-fanout"))

    await asyncio.gather(first, archive, managed_launch, task_timeout, summary, task_done, runtime, live, fanout)

    assert run_order == [
        "first",
        "managed-launch",
        "live",
        "runtime",
        "task-timeout",
        "summary",
        "task-done",
        "archive",
        "server-fanout",
    ]


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
    alternate_engine = make_engine(f"sqlite:///{tmp_path / 'alternate.db'}")
    alternate_factory = make_sessionmaker(alternate_engine)

    for engine in (base_engine, alternate_engine):
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE writes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL)")

    current_target: ContextVar[str] = ContextVar("current_target", default="base")

    serializer = WriteSerializer()
    serializer.configure_resolver(lambda: alternate_factory if current_target.get() == "alternate" else base_factory)

    await serializer.execute(
        lambda db: db.execute(sa_text("INSERT INTO writes(label) VALUES ('base-write')")),
        label="summary",
    )

    token = current_target.set("alternate")
    try:
        await serializer.execute(
            lambda db: db.execute(sa_text("INSERT INTO writes(label) VALUES ('alternate-write')")),
            label="summary",
        )
    finally:
        current_target.reset(token)

    with base_factory() as db:
        base_rows = [row[0] for row in db.execute(sa_text("SELECT label FROM writes ORDER BY id")).fetchall()]
    with alternate_factory() as db:
        alternate_rows = [row[0] for row in db.execute(sa_text("SELECT label FROM writes ORDER BY id")).fetchall()]

    assert base_rows == ["base-write"]
    assert alternate_rows == ["alternate-write"]


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


@pytest.mark.asyncio
async def test_execute_after_closing_request_session_releases_fallback_in_configured_runtime(tmp_path, monkeypatch):
    global_engine = make_engine(f"sqlite:///{tmp_path / 'global.db'}")
    global_factory = make_sessionmaker(global_engine)
    request_engine = make_engine(f"sqlite:///{tmp_path / 'request.db'}")
    request_factory = make_sessionmaker(request_engine)

    for engine in (global_engine, request_engine):
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE writes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL)")

    serializer = WriteSerializer()
    serializer.configure(global_factory)
    monkeypatch.delenv("TESTING", raising=False)

    request_db = request_factory()
    closed = False
    original_close = request_db.close

    def close_spy():
        nonlocal closed
        closed = True
        original_close()

    request_db.close = close_spy  # type: ignore[method-assign]
    try:
        await serializer.execute_after_closing_request_session(
            lambda db: db.execute(sa_text("INSERT INTO writes(label) VALUES ('global-write')")),
            request_db,
            label="summary",
        )
    finally:
        if not closed:
            request_db.close()

    with global_factory() as db:
        global_rows = [row[0] for row in db.execute(sa_text("SELECT label FROM writes ORDER BY id")).fetchall()]
    with request_factory() as db:
        request_rows = [row[0] for row in db.execute(sa_text("SELECT label FROM writes ORDER BY id")).fetchall()]

    assert closed is True
    assert global_rows == ["global-write"]
    assert request_rows == []


@pytest.mark.asyncio
async def test_execute_post_write_uses_configured_writer_after_request_session_release(tmp_path, monkeypatch):
    global_engine = make_engine(f"sqlite:///{tmp_path / 'global.db'}")
    global_factory = make_sessionmaker(global_engine)
    request_engine = make_engine(f"sqlite:///{tmp_path / 'request.db'}")
    request_factory = make_sessionmaker(request_engine)

    for engine in (global_engine, request_engine):
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE writes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL)")

    serializer = WriteSerializer()
    serializer.configure(global_factory)
    monkeypatch.delenv("TESTING", raising=False)

    with request_factory() as request_db:
        assert request_session_released_by_serializer(serializer) is True
        assert post_write_fallback_db(serializer, request_db) is None
        await execute_post_write(
            serializer,
            lambda db: db.execute(sa_text("INSERT INTO writes(label) VALUES ('post-write')")),
            post_write_fallback_db(serializer, request_db),
            label="summary",
        )

    with global_factory() as db:
        global_rows = [row[0] for row in db.execute(sa_text("SELECT label FROM writes ORDER BY id")).fetchall()]
    with request_factory() as db:
        request_rows = [row[0] for row in db.execute(sa_text("SELECT label FROM writes ORDER BY id")).fetchall()]

    assert global_rows == ["post-write"]
    assert request_rows == []


@pytest.mark.asyncio
async def test_execute_after_closing_request_session_keeps_fallback_for_testing(tmp_path, monkeypatch):
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
        await serializer.execute_after_closing_request_session(
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
            # Startup validation requires the providers in config/models.json
            # to have their keys set. This subprocess boots without TESTING=1
            # so the real validator runs; supply a stub key to satisfy it.
            "OPENROUTER_API_KEY": "test-openrouter-key",
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


@pytest.mark.asyncio
async def test_active_writer_metrics_track_label_and_age(tmp_path):
    db_path = tmp_path / "write-serializer-active-metrics.db"
    engine = make_engine(f"sqlite:///{db_path}")
    session_factory = make_sessionmaker(engine)

    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE writes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL)")

    serializer = WriteSerializer()
    serializer.configure(session_factory)

    started = threading.Event()
    release = threading.Event()

    def _write(db):
        serializer.set_active_stage("archive-blocked")
        started.set()
        release.wait(1.0)
        db.execute(sa_text("INSERT INTO writes(label) VALUES ('archive')"))

    task = asyncio.create_task(serializer.execute(_write, label="ingest-replay"))
    await asyncio.to_thread(started.wait, 1.0)

    assert serializer.writer_active is True
    assert serializer.active_label == "ingest-replay"
    assert serializer.active_priority is not None
    assert serializer.active_age_ms >= 0.0
    metrics = serializer.get_metrics()
    assert metrics["writer_active"] is True
    assert metrics["active_label"] == "ingest-replay"
    assert metrics["active_stage"] == "archive-blocked"
    assert metrics["queue_depth"] == 0

    release.set()
    await task

    assert serializer.writer_active is False
    assert serializer.active_label is None
    assert serializer.active_stage is None
    assert serializer.active_age_ms == 0.0


@pytest.mark.asyncio
async def test_active_writer_stack_dump_logs_stale_worker(tmp_path, monkeypatch, caplog):
    db_path = tmp_path / "write-serializer-active-stack.db"
    engine = make_engine(f"sqlite:///{db_path}")
    session_factory = make_sessionmaker(engine)

    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE writes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL)")

    monkeypatch.setenv("LONGHOUSE_WRITE_SERIALIZER_STACK_DUMP_AFTER_MS", "1")
    monkeypatch.setenv("LONGHOUSE_WRITE_SERIALIZER_STACK_DUMP_RATE_LIMIT_MS", "0")
    caplog.set_level(logging.WARNING, logger="zerg.services.write_serializer")

    serializer = WriteSerializer()
    serializer.configure(session_factory)

    started = threading.Event()
    release = threading.Event()

    def _write(db):
        started.set()
        release.wait(1.0)
        db.execute(sa_text("INSERT INTO writes(label) VALUES ('archive')"))

    task = asyncio.create_task(serializer.execute(_write, label="ingest-live"))
    await asyncio.to_thread(started.wait, 1.0)

    for _ in range(50):
        if serializer.active_worker_thread_id is not None and serializer.active_age_ms >= 1.0:
            break
        await asyncio.sleep(0.01)

    metrics = serializer.get_metrics()

    assert metrics["writer_active"] is True
    assert metrics["active_label"] == "ingest-live"
    assert metrics["active_job_id"] is not None
    assert metrics["active_worker_thread_id"] is not None
    assert metrics["active_stack_dump_count"] == 1
    assert metrics["last_active_stack_dump_reason"] == "metrics"
    assert "WriteSerializer active writer stack dump" in caplog.text
    assert "_write" in caplog.text

    release.set()
    await task


@pytest.mark.asyncio
async def test_sqlite_interrupt_releases_writer_slot_and_next_write_succeeds(tmp_path, monkeypatch):
    db_path = tmp_path / "write-serializer-interrupt.db"
    engine = make_engine(f"sqlite:///{db_path}")
    session_factory = make_sessionmaker(engine)

    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE writes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL)")

    monkeypatch.setenv("LONGHOUSE_WRITE_SERIALIZER_INTERRUPT_AFTER_SECONDS", "0.05")

    serializer = WriteSerializer()
    serializer.configure(session_factory)

    def _long_sql(db):
        db.execute(
            sa_text(
                """
                WITH RECURSIVE cnt(x) AS (
                  SELECT 0
                  UNION ALL
                  SELECT x + 1 FROM cnt WHERE x < 100000000
                )
                SELECT sum(x) FROM cnt
                """
            )
        ).scalar()

    with pytest.raises(InterruptedWriteError):
        await serializer.execute(_long_sql, label="ingest-live")

    assert serializer.writer_active is False
    assert serializer.queue_depth == 0
    metrics = serializer.get_metrics()
    assert metrics["active_interrupt_count"] == 1
    assert metrics["last_active_interrupt_label"] == "ingest-live"

    await serializer.execute(
        lambda db: db.execute(sa_text("INSERT INTO writes(label) VALUES ('after-interrupt')")),
        label="ingest-live",
    )

    with session_factory() as db:
        persisted = [row[0] for row in db.execute(sa_text("SELECT label FROM writes ORDER BY id")).fetchall()]
    assert persisted == ["after-interrupt"]


@pytest.mark.asyncio
async def test_non_sqlite_stall_escalates_when_interrupt_cannot_unwind(tmp_path, monkeypatch):
    db_path = tmp_path / "write-serializer-non-sqlite-stall.db"
    engine = make_engine(f"sqlite:///{db_path}")
    session_factory = make_sessionmaker(engine)

    monkeypatch.setenv("LONGHOUSE_WRITE_SERIALIZER_INTERRUPT_AFTER_SECONDS", "0.01")
    monkeypatch.setenv("LONGHOUSE_WRITE_SERIALIZER_INTERRUPT_GRACE_SECONDS", "0.01")
    monkeypatch.setenv("LONGHOUSE_WRITE_SERIALIZER_EXIT_ON_WEDGED_WRITER", "0")
    monkeypatch.setenv("LONGHOUSE_WRITE_SERIALIZER_STACK_DUMP_AFTER_MS", "1")
    monkeypatch.setenv("LONGHOUSE_WRITE_SERIALIZER_STACK_DUMP_RATE_LIMIT_MS", "0")

    serializer = WriteSerializer()
    serializer.configure(session_factory)

    started = threading.Event()
    release = threading.Event()

    def _blocked_python(_db):
        started.set()
        release.wait(1.0)

    task = asyncio.create_task(serializer.execute(_blocked_python, label="ingest-live"))
    await asyncio.to_thread(started.wait, 1.0)

    metrics = {}
    for _ in range(100):
        metrics = serializer.get_metrics()
        if metrics["active_wedged_writer_count"] >= 1:
            break
        await asyncio.sleep(0.01)

    assert serializer.writer_active is True
    assert metrics["active_interrupt_count"] == 1
    assert metrics["active_wedged_writer_count"] == 1
    assert metrics["last_active_wedged_writer_label"] == "ingest-live"
    assert metrics["last_active_wedged_writer_reason"] == "interrupt_wedged"
    assert metrics["active_stack_dump_count"] >= 2

    release.set()
    await task
    assert serializer.writer_active is False


@pytest.mark.asyncio
async def test_cancelled_stuck_write_deadman_frees_slot_when_exit_disabled(tmp_path, monkeypatch):
    db_path = tmp_path / "write-serializer-cancelled-stuck.db"
    engine = make_engine(f"sqlite:///{db_path}")
    session_factory = make_sessionmaker(engine)

    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE writes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL)")

    monkeypatch.setenv("LONGHOUSE_WRITE_SERIALIZER_INTERRUPT_AFTER_SECONDS", "0")
    monkeypatch.setenv("LONGHOUSE_WRITE_SERIALIZER_BACKGROUND_FINALIZE_GRACE_SECONDS", "0.02")
    monkeypatch.setenv("LONGHOUSE_WRITE_SERIALIZER_EXIT_ON_WEDGED_WRITER", "0")

    serializer = WriteSerializer()
    serializer.configure(session_factory)

    started = threading.Event()
    release = threading.Event()
    run_order: list[str] = []

    def _stuck_write(_db):
        run_order.append("stuck")
        started.set()
        release.wait(1.0)

    def _next_write(db):
        run_order.append("next")
        db.execute(sa_text("INSERT INTO writes(label) VALUES ('next')"))

    stuck = asyncio.create_task(serializer.execute(_stuck_write, label="ingest-live"))
    await asyncio.to_thread(started.wait, 1.0)

    stuck.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stuck

    next_write = asyncio.create_task(serializer.execute(_next_write, label="refresh-session"))
    await asyncio.wait_for(next_write, timeout=1.0)

    metrics = serializer.get_metrics()
    assert run_order == ["stuck", "next"]
    assert metrics["active_wedged_writer_count"] == 1
    assert metrics["last_active_wedged_writer_label"] == "ingest-live"
    assert metrics["last_active_wedged_writer_reason"] == "background_finalize_deadman"

    release.set()


@pytest.mark.asyncio
async def test_enqueue_wakes_sleeping_head_when_writer_is_idle(tmp_path):
    db_path = tmp_path / "write-serializer-idle-queue.db"
    engine = make_engine(f"sqlite:///{db_path}")
    session_factory = make_sessionmaker(engine)

    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE writes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL)")

    serializer = WriteSerializer()
    serializer.configure(session_factory)
    run_order: list[str] = []

    def _make_write(label: str):
        def _write(db):
            run_order.append(label)
            db.execute(sa_text("INSERT INTO writes(label) VALUES (:label)"), {"label": label})

        return _write

    # Recreate the production invariant violation: an existing caller is asleep
    # at the head of the queue, but the writer has become idle without a notify.
    serializer._writer_active = True  # noqa: SLF001 - white-box regression for queue liveness
    first = asyncio.create_task(serializer.execute(_make_write("first"), label="ingest-replay"))

    for _ in range(50):
        if serializer.queue_depth == 1:
            break
        await asyncio.sleep(0.01)
    assert serializer.queue_depth == 1

    serializer._writer_active = False  # noqa: SLF001
    second = asyncio.create_task(serializer.execute(_make_write("second"), label="refresh-session"))

    await asyncio.wait_for(asyncio.gather(first, second), timeout=1.0)
    assert sorted(run_order) == ["first", "second"]
    assert serializer.queue_depth == 0
    assert serializer.get_metrics()["idle_queue_stalled"] is False


@pytest.mark.asyncio
async def test_repair_idle_queue_promotes_existing_head_without_new_enqueue(tmp_path):
    db_path = tmp_path / "write-serializer-idle-queue-repair.db"
    engine = make_engine(f"sqlite:///{db_path}")
    session_factory = make_sessionmaker(engine)

    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE writes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL)")

    serializer = WriteSerializer()
    serializer.configure(session_factory)
    run_order: list[str] = []

    def _write(db):
        run_order.append("first")
        db.execute(sa_text("INSERT INTO writes(label) VALUES ('first')"))

    # Recreate an idle writer with work already queued. Archive admission reads
    # this state before any later enqueue can incidentally promote the head.
    serializer._writer_active = True  # noqa: SLF001 - white-box regression for queue liveness
    first = asyncio.create_task(serializer.execute(_write, label="ingest-replay"))

    for _ in range(50):
        if serializer.queue_depth == 1:
            break
        await asyncio.sleep(0.01)
    assert serializer.queue_depth == 1

    serializer._writer_active = False  # noqa: SLF001
    repaired = await serializer.repair_idle_queue()

    assert repaired is True
    await asyncio.wait_for(first, timeout=1.0)
    assert run_order == ["first"]
    assert serializer.queue_depth == 0
    assert serializer.get_metrics()["idle_queue_stalled"] is False


@pytest.mark.asyncio
async def test_last_write_timing_records_per_call_metrics(tmp_path):
    """Phase 1 instrumentation: each awaited execute() must leave its timing
    on the calling Task's contextvar so the ingest router can emit headers.
    """
    from zerg.services.write_serializer import last_write_timing

    db_path = tmp_path / "write-serializer-last-timing.db"
    engine = make_engine(f"sqlite:///{db_path}")
    session_factory = make_sessionmaker(engine)

    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE writes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL)")

    serializer = WriteSerializer()
    serializer.configure(session_factory)

    def _write(db):
        time.sleep(0.01)
        db.execute(sa_text("INSERT INTO writes(label) VALUES ('only')"))

    assert last_write_timing() is None
    await serializer.execute(_write, label="ingest-replay")

    timing = last_write_timing()
    assert timing is not None
    assert timing.label == "ingest-replay"
    assert timing.exec_ms >= 10.0
    assert timing.queue_wait_ms >= 0.0


@pytest.mark.asyncio
async def test_last_write_timing_isolated_between_tasks(tmp_path):
    """ContextVar must NOT leak across concurrent tasks."""
    from zerg.services.write_serializer import last_write_timing

    db_path = tmp_path / "write-serializer-task-isolation.db"
    engine = make_engine(f"sqlite:///{db_path}")
    session_factory = make_sessionmaker(engine)

    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE writes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL)")

    serializer = WriteSerializer()
    serializer.configure(session_factory)

    def _write(label: str):
        def _do(db):
            db.execute(sa_text("INSERT INTO writes(label) VALUES (:l)"), {"l": label})

        return _do

    async def _run(label: str) -> str | None:
        await serializer.execute(_write(label), label=label)
        t = last_write_timing()
        return t.label if t else None

    labels = await asyncio.gather(_run("ingest-live"), _run("ingest-replay"), _run("presence"))
    assert sorted(labels) == ["ingest-live", "ingest-replay", "presence"]


@pytest.mark.asyncio
async def test_get_metrics_includes_rolling_per_label_percentiles(tmp_path):
    """Phase 1 instrumentation: get_metrics() must surface rolling p50/p95/p99
    timings per label so /api/health can drive the engine's adaptive
    controller in phase 2."""
    db_path = tmp_path / "write-serializer-rolling.db"
    engine = make_engine(f"sqlite:///{db_path}")
    session_factory = make_sessionmaker(engine)

    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE writes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL)")

    serializer = WriteSerializer()
    serializer.configure(session_factory)

    def _write(label: str):
        def _do(db):
            db.execute(sa_text("INSERT INTO writes(label) VALUES (:l)"), {"l": label})

        return _do

    for _ in range(5):
        await serializer.execute(_write("ingest-live"), label="ingest-live")
        await serializer.execute(_write("ingest-replay"), label="ingest-replay")

    metrics = serializer.get_metrics()
    assert "rolling_window" in metrics
    assert metrics["rolling_window"] >= 1
    rolling = metrics["rolling_by_label"]
    assert "ingest-live" in rolling
    assert "ingest-replay" in rolling
    for key in ("ingest-live", "ingest-replay"):
        for axis in ("queue_wait_ms", "exec_ms"):
            stats = rolling[key][axis]
            assert stats["n"] == 5
            assert {"p50", "p95", "p99"} <= set(stats.keys())
            # Sample size 5 is small but percentiles should be monotonic
            assert stats["p50"] <= stats["p95"] <= stats["p99"]


def test_get_wal_bytes_returns_int_or_none(tmp_path, monkeypatch):
    """get_wal_bytes() reports current SQLite WAL file size for /api/health."""
    db_path = tmp_path / "wal-probe.db"
    engine = make_engine(f"sqlite:///{db_path}")

    # Force WAL mode on this engine and write something so a WAL file exists.
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        conn.exec_driver_sql("CREATE TABLE writes (id INTEGER PRIMARY KEY)")
        conn.exec_driver_sql("INSERT INTO writes DEFAULT VALUES")

    # The helper points at the *default* engine. Patch its url.database to
    # this temp DB so the helper resolves the correct WAL path.
    import zerg.database as database_mod

    original_engine = database_mod.default_engine
    monkeypatch.setattr(database_mod, "default_engine", engine)
    try:
        wal_bytes = database_mod.get_wal_bytes()
        assert wal_bytes is not None
        assert isinstance(wal_bytes, int)
        assert wal_bytes >= 0
    finally:
        monkeypatch.setattr(database_mod, "default_engine", original_engine)
