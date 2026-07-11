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
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession


def test_archive_backfill_previews_cli_updates_legacy_rows(tmp_path):
    db_path = tmp_path / "longhouse.db"
    session_id = uuid4()
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
            AgentEvent(
                session_id=session_id,
                role="user",
                content_text="legacy preview from cli",
                timestamp=_ts(),
            )
        )
        db.commit()

    result = CliRunner().invoke(
        app,
        [
            "archive",
            "backfill-previews",
            "--database-url",
            f"sqlite:///{db_path}",
            "--limit",
            "10",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["selected_sessions"] == 1
    assert payload["updated_sessions"] == 1
    assert payload["first_user_filled"] == 1

    with SessionLocal() as db:
        session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        assert session.first_user_message_preview == "legacy preview from cli"


def _session_factory(db_path):
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _ts() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)
