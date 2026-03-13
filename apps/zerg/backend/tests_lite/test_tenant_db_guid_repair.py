from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

from datetime import datetime
from datetime import timezone

from sqlalchemy import text

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models import User
from zerg.models.enums import UserRole
from zerg.models.models import Fiche
from zerg.models.thread import Thread
from zerg.models.enums import ThreadType
from zerg.services.tenant_db_guid_repair import find_db_paths
from zerg.services.tenant_db_guid_repair import repair_db
from zerg.services.tenant_db_guid_repair import scan_db


def _make_db(tmp_path, name: str = "tenant.db"):
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return db_path, make_sessionmaker(engine)


def _seed_run_graph(db):
    user = User(email="repair@test.local", role=UserRole.USER.value)
    db.add(user)
    db.commit()
    db.refresh(user)

    fiche = Fiche(
        name="Repair Test",
        status="idle",
        system_instructions="sys",
        task_instructions="task",
        model="gpt-scripted",
        owner_id=user.id,
    )
    db.add(fiche)
    db.commit()
    db.refresh(fiche)

    thread = Thread(fiche_id=fiche.id, title="Repair Thread", thread_type=ThreadType.CHAT.value)
    db.add(thread)
    db.commit()
    db.refresh(thread)
    return user, fiche, thread


def test_scan_and_repair_safe_guid_columns(tmp_path):
    instance_dir = tmp_path / "david010"
    instance_dir.mkdir()
    db_path, SessionLocal = _make_db(instance_dir, "longhouse.db")

    with SessionLocal() as db:
        _user, fiche, thread = _seed_run_graph(db)
        started_at = datetime(2026, 3, 6, 4, 0, 0, tzinfo=timezone.utc).replace(tzinfo=None)
        result = db.execute(
            text(
                """
                INSERT INTO runs (
                    fiche_id, thread_id, status, trigger, started_at, assistant_message_id, trace_id, model
                ) VALUES (
                    :fiche_id, :thread_id, :status, :trigger, :started_at, :assistant_message_id, :trace_id, :model
                )
                """
            ),
            {
                "fiche_id": fiche.id,
                "thread_id": thread.id,
                "status": "RUNNING",
                "trigger": "API",
                "started_at": started_at,
                "assistant_message_id": "live-voice-1772742439",
                "trace_id": "not-a-uuid",
                "model": "gpt-scripted",
            },
        )
        db.commit()
        run_id = result.lastrowid

    findings = scan_db(db_path)
    assert {(finding.table, finding.column, finding.action) for finding in findings} == {
        ("runs", "assistant_message_id", "set_null"),
        ("runs", "trace_id", "set_null"),
    }

    summary = repair_db(db_path)
    assert summary.repaired_count == 2
    assert summary.unsupported_count == 0

    with SessionLocal() as db:
        row = db.execute(
            text("SELECT assistant_message_id, trace_id FROM runs WHERE id = :run_id"),
            {"run_id": run_id},
        ).mappings().one()
    assert row["assistant_message_id"] is None
    assert row["trace_id"] is None


def test_scan_ignores_removed_legacy_memories_table(tmp_path):
    db_path, _SessionLocal = _make_db(tmp_path, "legacy_memories.db")

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE memories (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                fiche_id INTEGER,
                content TEXT NOT NULL,
                type TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO memories (id, user_id, fiche_id, content, type)
            VALUES ('not-a-real-uuid', 1, NULL, 'hello', 'note')
            """
        )
        conn.commit()

    findings = scan_db(db_path)
    assert findings == []

    summary = repair_db(db_path)
    assert summary.repaired_count == 0
    assert summary.unsupported_count == 0


def test_find_db_paths_discovers_instance_dbs(tmp_path):
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    alpha.mkdir()
    beta.mkdir()
    (alpha / "longhouse.db").write_text("")
    (beta / "longhouse.db").write_text("")

    found = find_db_paths(root=tmp_path)
    assert found == [alpha / "longhouse.db", beta / "longhouse.db"]


def test_cli_runs_without_app_env(tmp_path):
    instance_dir = tmp_path / "delta"
    instance_dir.mkdir()
    db_path, _SessionLocal = _make_db(instance_dir, "longhouse.db")

    script_path = Path(__file__).resolve().parents[4] / "scripts" / "repair-tenant-db-guids.py"
    result = subprocess.run(
        ["python3", str(script_path), "--db-path", str(db_path)],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "")},
        check=False,
    )

    assert result.returncode == 0
    assert "No malformed GUID values found." in result.stdout
    assert "Missing required environment variables" not in f"{result.stdout}\n{result.stderr}"
