from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from uuid import UUID

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("TESTING", "1")

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.browser_auth import get_current_browser_user
from zerg.main import api_app
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentSourceLine
from zerg.models.agents import SessionThread
from zerg.models.agents import SessionThreadAlias
from zerg.services.agents.kernel_backfill import backfill_subagent_child_threads
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest

PARENT_ID = UUID("f6a553e2-8aca-49c4-9823-3b3d8690fd2e")
CHILD_ID = UUID("ddb1a69b-628e-5677-bba7-3fb76ba6ffc2")
CODEX_PARENT_ID = UUID("019dd708-573a-7131-a4d9-9ee855520483")
CODEX_CHILD_ID = UUID("019ddb6e-114f-7643-89db-86c31a2aa706")
NOW = datetime(2026, 6, 2, 0, 19, 31, tzinfo=timezone.utc)


def _session_factory(tmp_path, name="subagent-thread-ingest.db"):
    engine = make_engine(f"sqlite:///{tmp_path / name}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _root_payload(
    *,
    session_id: UUID = PARENT_ID,
    provider: str = "claude",
    provider_session_id: str | None = None,
    project: str = "cipher982",
) -> SessionIngest:
    provider_session_id = provider_session_id or str(session_id)
    return SessionIngest(
        id=session_id,
        provider=provider,
        environment="production",
        project=project,
        device_id="cinder",
        cwd="/Users/davidrose/git/cipher982",
        git_branch="main",
        started_at=NOW,
        provider_session_id=provider_session_id,
        events=[
            EventIngest(
                role="user",
                content_text="Profile README redesign",
                timestamp=NOW,
                source_path=f"/Users/davidrose/.claude/projects/project/{session_id}.jsonl",
                source_offset=0,
                raw_json='{"type":"user","uuid":"root-u1","message":{"content":"Profile README redesign"}}',
            )
        ],
    )


def _claude_child_payload(
    *,
    child_id: UUID = CHILD_ID,
    parent_id: UUID = PARENT_ID,
    source_path: str | None = None,
) -> SessionIngest:
    source_path = source_path or (
        "/Users/davidrose/.claude/projects/-Users-davidrose-git-cipher982/"
        f"{parent_id}/subagents/agent-a0325d64b2dc7300f.jsonl"
    )
    return SessionIngest(
        id=child_id,
        provider="claude",
        environment="production",
        project="cipher982",
        device_id="cinder",
        cwd="/Users/davidrose/git/cipher982",
        git_branch="main",
        started_at=NOW,
        provider_session_id=str(child_id),
        is_sidechain=True,
        parent_provider_session_id=str(parent_id),
        subagent_id="a0325d64b2dc7300f",
        subagent_prompt_id="be1331ba-91c3-4670-a113-7f1c63773df8",
        events=[
            EventIngest(
                role="user",
                content_text="Deploy crims on drose.io",
                timestamp=NOW,
                source_path=source_path,
                source_offset=0,
                raw_json=(
                    f'{{"type":"user","uuid":"child-{child_id}","isSidechain":true,'
                    f'"sessionId":"{parent_id}","agentId":"a0325d64b2dc7300f",'
                    '"promptId":"be1331ba-91c3-4670-a113-7f1c63773df8",'
                    '"message":{"content":"Deploy crims on drose.io"}}'
                ),
            )
        ],
    )


def _thread_alias_values(db, thread_id):
    return {
        (row.alias_kind, row.alias_value)
        for row in db.query(SessionThreadAlias).filter(SessionThreadAlias.thread_id == thread_id).all()
    }


def test_claude_child_ingest_creates_child_thread_not_session(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(_root_payload())
        result = store.ingest_session(_claude_child_payload())

        assert result.session_id == PARENT_ID
        assert db.query(AgentSession).count() == 1

        root_thread = (
            db.query(SessionThread).filter(SessionThread.session_id == PARENT_ID, SessionThread.is_primary == 1).one()
        )
        child_thread = (
            db.query(SessionThread)
            .filter(SessionThread.session_id == PARENT_ID, SessionThread.branch_kind == "subagent")
            .filter(SessionThread.is_primary == 0)
            .one()
        )
        assert child_thread.parent_thread_id == root_thread.id

        child_event = db.query(AgentEvent).filter(AgentEvent.content_text == "Deploy crims on drose.io").one()
        assert child_event.session_id == PARENT_ID
        assert child_event.thread_id == child_thread.id

        child_source_line = db.query(AgentSourceLine).filter(AgentSourceLine.source_path.like("%/subagents/%")).one()
        assert child_source_line.session_id == PARENT_ID
        assert child_source_line.thread_id == child_thread.id

        aliases = _thread_alias_values(db, child_thread.id)
        assert ("longhouse_session_id", str(CHILD_ID)) in aliases
        assert ("provider_session_id", str(CHILD_ID)) in aliases
        assert ("claude_agent_id", "a0325d64b2dc7300f") in aliases
        assert ("forked_from_provider_session_id", str(PARENT_ID)) in aliases


def test_replaying_same_claude_child_reuses_child_thread(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(_root_payload())
        first = store.ingest_session(_claude_child_payload())
        second = store.ingest_session(_claude_child_payload())

        assert first.session_id == PARENT_ID
        assert second.session_id == PARENT_ID
        assert db.query(AgentSession).count() == 1
        assert db.query(SessionThread).filter(SessionThread.branch_kind == "subagent").count() == 1
        assert db.query(AgentEvent).filter(AgentEvent.content_text == "Deploy crims on drose.io").count() == 1


def test_codex_fork_child_attaches_by_parent_provider_alias(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(
            _root_payload(
                session_id=CODEX_PARENT_ID,
                provider="codex",
                provider_session_id="codex-native-parent",
                project="zerg",
            )
        )
        result = store.ingest_session(
            SessionIngest(
                id=CODEX_CHILD_ID,
                provider="codex",
                environment="production",
                project="zerg",
                device_id="cinder",
                cwd="/Users/davidrose/git/zerg/longhouse",
                started_at=NOW,
                provider_session_id=str(CODEX_CHILD_ID),
                is_sidechain=True,
                parent_provider_session_id="codex-native-parent",
                events=[
                    EventIngest(
                        role="user",
                        content_text="codex child work",
                        timestamp=NOW,
                        source_path="/Users/davidrose/.codex/sessions/child.jsonl",
                        source_offset=0,
                        raw_json='{"type":"response_item","payload":{"type":"message","role":"user"}}',
                    )
                ],
            )
        )

        assert result.session_id == CODEX_PARENT_ID
        assert db.query(AgentSession).count() == 1
        child_thread = db.query(SessionThread).filter(SessionThread.branch_kind == "subagent").one()
        assert child_thread.session_id == CODEX_PARENT_ID
        child_event = db.query(AgentEvent).filter(AgentEvent.content_text == "codex child work").one()
        assert child_event.thread_id == child_thread.id


def test_unresolved_child_file_is_hidden_from_default_timeline(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        result = store.ingest_session(_claude_child_payload(parent_id=UUID("aaaaaaaa-0000-0000-0000-000000000001")))

        assert result.session_id == CHILD_ID
        primary = (
            db.query(SessionThread).filter(SessionThread.session_id == CHILD_ID, SessionThread.is_primary == 1).one()
        )
        assert primary.branch_kind == "subagent"
        assert primary.parent_thread_id is None

        total, rows = store.list_timeline_thread_page(hide_autonomous=True, include_test=True)
        assert total == 0
        assert rows == ()

        raw_total, raw_rows = store.list_timeline_thread_page(hide_autonomous=False, include_test=True)
        assert raw_total == 1
        assert raw_rows[0][1] == str(CHILD_ID)


def test_env_style_sidechain_without_child_path_remains_timeline_visible(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        sidechain_id = UUID("bbbbbbbb-0000-0000-0000-000000000001")
        result = store.ingest_session(
            SessionIngest(
                id=sidechain_id,
                provider="claude",
                environment="production",
                project="zerg",
                device_id="cinder",
                started_at=NOW,
                is_sidechain=True,
                events=[
                    EventIngest(
                        role="user",
                        content_text="root marked sidechain by environment",
                        timestamp=NOW,
                        source_path="/tmp/root.jsonl",
                        source_offset=0,
                    )
                ],
            )
        )

        assert result.session_id == sidechain_id
        primary = (
            db.query(SessionThread)
            .filter(SessionThread.session_id == sidechain_id, SessionThread.is_primary == 1)
            .one()
        )
        assert primary.branch_kind == "root"
        total, rows = store.list_timeline_thread_page(hide_autonomous=True, include_test=True)
        assert total == 1
        assert rows[0][1] == str(sidechain_id)


def test_backfill_moves_existing_leaked_child_session_under_parent(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(_root_payload())

        leaked_payload = _claude_child_payload().model_copy(update={"parent_provider_session_id": None})
        leaked = store.ingest_session(leaked_payload)
        assert leaked.session_id == CHILD_ID
        assert db.query(AgentSession).count() == 2

        report = backfill_subagent_child_threads(db)

        assert report["candidates_resolved"] == 1
        assert report["sessions_removed"] == 1
        assert db.query(AgentSession).count() == 1
        assert db.query(AgentSession).filter(AgentSession.id == CHILD_ID).first() is None

        child_thread = db.query(SessionThread).filter(SessionThread.branch_kind == "subagent").one()
        assert child_thread.session_id == PARENT_ID
        child_event = db.query(AgentEvent).filter(AgentEvent.content_text == "Deploy crims on drose.io").one()
        assert child_event.session_id == PARENT_ID
        assert child_event.thread_id == child_thread.id
        child_source_line = db.query(AgentSourceLine).filter(AgentSourceLine.source_path.like("%/subagents/%")).one()
        assert child_source_line.session_id == PARENT_ID
        assert child_source_line.thread_id == child_thread.id

        second_report = backfill_subagent_child_threads(db)
        assert second_report["candidates_resolved"] == 0


def test_timeline_sessions_api_collapses_parent_with_children(tmp_path):
    db_path = tmp_path / "subagent-api.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    factory = make_sessionmaker(engine)

    with factory() as db:
        store = AgentsStore(db)
        store.ingest_session(_root_payload())
        for idx in range(3):
            child_id = UUID(f"ddb1a69b-628e-5677-bba7-3fb76ba6ffc{idx}")
            store.ingest_session(
                _claude_child_payload(
                    child_id=child_id,
                    source_path=(
                        "/Users/davidrose/.claude/projects/-Users-davidrose-git-cipher982/"
                        f"{PARENT_ID}/subagents/agent-{idx}.jsonl"
                    ),
                )
            )
        db.commit()

    def override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[get_current_browser_user] = lambda: SimpleNamespace(id=1, email="david010@gmail.com")
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    try:
        client = TestClient(api_app)
        response = client.get("/timeline/sessions?project=cipher982&limit=20&days_back=90")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == 1
        assert len(body["sessions"]) == 1
        assert body["sessions"][0]["head"]["id"] == str(PARENT_ID)
    finally:
        api_app.dependency_overrides.clear()
