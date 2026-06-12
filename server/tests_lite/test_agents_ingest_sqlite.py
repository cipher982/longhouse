import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from uuid import uuid4

from cryptography.fernet import Fernet
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import Base
from zerg.database import make_engine
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionPauseRequest
from zerg.services.agents import AgentsStore
from zerg.services.agents import EventIngest
from zerg.services.agents import SessionIngest
from zerg.services.agents.kernel_writes import ensure_primary_thread
from zerg.services.agents.kernel_writes import record_connection
from zerg.services.agents.kernel_writes import record_run
from zerg.services.session_pause_requests import load_active_pause_request_map
from zerg.services.session_pause_requests import serialize_pause_request_projection
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import ingest_runtime_events
from zerg.services.session_runtime import runtime_key_for_session
from zerg.services.session_workspace import build_session_mobile_tail


def test_agents_ingest_sqlite(tmp_path):
    db_path = tmp_path / "ingest.db"
    engine = make_engine(f"sqlite:///{db_path}")
    # Strip schema for SQLite (models use schema="agents" for Postgres)
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)

    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        store = AgentsStore(db)
        result = store.ingest_session(
            SessionIngest(
                provider="codex",
                environment="test",
                project="zerg",
                device_id="dev-machine",
                cwd="/tmp",
                git_repo=None,
                git_branch=None,
                started_at=datetime(2026, 1, 31, tzinfo=timezone.utc),
                events=[
                    EventIngest(
                        role="user",
                        content_text="hello",
                        timestamp=datetime(2026, 1, 31, tzinfo=timezone.utc),
                        source_path="/tmp/session.jsonl",
                        source_offset=0,
                    )
                ],
            )
        )

        assert result.events_inserted == 1
        assert result.events_skipped == 0


def test_unmanaged_claude_ask_user_question_transcript_creates_read_only_pause_request(tmp_path):
    db_path = tmp_path / "claude_pause_ingest.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)

    SessionLocal = sessionmaker(bind=engine)
    now = datetime(2026, 6, 9, 19, 14, 31, tzinfo=timezone.utc)
    with SessionLocal() as db:
        store = AgentsStore(db)
        result = store.ingest_session(
            SessionIngest(
                provider="claude",
                environment="test",
                project="g55",
                device_id="cinder",
                cwd="/Users/davidrose/git/g55",
                started_at=datetime(2026, 6, 9, 19, 0, tzinfo=timezone.utc),
                events=[
                    EventIngest(
                        role="assistant",
                        tool_name="AskUserQuestion",
                        tool_call_id="toolu_bdrk_question_1",
                        tool_input_json={
                            "questions": [
                                {
                                    "id": "image_scope",
                                    "header": "Image scope",
                                    "question": "How should I run the full image download?",
                                    "options": [
                                        {
                                            "label": "ibsrv first, then external",
                                            "description": "Download MBWorld-hosted images first.",
                                        },
                                        {
                                            "label": "Both back-to-back",
                                            "description": "Queue both image sets in one run.",
                                        },
                                    ],
                                }
                            ]
                        },
                        timestamp=now,
                        source_path="/Users/davidrose/.claude/projects/g55/session.jsonl",
                        source_offset=601,
                    )
                ],
            )
        )

        pause = db.query(SessionPauseRequest).filter(SessionPauseRequest.session_id == result.session_id).one()
        projection = serialize_pause_request_projection(pause)

        assert pause.provider == "claude"
        assert pause.status == "pending"
        assert pause.tool_name == "AskUserQuestion"
        assert pause.can_respond is False
        assert projection is not None
        assert projection["title"] == "Image scope"
        assert projection["summary"] == "Waiting for your answer."
        assert projection["questions"][0]["question"] == "How should I run the full image download?"
        assert projection["questions"][0]["options"][0]["label"] == "ibsrv first, then external"


def test_managed_claude_ask_user_question_transcript_creates_answerable_pause_request(tmp_path):
    db_path = tmp_path / "managed_claude_pause_ingest.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)

    SessionLocal = sessionmaker(bind=engine)
    now = datetime(2026, 6, 9, 19, 14, 31, tzinfo=timezone.utc)
    session_id = uuid4()
    with SessionLocal() as db:
        session = AgentSession(
            id=session_id,
            provider="claude",
            environment="cinder",
            project="g55",
            device_id="cinder",
            cwd="/Users/davidrose/git/g55",
            started_at=datetime(2026, 6, 9, 19, 0, tzinfo=timezone.utc),
        )
        db.add(session)
        db.flush()
        thread = ensure_primary_thread(db, session)
        run = record_run(
            db,
            thread=thread,
            provider="claude",
            host_id="cinder",
            cwd="/Users/davidrose/git/g55",
            started_at=datetime(2026, 6, 9, 19, 0, tzinfo=timezone.utc),
        )
        record_connection(
            db,
            run=run,
            control_plane="claude_channel_bridge",
            acquisition_kind="spawned_control",
            state="attached",
            device_id="cinder",
            can_send_input=1,
            can_interrupt=1,
            can_tail_output=1,
            can_resume=1,
        )
        db.commit()

        store = AgentsStore(db)
        result = store.ingest_session(
            SessionIngest(
                id=session_id,
                provider="claude",
                environment="test",
                project="g55",
                device_id="cinder",
                cwd="/Users/davidrose/git/g55",
                started_at=datetime(2026, 6, 9, 19, 0, tzinfo=timezone.utc),
                events=[
                    EventIngest(
                        role="assistant",
                        tool_name="AskUserQuestion",
                        tool_call_id="toolu_bdrk_question_1",
                        tool_input_json={
                            "questions": [
                                {
                                    "id": "success",
                                    "header": "Success metric",
                                    "question": "What should the plan optimize for?",
                                    "options": [
                                        {"label": "Real users + feedback", "value": "real_users"},
                                        {"label": "GitHub stars / OSS", "value": "stars"},
                                    ],
                                }
                            ]
                        },
                        timestamp=now,
                        source_path="/Users/davidrose/.claude/projects/g55/session.jsonl",
                        source_offset=601,
                    )
                ],
            )
        )

        pause = db.query(SessionPauseRequest).filter(SessionPauseRequest.session_id == result.session_id).one()
        projection = serialize_pause_request_projection(pause)

        assert pause.can_respond is True
        assert projection is not None
        assert projection["can_respond"] is True
        assert projection["questions"][0]["id"] == "success"


def test_claude_hook_then_transcript_mobile_tail_exposes_structured_question(tmp_path):
    db_path = tmp_path / "claude_hook_transcript_tail.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)

    SessionLocal = sessionmaker(bind=engine)
    started_at = datetime(2026, 6, 9, 19, 0, tzinfo=timezone.utc)
    hook_at = datetime(2026, 6, 9, 19, 14, 30, tzinfo=timezone.utc)
    asked_at = hook_at + timedelta(seconds=1)
    session_id = uuid4()
    with SessionLocal() as db:
        session = AgentSession(
            id=session_id,
            provider="claude",
            environment="cinder",
            project="spacex",
            device_id="cinder",
            cwd="/Users/davidrose/git/spacex",
            started_at=started_at,
        )
        db.add(session)
        db.flush()
        thread = ensure_primary_thread(db, session)
        run = record_run(
            db,
            thread=thread,
            provider="claude",
            host_id="cinder",
            cwd="/Users/davidrose/git/spacex",
            started_at=started_at,
        )
        record_connection(
            db,
            run=run,
            control_plane="claude_channel_bridge",
            acquisition_kind="spawned_control",
            state="attached",
            device_id="cinder",
            can_send_input=1,
            can_interrupt=1,
            can_tail_output=1,
            can_resume=1,
        )
        db.commit()

        runtime_key = runtime_key_for_session("claude", str(session_id))
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session_id,
                    provider="claude",
                    device_id="cinder",
                    source="claude_hook",
                    kind="phase_signal",
                    phase="blocked",
                    tool_name="AskUserQuestion",
                    occurred_at=hook_at,
                    freshness_ms=10 * 60 * 1000,
                    dedupe_key="claude-hook-ask-user-question",
                    payload={},
                )
            ],
        )
        db.commit()

        assert load_active_pause_request_map(db, [session_id]) == {}
        hook_tail = build_session_mobile_tail(db=db, session_id=session_id, limit=50)
        assert hook_tail.session.runtime_display.pause_request is None
        assert hook_tail.workspace_revision.pause_request_count == 0

        structured_questions = [
            {
                "id": "scope",
                "header": "Scope",
                "question": "What's the scope of the system to build now?",
                "options": [
                    {
                        "label": "Full auth-broker login orchestrator",
                        "description": "Build reusable iMessage-code retrieval and challenge orchestration.",
                        "value": "full_orchestrator",
                    },
                    {
                        "label": "iMessage code reader first",
                        "description": "Build the SMS-code retrieval primitive before wiring orchestration.",
                        "value": "code_reader",
                    },
                    {
                        "label": "Prove trust-cookie durability first",
                        "description": "Measure whether trusted-device cookies survive close and time.",
                        "value": "cookie_durability",
                    },
                ],
            }
        ]
        AgentsStore(db).ingest_session(
            SessionIngest(
                id=session_id,
                provider="claude",
                environment="cinder",
                project="spacex",
                device_id="cinder",
                cwd="/Users/davidrose/git/spacex",
                started_at=started_at,
                events=[
                    EventIngest(
                        role="assistant",
                        content_text="Before I build, two scoping decisions:",
                        tool_name="AskUserQuestion",
                        tool_call_id="toolu_scope_question",
                        tool_input_json={"questions": structured_questions},
                        timestamp=asked_at,
                        source_path="/Users/davidrose/.claude/projects/spacex/session.jsonl",
                        source_offset=601,
                    )
                ],
            )
        )

        rows = db.query(SessionPauseRequest).filter(SessionPauseRequest.session_id == session_id).all()
        assert len(rows) == 1
        pause = load_active_pause_request_map(db, [session_id])[session_id]
        projection = serialize_pause_request_projection(pause)
        assert projection is not None
        assert projection["can_respond"] is True
        assert projection["title"] == "Scope"
        assert projection["questions"][0]["id"] == "scope"
        assert projection["questions"][0]["options"][0]["label"] == "Full auth-broker login orchestrator"
        assert projection["questions"][0]["id"] != "terminal_answer"

        tail = build_session_mobile_tail(db=db, session_id=session_id, limit=50)
        tail_pause = tail.session.runtime_display.pause_request
        assert tail_pause is not None
        assert tail_pause.can_respond is True
        assert tail_pause.title == "Scope"
        assert tail_pause.questions[0].id == "scope"
        assert tail_pause.questions[0].options[1].label == "iMessage code reader first"
        assert tail.workspace_revision.pause_request_count == 1

        ask_event = next(item.event for item in tail.projection.items if item.event and item.event.tool_name == "AskUserQuestion")
        assert ask_event.content_text == "Before I build, two scoping decisions:"
        assert ask_event.tool_input_json["questions"][0]["options"][2]["value"] == "cookie_durability"


def test_claude_ask_user_question_tool_result_resolves_transcript_pause_request(tmp_path):
    db_path = tmp_path / "claude_pause_resolve.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)

    SessionLocal = sessionmaker(bind=engine)
    started_at = datetime(2026, 6, 9, 19, 0, tzinfo=timezone.utc)
    asked_at = datetime(2026, 6, 9, 19, 14, 31, tzinfo=timezone.utc)
    answered_at = datetime(2026, 6, 9, 19, 15, 2, tzinfo=timezone.utc)
    with SessionLocal() as db:
        store = AgentsStore(db)
        first = store.ingest_session(
            SessionIngest(
                provider="claude",
                environment="test",
                project="g55",
                device_id="cinder",
                cwd="/Users/davidrose/git/g55",
                started_at=started_at,
                events=[
                    EventIngest(
                        role="assistant",
                        tool_name="AskUserQuestion",
                        tool_call_id="toolu_bdrk_question_1",
                        tool_input_json={"question": "How should I run it?", "choices": ["ibsrv only", "both"]},
                        timestamp=asked_at,
                        source_path="/tmp/session.jsonl",
                        source_offset=100,
                    )
                ],
            )
        )

        store.ingest_session(
            SessionIngest(
                id=first.session_id,
                provider="claude",
                environment="test",
                project="g55",
                device_id="cinder",
                cwd="/Users/davidrose/git/g55",
                started_at=started_at,
                events=[
                    EventIngest(
                        role="tool",
                        content_text="User chose ibsrv only.",
                        tool_call_id="toolu_bdrk_question_1",
                        timestamp=answered_at,
                        source_path="/tmp/session.jsonl",
                        source_offset=200,
                    )
                ],
            )
        )

        pause = db.query(SessionPauseRequest).filter(SessionPauseRequest.session_id == first.session_id).one()
        projection = serialize_pause_request_projection(pause)

        assert pause.status == "resolved"
        assert pause.resolved_at is not None
        assert pause.response_text == "User chose ibsrv only."
        assert projection is not None
        assert projection["questions"][0]["options"] == [
            {"label": "ibsrv only", "description": None, "value": "ibsrv only"},
            {"label": "both", "description": None, "value": "both"},
        ]
