from __future__ import annotations

import json
from datetime import datetime
from datetime import timezone
from uuid import uuid4

from typer.testing import CliRunner

from zerg.cli.main import app
from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentSourceLine
from zerg.models.agents import ArchiveChunk
from zerg.services.raw_json_compression import CODEC_ZSTD
from zerg.services.raw_json_compression import compress_raw_json


def test_archive_export_legacy_cli_exports_one_batch(tmp_path):
    db_path = tmp_path / "longhouse.db"
    archive_root = tmp_path / "archive"
    session_id = uuid4()
    _seed_source_line(db_path, session_id=session_id)

    result = CliRunner().invoke(
        app,
        [
            "archive",
            "export-legacy",
            "--database-url",
            f"sqlite:///{db_path}",
            "--archive-root",
            str(archive_root),
            "--tenant-id",
            "tenant-a",
            "--session-id",
            str(session_id),
            "--source-table",
            "source_lines",
            "--disk-floor",
            "1b",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["selected_rows"] == 1
    assert payload["rows_exported"] == 1
    assert payload["chunks_written"] == 1

    SessionLocal = _session_factory(db_path)
    with SessionLocal() as db:
        assert db.query(ArchiveChunk).filter(ArchiveChunk.session_id == session_id).count() == 1


def test_archive_export_legacy_cli_requires_disk_floor(tmp_path):
    db_path = tmp_path / "longhouse.db"
    session_id = uuid4()
    _seed_source_line(db_path, session_id=session_id)

    result = CliRunner().invoke(
        app,
        [
            "archive",
            "export-legacy",
            "--database-url",
            f"sqlite:///{db_path}",
            "--archive-root",
            str(tmp_path / "archive"),
            "--tenant-id",
            "tenant-a",
            "--session-id",
            str(session_id),
        ],
    )

    assert result.exit_code != 0
    assert "--disk-floor" in result.output


def _session_factory(db_path):
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _ts() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def _seed_source_line(db_path, *, session_id) -> None:
    SessionLocal = _session_factory(db_path)
    with SessionLocal() as db:
        db.add(
            AgentSession(
                id=session_id,
                provider="claude",
                environment="test",
                project="longhouse",
                device_id="device-1",
                cwd="/tmp/longhouse",
                started_at=_ts(),
                last_activity_at=_ts(),
            )
        )
        db.add(
            AgentSourceLine(
                session_id=session_id,
                thread_id=None,
                source_path="/tmp/session.jsonl",
                source_offset=0,
                branch_id=1,
                revision=1,
                is_branch_copy=0,
                raw_json="",
                raw_json_z=compress_raw_json('{"line":1}'),
                raw_json_codec=CODEC_ZSTD,
                line_hash="hash-0",
            )
        )
        db.commit()
