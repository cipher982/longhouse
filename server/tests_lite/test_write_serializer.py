from __future__ import annotations

import asyncio
import threading
import time

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
