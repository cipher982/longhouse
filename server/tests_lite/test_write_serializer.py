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
    runtime = asyncio.create_task(serializer.execute(_make_write("runtime"), label="runtime-observations"))

    await asyncio.gather(first, presence, ingest, runtime)

    assert run_order == ["first", "runtime", "ingest", "presence"]


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

    await asyncio.gather(first, replay, scan, presence, heartbeat, runtime)

    assert run_order == ["first", "runtime", "presence", "heartbeat", "replay", "scan"]


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

    assert run_order == ["first", "live", "archive", "presence"]


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
        "server-fanout",
        "archive",
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
