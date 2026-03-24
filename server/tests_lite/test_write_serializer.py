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
