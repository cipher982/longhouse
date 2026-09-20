import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from uuid import uuid4

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.database import Base
from zerg.database import initialize_live_database
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionPauseRequest
from zerg.models.live_store import LiveInteractionRequest
from zerg.models.live_store import LiveRuntimeState
from zerg.services.session_pause_requests import PAUSE_KIND_PERMISSION_PROMPT
from zerg.services.session_pause_requests import PAUSE_KIND_PLAN_APPROVAL
from zerg.services.session_pause_requests import PAUSE_KIND_STRUCTURED_QUESTION
from zerg.services.session_pause_requests import load_active_pause_request_map
from zerg.services.session_pause_requests import pending_interaction_from_live_runtime
from zerg.services.session_pause_requests import serialize_pause_request_projection
from zerg.services.session_pause_requests import upsert_pause_request
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import ingest_runtime_events
from zerg.utils.time import normalize_utc


def _make_db(tmp_path, name="session_pause_requests.db"):
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_live_database(engine)
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


def test_runtime_plan_approval_event_projects_user_facing_approval_question(tmp_path):
    factory = _make_db(tmp_path, "plan_approval_pause.db")
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
                    kind="pause_request",
                    occurred_at=now,
                    dedupe_key="plan-approval-1",
                    payload={
                        "kind": PAUSE_KIND_PLAN_APPROVAL,
                        "provider_request_id": "plan-1",
                        "request_key": f"codex:{session.id}:plan-1",
                        "can_respond": True,
                        "title": "Plan approval required",
                        "summary": "The agent needs approval before it can continue with this plan.",
                        "plan": "1. Inspect the API. 2. Patch the handler. 3. Run tests.",
                    },
                )
            ],
        )
        db.commit()

        row = db.query(LiveInteractionRequest).filter(LiveInteractionRequest.runtime_key == runtime_key).one()
        projection = dict(row.projection_json or {})

        assert row.kind == PAUSE_KIND_PLAN_APPROVAL
        assert projection is not None
        assert projection["kind"] == PAUSE_KIND_PLAN_APPROVAL
        assert projection["title"] == "Plan approval required"
        assert projection["can_respond"] is True
        assert projection["questions"] == [
            {
                "id": "approval",
                "header": None,
                "question": "1. Inspect the API. 2. Patch the handler. 3. Run tests.",
                "multi_select": False,
                "options": [
                    {"label": "Approve", "description": None, "value": "approve"},
                    {"label": "Reject", "description": None, "value": "reject"},
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

        state = db.query(LiveRuntimeState).filter(LiveRuntimeState.runtime_key == runtime_key).one()
        assert state.phase == "needs_user"
        pause = db.query(LiveInteractionRequest).filter(LiveInteractionRequest.runtime_key == runtime_key).one()
        assert pause.status == "pending"
        assert pause.projection_json["title"] == "Choose approach"

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

        # A phase signal does not resolve a held interaction; only an explicit
        # canonical pause_resolution event can close it.
        assert pause.status == "pending"
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="codex",
                    device_id="cinder",
                    source="codex_bridge",
                    kind="pause_resolution",
                    occurred_at=now + timedelta(seconds=11),
                    dedupe_key="pause-resolution",
                    payload={"request_key": pause.request_key, "status": "resolved", "response_text": "A"},
                )
            ],
        )
        db.commit()
        db.refresh(pause)
        assert pause.status == "resolved"
        assert normalize_utc(pause.resolved_at) == now + timedelta(seconds=11)
    finally:
        db.close()


def test_claude_hook_pause_event_creates_detection_only_request(tmp_path):
    factory = _make_db(tmp_path, "claude_hook_pause_event.db")
    now = datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc)
    db = factory()
    try:
        session = _seed_session(db, provider="claude", started_at=now - timedelta(minutes=5))
        runtime_key = f"claude:{session.id}"
        request_key = f"claude-hook:elicitation_dialog:{session.id}"

        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="claude_hook",
                    kind="pause_request",
                    occurred_at=now,
                    dedupe_key=request_key,
                    payload={
                        "request_key": request_key,
                        "provider_request_id": "elicitation_dialog",
                        "kind": "structured_question",
                        "tool_name": "AskUserQuestion",
                        "title": "Question needed",
                        "summary": "Which direction should I take?",
                        "can_respond": False,
                        "single_active": True,
                        "request_payload": {
                            "questions": [
                                {
                                    "id": "terminal",
                                    "header": "Claude",
                                    "question": "Which direction should I take?",
                                    "options": [],
                                }
                            ]
                        },
                    },
                )
            ],
        )
        db.commit()

        pause = db.query(LiveInteractionRequest).filter(LiveInteractionRequest.runtime_key == runtime_key).one()
        assert pause.provider == "claude"
        assert pause.status == "pending"
        assert pause.provider_request_id == "elicitation_dialog"
        projection = dict(pause.projection_json or {})
        assert projection["tool_name"] == "AskUserQuestion"
        assert projection is not None
        assert projection["can_respond"] is False
        assert projection["questions"] == [
            {
                "id": "terminal",
                "header": "Claude",
                "question": "Which direction should I take?",
                "multi_select": False,
                "options": [],
            }
        ]
    finally:
        db.close()


def test_pause_event_for_unmaterialized_session_keeps_canonical_batch_truth(tmp_path):
    factory = _make_db(tmp_path, "pause_unknown_session.db")
    now = datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc)
    db = factory()
    try:
        unknown_session_id = uuid4()

        result = ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=f"claude:{unknown_session_id}",
                    session_id=unknown_session_id,
                    provider="claude",
                    device_id="cinder",
                    source="claude_hook",
                    kind="pause_request",
                    occurred_at=now,
                    dedupe_key=f"claude-hook:elicitation_dialog:{unknown_session_id}",
                    payload={
                        "request_key": f"claude-hook:elicitation_dialog:{unknown_session_id}",
                        "provider_request_id": "elicitation_dialog",
                        "kind": "structured_question",
                        "tool_name": "AskUserQuestion",
                        "summary": "Question waiting in Claude terminal",
                        "can_respond": False,
                    },
                )
            ],
        )
        db.commit()

        assert result.accepted == 1
        assert db.query(LiveInteractionRequest).count() == 1
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

        rows = (
            db.query(LiveInteractionRequest)
            .filter(LiveInteractionRequest.runtime_key == runtime_key)
            .order_by(LiveInteractionRequest.provider_request_id)
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
                    occurred_at=now,
                    dedupe_key="pause-question-preserve",
                    payload={
                        "provider_request_id": "question-1",
                        "title": "Choose approach",
                        "single_active": True,
                    },
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

        pause = db.query(LiveInteractionRequest).filter(LiveInteractionRequest.runtime_key == runtime_key).one()
        assert pause.status == "pending"
        assert pause.resolved_at is None
    finally:
        db.close()


def test_claude_hook_blocked_signal_does_not_create_question_payload(tmp_path):
    factory = _make_db(tmp_path, "pause_claude_hook_blocked.db")
    now = datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc)
    db = factory()
    try:
        session = _seed_session(db, provider="claude", started_at=now - timedelta(minutes=5))
        runtime_key = f"claude:{session.id}"

        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="claude_hook",
                    kind="phase_signal",
                    phase="blocked",
                    tool_name="AskUserQuestion",
                    occurred_at=now,
                    freshness_ms=10 * 60 * 1000,
                    dedupe_key="claude-hook-blocked-question",
                )
            ],
        )
        db.commit()

        assert load_active_pause_request_map(db, [session.id]) == {}

        state = db.query(LiveRuntimeState).filter(LiveRuntimeState.runtime_key == runtime_key).one()
        assert state.phase == "blocked"
        assert state.active_tool == "AskUserQuestion"
    finally:
        db.close()


def test_legacy_claude_hook_placeholder_is_not_user_facing(tmp_path):
    factory = _make_db(tmp_path, "pause_legacy_claude_hook_hidden.db")
    now = datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc)
    db = factory()
    try:
        session = _seed_session(db, provider="claude", started_at=now - timedelta(minutes=5))
        runtime_key = f"claude:{session.id}"
        upsert_pause_request(
            db,
            session_id=session.id,
            runtime_key=runtime_key,
            provider="claude",
            request_key=f"claude-hook:{runtime_key}:AskUserQuestion",
            provider_request_id="claude-hook-ask-user-question",
            provider_ref={"source": "claude_hook"},
            kind=PAUSE_KIND_STRUCTURED_QUESTION,
            tool_name="AskUserQuestion",
            title="Claude needs an answer",
            summary="Answer this in the original terminal.",
            request_payload={
                "questions": [
                    {
                        "id": "terminal_answer",
                        "question": "Claude is waiting for an interactive answer in the terminal.",
                    }
                ]
            },
            can_respond=False,
            occurred_at=now,
        )
        db.commit()

        assert load_active_pause_request_map(db, [session.id]) == {}
    finally:
        db.close()


def test_transcript_pause_request_is_the_only_user_facing_claude_question(tmp_path):
    factory = _make_db(tmp_path, "pause_claude_hook_superseded.db")
    now = datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc)
    db = factory()
    try:
        session = _seed_session(db, provider="claude", started_at=now - timedelta(minutes=5))
        runtime_key = f"claude:{session.id}"

        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="claude_hook",
                    kind="phase_signal",
                    phase="blocked",
                    tool_name="AskUserQuestion",
                    occurred_at=now,
                    freshness_ms=10 * 60 * 1000,
                    dedupe_key="claude-hook-blocked-question",
                ),
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="agents_ingest",
                    kind="pause_request",
                    tool_name="AskUserQuestion",
                    occurred_at=now + timedelta(seconds=1),
                    dedupe_key="claude-transcript-question",
                    payload={
                        "request_key": f"claude-transcript:{runtime_key}:toolu_1",
                        "provider_request_id": "toolu_1",
                        "kind": "structured_question",
                        "tool_name": "AskUserQuestion",
                        "title": "Success metric",
                        "summary": "Waiting for an answer in the original terminal.",
                        "request_payload": {
                            "questions": [
                                {
                                    "id": "success",
                                    "question": "What should the launch optimize for?",
                                    "options": [{"label": "Real users"}, {"label": "GitHub stars"}],
                                }
                            ]
                        },
                        "can_respond": False,
                        "single_active": True,
                    },
                ),
            ],
        )
        db.commit()

        runtime = db.query(LiveRuntimeState).filter(LiveRuntimeState.runtime_key == runtime_key).one()
        projection = pending_interaction_from_live_runtime(runtime)
        assert projection is not None
        assert projection["kind"] == PAUSE_KIND_STRUCTURED_QUESTION
        assert projection["title"] == "Success metric"
        assert projection["can_respond"] is False
    finally:
        db.close()


def test_claude_hook_blocked_signal_clears_without_question_payload(tmp_path):
    factory = _make_db(tmp_path, "pause_claude_hook_resolves.db")
    now = datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc)
    db = factory()
    try:
        session = _seed_session(db, provider="claude", started_at=now - timedelta(minutes=5))
        runtime_key = f"claude:{session.id}"

        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="claude_hook",
                    kind="phase_signal",
                    phase="blocked",
                    tool_name="AskUserQuestion",
                    occurred_at=now,
                    freshness_ms=10 * 60 * 1000,
                    dedupe_key="claude-hook-blocked-question",
                )
            ],
        )
        assert load_active_pause_request_map(db, [session.id]) == {}

        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="claude",
                    device_id="cinder",
                    source="claude_hook",
                    kind="phase_signal",
                    phase="running",
                    tool_name=None,
                    occurred_at=now + timedelta(seconds=10),
                    freshness_ms=90_000,
                    dedupe_key="claude-hook-running-after-question",
                )
            ],
        )
        db.commit()

        assert load_active_pause_request_map(db, [session.id]) == {}
        assert db.query(SessionPauseRequest).filter(SessionPauseRequest.runtime_key == runtime_key).count() == 0
    finally:
        db.close()


def test_terminal_signal_expires_pending_pause_requests(tmp_path):
    factory = _make_db(tmp_path, "pause_terminal_expiry.db")
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
                    kind="pause_request",
                    occurred_at=now,
                    dedupe_key="pause-1",
                    payload={
                        "request_key": f"codex:{session.id}:question-1",
                        "provider_request_id": "question-1",
                        "kind": PAUSE_KIND_PERMISSION_PROMPT,
                        "can_respond": True,
                        "provider_ref": {
                            "source": "codex_bridge",
                            "reply_transport": "managed_push",
                        },
                        "title": "Choose approach",
                    },
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
                    kind="terminal_signal",
                    occurred_at=now + timedelta(seconds=30),
                    dedupe_key="terminal-process-gone",
                    payload={"terminal_state": "process_gone"},
                )
            ],
        )
        db.commit()

        row = db.query(LiveInteractionRequest).filter(LiveInteractionRequest.runtime_key == runtime_key).one()
        assert row.status == "expired"
        state = db.query(LiveRuntimeState).filter(LiveRuntimeState.runtime_key == runtime_key).one()
        assert pending_interaction_from_live_runtime(state) is None
        assert state.terminal_state == "process_gone"
    finally:
        db.close()
