import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionPauseRequest
from zerg.models.agents import SessionRuntimeState
from zerg.services.session_pause_requests import PAUSE_KIND_STRUCTURED_QUESTION
from zerg.services.session_pause_requests import load_active_pause_request_map
from zerg.services.session_pause_requests import serialize_pause_request_projection
from zerg.services.session_pause_requests import upsert_pause_request
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import ingest_runtime_events
from zerg.utils.time import normalize_utc


def _make_db(tmp_path, name="session_pause_requests.db"):
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_session(db, *, provider: str = "codex", started_at: datetime):
    session = AgentSession(
        provider=provider,
        environment="test",
        project="runtime-pause",
        started_at=started_at,
        user_messages=1,
        assistant_messages=1,
    )
    db.add(session)
    db.flush()
    db.refresh(session)
    return session


def test_upsert_pause_request_serializes_structured_questions(tmp_path):
    factory = _make_db(tmp_path)
    now = datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc)
    db = factory()
    try:
        session = _seed_session(db, started_at=now - timedelta(minutes=5))
        row, changed = upsert_pause_request(
            db,
            session_id=session.id,
            runtime_key=f"codex:{session.id}",
            provider="codex",
            request_key=f"codex:{session.id}:question-1",
            provider_request_id="question-1",
            kind=PAUSE_KIND_STRUCTURED_QUESTION,
            title="Choose storage",
            summary="The agent needs a storage decision.",
            request_payload={
                "questions": [
                    {
                        "id": "storage",
                        "header": "Storage",
                        "question": "Which storage backend should I use?",
                        "multiSelect": False,
                        "options": [
                            {"label": "SQLite", "description": "Keep it local."},
                            {"label": "Postgres", "description": "Use a service."},
                        ],
                    }
                ]
            },
            can_respond=True,
            occurred_at=now,
        )
        db.commit()

        assert changed is True
        projection = serialize_pause_request_projection(row)
        assert projection is not None
        assert projection["status"] == "pending"
        assert projection["can_respond"] is True
        assert projection["questions"] == [
            {
                "id": "storage",
                "header": "Storage",
                "question": "Which storage backend should I use?",
                "multi_select": False,
                "options": [
                    {"label": "SQLite", "description": "Keep it local.", "value": "SQLite"},
                    {"label": "Postgres", "description": "Use a service.", "value": "Postgres"},
                ],
            }
        ]
    finally:
        db.close()


def test_runtime_pause_events_create_and_resolve_without_mutating_phase(tmp_path):
    factory = _make_db(tmp_path, "pause_runtime_events.db")
    now = datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc)
    db = factory()
    try:
        session = _seed_session(db, started_at=now - timedelta(minutes=5))
        runtime_key = f"codex:{session.id}"

        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="codex",
                    device_id="cinder",
                    source="codex_bridge",
                    kind="phase_signal",
                    phase="needs_user",
                    occurred_at=now,
                    freshness_ms=10 * 60 * 1000,
                    dedupe_key="phase-needs-user",
                    payload={},
                )
            ],
        )
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="codex",
                    device_id="cinder",
                    source="codex_bridge",
                    kind="pause_request",
                    occurred_at=now + timedelta(seconds=1),
                    dedupe_key="pause-question",
                    payload={
                        "provider_request_id": "question-1",
                        "title": "Choose approach",
                        "request_payload": {"question": "Which approach?", "options": ["A", "B"]},
                    },
                )
            ],
        )
        db.commit()

        state = db.query(SessionRuntimeState).filter(SessionRuntimeState.runtime_key == runtime_key).one()
        assert state.phase == "needs_user"
        pause = db.query(SessionPauseRequest).filter(SessionPauseRequest.runtime_key == runtime_key).one()
        assert pause.status == "pending"
        assert pause.title == "Choose approach"

        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="codex",
                    device_id="cinder",
                    source="codex_bridge",
                    kind="phase_signal",
                    phase="running",
                    tool_name="Bash",
                    occurred_at=now + timedelta(seconds=10),
                    freshness_ms=10 * 60 * 1000,
                    dedupe_key="phase-running",
                    payload={},
                )
            ],
        )
        db.commit()

        db.refresh(pause)
        assert pause.status == "resolved"
        assert normalize_utc(pause.resolved_at) == now + timedelta(seconds=10)
    finally:
        db.close()


def test_pause_request_can_keep_multiple_pending_when_provider_allows_it(tmp_path):
    factory = _make_db(tmp_path, "pause_multiple_pending.db")
    now = datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc)
    db = factory()
    try:
        session = _seed_session(db, started_at=now - timedelta(minutes=5))
        runtime_key = f"codex:{session.id}"

        for idx in (1, 2):
            ingest_runtime_events(
                db,
                [
                    RuntimeEventIngest(
                        runtime_key=runtime_key,
                        session_id=session.id,
                        provider="codex",
                        device_id="cinder",
                        source="codex_bridge",
                        kind="pause_request",
                        occurred_at=now + timedelta(seconds=idx),
                        dedupe_key=f"pause-question-{idx}",
                        payload={
                            "provider_request_id": f"question-{idx}",
                            "title": f"Question {idx}",
                            "single_active": False,
                        },
                    )
                ],
            )
        db.commit()

        rows = db.query(SessionPauseRequest).filter(SessionPauseRequest.runtime_key == runtime_key).order_by(
            SessionPauseRequest.provider_request_id
        )
        assert [(row.provider_request_id, row.status) for row in rows] == [
            ("question-1", "pending"),
            ("question-2", "pending"),
        ]
    finally:
        db.close()


def test_phase_signal_can_preserve_pending_pause_request(tmp_path):
    factory = _make_db(tmp_path, "pause_phase_preserve.db")
    now = datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc)
    db = factory()
    try:
        session = _seed_session(db, started_at=now - timedelta(minutes=5))
        runtime_key = f"codex:{session.id}"
        pause, _changed = upsert_pause_request(
            db,
            session_id=session.id,
            runtime_key=runtime_key,
            provider="codex",
            request_key=f"codex:{session.id}:question-1",
            provider_request_id="question-1",
            title="Choose approach",
            occurred_at=now,
        )

        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="codex",
                    device_id="cinder",
                    source="codex_bridge",
                    kind="phase_signal",
                    phase="running",
                    tool_name="Bash",
                    occurred_at=now + timedelta(seconds=5),
                    freshness_ms=10 * 60 * 1000,
                    dedupe_key="phase-running-preserve-pause",
                    payload={"pause_request_still_pending": True},
                )
            ],
        )
        db.commit()

        db.refresh(pause)
        assert pause.status == "pending"
        assert pause.resolved_at is None
    finally:
        db.close()


def test_terminal_signal_expires_pending_pause_requests(tmp_path):
    factory = _make_db(tmp_path, "pause_terminal_expiry.db")
    now = datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc)
    db = factory()
    try:
        session = _seed_session(db, started_at=now - timedelta(minutes=5))
        runtime_key = f"codex:{session.id}"
        row, _changed = upsert_pause_request(
            db,
            session_id=session.id,
            runtime_key=runtime_key,
            provider="codex",
            request_key=f"codex:{session.id}:question-1",
            provider_request_id="question-1",
            title="Choose approach",
            occurred_at=now,
        )

        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="codex",
                    device_id="cinder",
                    source="codex_bridge",
                    kind="terminal_signal",
                    occurred_at=now + timedelta(seconds=30),
                    dedupe_key="terminal-process-gone",
                    payload={"terminal_state": "process_gone"},
                )
            ],
        )
        db.commit()

        db.refresh(row)
        assert row.status == "expired"
        assert load_active_pause_request_map(db, [session.id]) == {}
    finally:
        db.close()
