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
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.dependencies.browser_auth import get_current_browser_user
from zerg.main import api_app
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentSourceLine
from zerg.models.agents import SessionEdge
from zerg.models.agents import SessionEmbedding
from zerg.models.agents import SessionObservation
from zerg.models.agents import SessionRuntimeState
from zerg.models.agents import SessionTask
from zerg.models.agents import SessionThread
from zerg.models.agents import SessionThreadAlias
from zerg.services.agents import AgentsStore
from zerg.services.agents import EventIngest
from zerg.services.agents import SessionIngest
from zerg.services.agents.kernel_backfill import backfill_subagent_child_threads
from zerg.services.session_graph_projection import build_session_graph_projection

PARENT_ID = UUID("f6a553e2-8aca-49c4-9823-3b3d8690fd2e")
CHILD_ID = UUID("ddb1a69b-628e-5677-bba7-3fb76ba6ffc2")
CODEX_PARENT_ID = UUID("019dd708-573a-7131-a4d9-9ee855520483")
CODEX_CHILD_ID = UUID("019ddb6e-114f-7643-89db-86c31a2aa706")
OPENCODE_PARENT_ID = UUID("019ee600-0000-7000-8000-000000000001")
OPENCODE_CHILD_ID = UUID("019ee600-0000-7000-8000-000000000002")
OPENCODE_FORK_ID = UUID("019ee600-0000-7000-8000-000000000003")
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


def _edge_rows(db):
    return db.query(SessionEdge).order_by(SessionEdge.edge_kind.asc(), SessionEdge.id.asc()).all()


def test_root_ingest_without_provider_session_id_does_not_create_provider_alias(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(
            SessionIngest(
                id=PARENT_ID,
                provider="claude",
                environment="production",
                project="cipher982",
                device_id="cinder",
                cwd="/Users/davidrose/git/cipher982",
                started_at=NOW,
                provider_session_id=None,
                events=[
                    EventIngest(
                        role="user",
                        content_text="root without provider id",
                        timestamp=NOW,
                        source_path=f"/Users/davidrose/.claude/projects/project/{PARENT_ID}.jsonl",
                        source_offset=0,
                        raw_json='{"type":"user","message":{"content":"root without provider id"}}',
                    )
                ],
            )
        )

        primary = (
            db.query(SessionThread).filter(SessionThread.session_id == PARENT_ID, SessionThread.is_primary == 1).one()
        )
        aliases = _thread_alias_values(db, primary.id)
        assert ("longhouse_session_id", str(PARENT_ID)) in aliases
        assert ("provider_session_id", str(PARENT_ID)) not in aliases
        assert not [alias for alias in aliases if alias[0] == "provider_session_id"]


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

        child_runtime = db.query(SessionRuntimeState).filter(SessionRuntimeState.thread_id == child_thread.id).one()
        assert child_runtime.session_id == PARENT_ID
        assert str(child_thread.id) in child_runtime.runtime_key

        parent = db.query(AgentSession).filter(AgentSession.id == PARENT_ID).one()
        assert parent.user_messages == 1

        aliases = _thread_alias_values(db, child_thread.id)
        assert ("longhouse_session_id", str(CHILD_ID)) in aliases
        assert ("provider_session_id", str(CHILD_ID)) in aliases
        assert ("claude_agent_id", "a0325d64b2dc7300f") in aliases
        assert ("forked_from_provider_session_id", str(PARENT_ID)) in aliases

        parent_events = store.get_session_events(PARENT_ID)
        assert [event.content_text for event in parent_events] == ["Profile README redesign"]
        assert store.count_session_events(PARENT_ID) == 1

        child_events = store.get_session_events(PARENT_ID, thread_id=child_thread.id)
        assert [event.content_text for event in child_events] == ["Deploy crims on drose.io"]
        assert store.count_session_events(PARENT_ID, thread_id=child_thread.id) == 1

        projection = store.get_session_projection_page(PARENT_ID)
        assert projection.total == 1
        assert [item.event.content_text for item in projection.items if item.event is not None] == [
            "Profile README redesign"
        ]
        child_projection = store.get_session_projection_page(PARENT_ID, thread_id=child_thread.id)
        assert child_projection.total == 1
        assert [item.event.content_text for item in child_projection.items if item.event is not None] == [
            "Deploy crims on drose.io"
        ]


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


def test_opencode_task_child_attaches_by_parent_provider_alias(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(
            _root_payload(
                session_id=OPENCODE_PARENT_ID,
                provider="opencode",
                provider_session_id="ses_parent",
                project="longhouse",
            )
        )

        result = store.ingest_session(
            SessionIngest(
                id=OPENCODE_CHILD_ID,
                provider="opencode",
                environment="production",
                project="longhouse",
                device_id="cinder",
                cwd="/Users/davidrose/git/zerg/longhouse",
                started_at=NOW,
                provider_session_id="ses_child",
                is_sidechain=True,
                parent_provider_session_id="ses_parent",
                subagent_id="explore",
                subagent_tool_use_id="call_task",
                attribution_agent="explore",
                events=[
                    EventIngest(
                        role="user",
                        content_text="opencode child work",
                        timestamp=NOW,
                        source_path="/Users/davidrose/.local/share/opencode/opencode.db#opencode:ses_child",
                        source_offset=0,
                        raw_json='{"provider":"opencode","session_id":"ses_child"}',
                    )
                ],
            )
        )

        assert result.session_id == OPENCODE_PARENT_ID
        assert db.query(AgentSession).count() == 1
        child_thread = db.query(SessionThread).filter(SessionThread.branch_kind == "subagent").one()
        assert child_thread.session_id == OPENCODE_PARENT_ID
        aliases = _thread_alias_values(db, child_thread.id)
        assert ("provider_session_id", "ses_child") in aliases
        assert ("subagent_id", "explore") in aliases
        assert ("subagent_tool_use_id", "call_task") in aliases
        assert ("forked_from_provider_session_id", "ses_parent") in aliases
        assert ("claude_agent_id", "explore") not in aliases

        child_event = db.query(AgentEvent).filter(AgentEvent.content_text == "opencode child work").one()
        assert child_event.session_id == OPENCODE_PARENT_ID
        assert child_event.thread_id == child_thread.id
        edge = _edge_rows(db)[0]
        assert edge.edge_kind == "task_child"
        assert edge.visibility == "hidden"
        assert edge.source_thread_id is not None
        assert edge.target_thread_id == child_thread.id
        assert edge.provider_edge_id == "call_task"
        assert edge.metadata_json["child_provider_session_id"] == "ses_child"


def test_opencode_nested_task_child_attaches_to_subagent_parent_thread(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(
            _root_payload(
                session_id=OPENCODE_PARENT_ID,
                provider="opencode",
                provider_session_id="ses_parent",
                project="longhouse",
            )
        )
        store.ingest_session(
            SessionIngest(
                id=OPENCODE_CHILD_ID,
                provider="opencode",
                environment="production",
                project="longhouse",
                device_id="cinder",
                cwd="/Users/davidrose/git/zerg/longhouse",
                started_at=NOW,
                provider_session_id="ses_child",
                is_sidechain=True,
                parent_provider_session_id="ses_parent",
                subagent_id="general",
                subagent_tool_use_id="call_task",
                attribution_agent="general",
                events=[
                    EventIngest(
                        role="user",
                        content_text="opencode child work",
                        timestamp=NOW,
                        source_path="/Users/davidrose/.local/share/opencode/opencode.db#opencode:ses_child",
                        source_offset=0,
                    )
                ],
            )
        )
        nested_id = UUID("019ee600-0000-7000-8000-000000000005")
        store.ingest_session(
            SessionIngest(
                id=nested_id,
                provider="opencode",
                environment="production",
                project="longhouse",
                device_id="cinder",
                cwd="/Users/davidrose/git/zerg/longhouse",
                started_at=NOW,
                provider_session_id="ses_nested",
                is_sidechain=True,
                parent_provider_session_id="ses_child",
                subagent_id="explore",
                subagent_tool_use_id="call_nested",
                attribution_agent="explore",
                events=[
                    EventIngest(
                        role="user",
                        content_text="opencode nested child work",
                        timestamp=NOW,
                        source_path="/Users/davidrose/.local/share/opencode/opencode.db#opencode:ses_nested",
                        source_offset=0,
                    )
                ],
            )
        )

        assert db.query(AgentSession).count() == 1
        child_thread = (
            db.query(SessionThread)
            .join(SessionThreadAlias, SessionThreadAlias.thread_id == SessionThread.id)
            .filter(SessionThreadAlias.alias_kind == "provider_session_id")
            .filter(SessionThreadAlias.alias_value == "ses_child")
            .one()
        )
        nested_thread = (
            db.query(SessionThread)
            .join(SessionThreadAlias, SessionThreadAlias.thread_id == SessionThread.id)
            .filter(SessionThreadAlias.alias_kind == "provider_session_id")
            .filter(SessionThreadAlias.alias_value == "ses_nested")
            .one()
        )
        assert child_thread.parent_thread_id is not None
        assert nested_thread.session_id == OPENCODE_PARENT_ID
        assert nested_thread.parent_thread_id == child_thread.id
        nested_event = db.query(AgentEvent).filter(AgentEvent.content_text == "opencode nested child work").one()
        assert nested_event.session_id == OPENCODE_PARENT_ID
        assert nested_event.thread_id == nested_thread.id

        edges = _edge_rows(db)
        assert [edge.edge_kind for edge in edges] == ["task_child", "task_child"]
        nested_edges = [edge for edge in edges if edge.target_thread_id == nested_thread.id]
        assert len(nested_edges) == 1
        assert nested_edges[0].source_thread_id == child_thread.id
        assert nested_edges[0].provider_edge_id == "call_nested"


def test_unresolved_opencode_task_child_is_hidden_from_default_timeline(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        result = store.ingest_session(
            SessionIngest(
                id=OPENCODE_CHILD_ID,
                provider="opencode",
                environment="production",
                project="longhouse",
                device_id="cinder",
                cwd="/Users/davidrose/git/zerg/longhouse",
                started_at=NOW,
                provider_session_id="ses_child",
                is_sidechain=True,
                parent_provider_session_id="ses_parent",
                subagent_id="explore",
                events=[
                    EventIngest(
                        role="user",
                        content_text="orphan opencode child",
                        timestamp=NOW,
                        source_path="/Users/davidrose/.local/share/opencode/opencode.db#opencode:ses_child",
                        source_offset=0,
                    )
                ],
            )
        )

        assert result.session_id == OPENCODE_CHILD_ID
        primary = (
            db.query(SessionThread)
            .filter(SessionThread.session_id == OPENCODE_CHILD_ID, SessionThread.is_primary == 1)
            .one()
        )
        assert primary.branch_kind == "subagent"
        aliases = _thread_alias_values(db, primary.id)
        assert ("forked_from_provider_session_id", "ses_parent") in aliases
        assert ("subagent_id", "explore") in aliases
        edge = _edge_rows(db)[0]
        assert edge.edge_kind == "task_child"
        assert edge.visibility == "hidden"
        assert edge.source_thread_id is None
        assert edge.target_thread_id == primary.id
        assert edge.provider_edge_id == "ses_parent:ses_child"

        total, rows = store.list_timeline_thread_page(hide_autonomous=True, include_test=True)
        assert total == 0
        assert rows == ()


def test_opencode_task_child_relinks_when_parent_arrives_later(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(
            SessionIngest(
                id=OPENCODE_CHILD_ID,
                provider="opencode",
                environment="production",
                project="longhouse",
                device_id="cinder",
                cwd="/Users/davidrose/git/zerg/longhouse",
                started_at=NOW,
                provider_session_id="ses_child",
                is_sidechain=True,
                parent_provider_session_id="ses_parent",
                subagent_id="explore",
                attribution_agent="explore",
                events=[
                    EventIngest(
                        role="user",
                        content_text="orphan then relinked",
                        timestamp=NOW,
                        source_path="/Users/davidrose/.local/share/opencode/opencode.db#opencode:ses_child",
                        source_offset=0,
                    )
                ],
            )
        )
        assert db.query(AgentSession).count() == 1

        parent_result = store.ingest_session(
            _root_payload(
                session_id=OPENCODE_PARENT_ID,
                provider="opencode",
                provider_session_id="ses_parent",
                project="longhouse",
            )
        )

        assert parent_result.session_id == OPENCODE_PARENT_ID
        assert db.query(AgentSession).count() == 1
        assert db.query(AgentSession).filter(AgentSession.id == OPENCODE_CHILD_ID).first() is None
        child_thread = db.query(SessionThread).filter(SessionThread.branch_kind == "subagent").one()
        assert child_thread.session_id == OPENCODE_PARENT_ID
        aliases = _thread_alias_values(db, child_thread.id)
        assert ("subagent_id", "explore") in aliases
        assert ("forked_from_provider_session_id", "ses_parent") in aliases

        child_event = db.query(AgentEvent).filter(AgentEvent.content_text == "orphan then relinked").one()
        assert child_event.session_id == OPENCODE_PARENT_ID
        assert child_event.thread_id == child_thread.id
        edges = _edge_rows(db)
        assert len(edges) == 1
        assert edges[0].edge_kind == "task_child"
        assert edges[0].source_thread_id is not None
        assert edges[0].target_thread_id == child_thread.id
        assert edges[0].metadata_json["child_provider_session_id"] == "ses_child"


def test_task_child_ingest_relinks_preexisting_orphan_when_parent_known(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(
            _root_payload(
                session_id=OPENCODE_PARENT_ID,
                provider="opencode",
                provider_session_id="ses_parent",
                project="longhouse",
            )
        )

        orphan = AgentSession(
            id=OPENCODE_CHILD_ID,
            provider="opencode",
            environment="production",
            project="longhouse",
            device_id="cinder",
            cwd="/Users/davidrose/git/zerg/longhouse",
            started_at=NOW,
        )
        db.add(orphan)
        db.flush()
        orphan_thread = SessionThread(
            session_id=orphan.id,
            provider="opencode",
            branch_kind="subagent",
            is_primary=1,
        )
        db.add(orphan_thread)
        db.flush()
        db.add_all(
            [
                SessionThreadAlias(
                    thread_id=orphan_thread.id,
                    provider="opencode",
                    alias_kind="provider_session_id",
                    alias_value="ses_child",
                ),
                SessionThreadAlias(
                    thread_id=orphan_thread.id,
                    provider="opencode",
                    alias_kind="forked_from_provider_session_id",
                    alias_value="ses_parent",
                ),
            ]
        )
        db.commit()

        result = store.ingest_session(
            SessionIngest(
                id=OPENCODE_CHILD_ID,
                provider="opencode",
                environment="production",
                project="longhouse",
                device_id="cinder",
                cwd="/Users/davidrose/git/zerg/longhouse",
                started_at=NOW,
                provider_session_id="ses_child",
                is_sidechain=True,
                parent_provider_session_id="ses_parent",
                subagent_id="explore",
                attribution_agent="explore",
                events=[
                    EventIngest(
                        role="user",
                        content_text="preexisting orphan relinked",
                        timestamp=NOW,
                        source_path="/Users/davidrose/.local/share/opencode/opencode.db#opencode:ses_child",
                        source_offset=0,
                    )
                ],
            )
        )

        assert result.session_id == OPENCODE_PARENT_ID
        assert db.query(AgentSession).count() == 1
        child_thread = db.query(SessionThread).filter(SessionThread.branch_kind == "subagent").one()
        assert child_thread.session_id == OPENCODE_PARENT_ID
        assert ("provider_session_id", "ses_child") in _thread_alias_values(db, child_thread.id)


def test_unrelatable_provider_binding_conflict_records_diagnostic_without_third_session(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(
            _root_payload(
                session_id=OPENCODE_PARENT_ID,
                provider="opencode",
                provider_session_id="ses_parent",
                project="longhouse",
            )
        )

        foreign = AgentSession(
            id=OPENCODE_CHILD_ID,
            provider="opencode",
            environment="production",
            project="other",
            device_id="cinder",
            cwd="/tmp/other",
            started_at=NOW,
        )
        db.add(foreign)
        db.flush()
        foreign_thread = SessionThread(
            session_id=foreign.id,
            provider="opencode",
            branch_kind="root",
            is_primary=1,
        )
        db.add(foreign_thread)
        db.flush()
        db.add(
            SessionThreadAlias(
                thread_id=foreign_thread.id,
                provider="opencode",
                alias_kind="provider_session_id",
                alias_value="ses_child",
            )
        )
        db.commit()

        result = store.ingest_session(
            SessionIngest(
                id=UUID("019ee600-0000-7000-8000-000000000099"),
                provider="opencode",
                environment="production",
                project="longhouse",
                device_id="cinder",
                cwd="/Users/davidrose/git/zerg/longhouse",
                started_at=NOW,
                provider_session_id="ses_child",
                is_sidechain=True,
                parent_provider_session_id="ses_parent",
                subagent_id="explore",
                events=[
                    EventIngest(
                        role="user",
                        content_text="conflicting provider child",
                        timestamp=NOW,
                        source_path="/Users/davidrose/.local/share/opencode/opencode.db#opencode:ses_child",
                        source_offset=0,
                    )
                ],
            )
        )

        assert result.session_id == OPENCODE_CHILD_ID
        assert db.query(AgentSession).count() == 2
        diagnostic = db.query(SessionObservation).filter(SessionObservation.kind == "provider_binding_conflict").one()
        assert diagnostic.session_id == OPENCODE_CHILD_ID
        assert "ses_child" in (diagnostic.payload_json or "")


def test_opencode_fork_parentage_stays_visible_with_fork_thread_alias(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        result = store.ingest_session(
            SessionIngest(
                id=OPENCODE_FORK_ID,
                provider="opencode",
                environment="production",
                project="longhouse",
                device_id="cinder",
                cwd="/Users/davidrose/git/zerg/longhouse",
                started_at=NOW,
                provider_session_id="ses_fork",
                parent_provider_session_id="ses_parent",
                lineage_kind="fork",
                events=[
                    EventIngest(
                        role="user",
                        content_text="forked opencode work",
                        timestamp=NOW,
                        source_path="/Users/davidrose/.local/share/opencode/opencode.db#opencode:ses_fork",
                        source_offset=0,
                    )
                ],
            )
        )

        assert result.session_id == OPENCODE_FORK_ID
        primary = (
            db.query(SessionThread)
            .filter(SessionThread.session_id == OPENCODE_FORK_ID, SessionThread.is_primary == 1)
            .one()
        )
        assert primary.branch_kind == "fork"
        aliases = _thread_alias_values(db, primary.id)
        assert ("provider_session_id", "ses_fork") in aliases
        assert ("forked_from_provider_session_id", "ses_parent") in aliases
        edge = _edge_rows(db)[0]
        assert edge.edge_kind == "fork"
        assert edge.visibility == "timeline"
        assert edge.target_thread_id == primary.id
        assert edge.metadata_json["parent_provider_session_id"] == "ses_parent"

        total, rows = store.list_timeline_thread_page(hide_autonomous=True, include_test=True)
        assert total == 1
        assert rows[0][1] == str(OPENCODE_FORK_ID)


def test_opencode_unknown_parentage_stays_visible_without_fork_label(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        result = store.ingest_session(
            SessionIngest(
                id=OPENCODE_FORK_ID,
                provider="opencode",
                environment="production",
                project="longhouse",
                device_id="cinder",
                cwd="/Users/davidrose/git/zerg/longhouse",
                started_at=NOW,
                provider_session_id="ses_linked",
                parent_provider_session_id="ses_parent",
                events=[
                    EventIngest(
                        role="user",
                        content_text="linked opencode work",
                        timestamp=NOW,
                        source_path="/Users/davidrose/.local/share/opencode/opencode.db#opencode:ses_linked",
                        source_offset=0,
                    )
                ],
            )
        )

        assert result.session_id == OPENCODE_FORK_ID
        primary = (
            db.query(SessionThread)
            .filter(SessionThread.session_id == OPENCODE_FORK_ID, SessionThread.is_primary == 1)
            .one()
        )
        assert primary.branch_kind == "root"
        aliases = _thread_alias_values(db, primary.id)
        assert ("provider_session_id", "ses_linked") in aliases
        assert ("parent_provider_session_id", "ses_parent") in aliases
        assert ("forked_from_provider_session_id", "ses_parent") not in aliases
        edge = _edge_rows(db)[0]
        assert edge.edge_kind == "unknown"
        assert edge.visibility == "timeline"
        assert edge.target_thread_id == primary.id
        assert edge.metadata_json["parent_provider_session_id"] == "ses_parent"

        total, rows = store.list_timeline_thread_page(hide_autonomous=True, include_test=True)
        assert total == 1
        assert rows[0][1] == str(OPENCODE_FORK_ID)


def test_session_graph_projection_surfaces_child_fork_and_unknown_edges(tmp_path):
    db_path = tmp_path / "session-graph.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    factory = make_sessionmaker(engine)
    linked_id = UUID("019ee600-0000-7000-8000-000000000004")

    with factory() as db:
        store = AgentsStore(db)
        store.ingest_session(
            _root_payload(
                session_id=OPENCODE_PARENT_ID,
                provider="opencode",
                provider_session_id="ses_parent",
                project="longhouse",
            )
        )
        store.ingest_session(
            SessionIngest(
                id=OPENCODE_CHILD_ID,
                provider="opencode",
                environment="production",
                project="longhouse",
                device_id="cinder",
                cwd="/Users/davidrose/git/zerg/longhouse",
                started_at=NOW,
                provider_session_id="ses_child",
                is_sidechain=True,
                parent_provider_session_id="ses_parent",
                subagent_id="explore",
                subagent_tool_use_id="call_task",
                attribution_agent="explore",
                events=[
                    EventIngest(
                        role="user",
                        content_text="opencode child work",
                        timestamp=NOW,
                        source_path="/Users/davidrose/.local/share/opencode/opencode.db#opencode:ses_child",
                        source_offset=0,
                    )
                ],
            )
        )
        store.ingest_session(
            SessionIngest(
                id=OPENCODE_FORK_ID,
                provider="opencode",
                environment="production",
                project="longhouse",
                device_id="cinder",
                cwd="/Users/davidrose/git/zerg/longhouse",
                started_at=NOW,
                provider_session_id="ses_fork",
                parent_provider_session_id="ses_parent",
                lineage_kind="fork",
                events=[
                    EventIngest(
                        role="user",
                        content_text="forked opencode work",
                        timestamp=NOW,
                        source_path="/Users/davidrose/.local/share/opencode/opencode.db#opencode:ses_fork",
                        source_offset=0,
                    )
                ],
            )
        )
        store.ingest_session(
            SessionIngest(
                id=linked_id,
                provider="opencode",
                environment="production",
                project="longhouse",
                device_id="cinder",
                cwd="/Users/davidrose/git/zerg/longhouse",
                started_at=NOW,
                provider_session_id="ses_linked",
                parent_provider_session_id="ses_parent",
                events=[
                    EventIngest(
                        role="user",
                        content_text="linked opencode work",
                        timestamp=NOW,
                        source_path="/Users/davidrose/.local/share/opencode/opencode.db#opencode:ses_linked",
                        source_offset=0,
                    )
                ],
            )
        )
        projection = build_session_graph_projection(db, OPENCODE_PARENT_ID)
        db.commit()

    assert projection["session_id"] == str(OPENCODE_PARENT_ID)
    assert [edge["edge_kind"] for edge in projection["children"]] == ["task_child"]
    assert projection["children"][0]["agent_id"] == "explore"
    assert [edge["edge_kind"] for edge in projection["forks"]] == ["fork"]
    assert [edge["edge_kind"] for edge in projection["linked"]] == ["unknown"]

    def override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[verify_agents_token] = lambda: None
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    try:
        client = TestClient(api_app)
        response = client.get(f"/agents/sessions/{OPENCODE_PARENT_ID}/graph")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["session_id"] == str(OPENCODE_PARENT_ID)
        assert [edge["edge_kind"] for edge in body["children"]] == ["task_child"]
        assert [edge["edge_kind"] for edge in body["forks"]] == ["fork"]
        assert [edge["edge_kind"] for edge in body["linked"]] == ["unknown"]
    finally:
        api_app.dependency_overrides.clear()


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
        db.add(SessionTask(session_id=str(CHILD_ID), task_type="summary", status="pending"))
        db.add(
            SessionEmbedding(
                session_id=CHILD_ID,
                kind="session",
                chunk_index=-1,
                model="test-embedding",
                dims=1,
                embedding=b"\x00\x00\x00\x00",
            )
        )
        db.flush()

        report = backfill_subagent_child_threads(db)

        assert report["candidates_resolved"] == 1
        assert report["sessions_removed"] == 1
        assert report["legacy_tasks_deleted"] == 1
        assert report["embeddings_deleted"] == 1
        assert report["parent_counts_refreshed"] == 1
        assert db.query(AgentSession).count() == 1
        assert db.query(AgentSession).filter(AgentSession.id == CHILD_ID).first() is None
        assert db.query(SessionTask).filter(SessionTask.session_id == str(CHILD_ID)).count() == 0
        assert db.query(SessionEmbedding).filter(SessionEmbedding.session_id == CHILD_ID).count() == 0

        child_thread = db.query(SessionThread).filter(SessionThread.branch_kind == "subagent").one()
        assert child_thread.session_id == PARENT_ID
        child_event = db.query(AgentEvent).filter(AgentEvent.content_text == "Deploy crims on drose.io").one()
        assert child_event.session_id == PARENT_ID
        assert child_event.thread_id == child_thread.id
        child_source_line = db.query(AgentSourceLine).filter(AgentSourceLine.source_path.like("%/subagents/%")).one()
        assert child_source_line.session_id == PARENT_ID
        assert child_source_line.thread_id == child_thread.id
        parent_session = db.query(AgentSession).filter(AgentSession.id == PARENT_ID).one()
        parent_user_events = (
            db.query(AgentEvent).filter(AgentEvent.session_id == PARENT_ID, AgentEvent.role == "user").count()
        )
        assert parent_user_events == 2
        assert parent_session.user_messages == 1
        assert bool(parent_session.needs_embedding) is True

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
        child_thread = db.query(SessionThread).filter(SessionThread.branch_kind == "subagent").first()
        assert child_thread is not None
        child_thread_id = str(child_thread.id)
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

        events_response = client.get(f"/timeline/sessions/{PARENT_ID}/events")
        assert events_response.status_code == 200, events_response.text
        events_body = events_response.json()
        assert events_body["total"] == 1
        assert [event["content_text"] for event in events_body["events"]] == ["Profile README redesign"]

        child_response = client.get(f"/timeline/sessions/{PARENT_ID}/events?thread_id={child_thread_id}")
        assert child_response.status_code == 200, child_response.text
        child_body = child_response.json()
        assert child_body["total"] == 3
        assert {event["content_text"] for event in child_body["events"]} == {"Deploy crims on drose.io"}
    finally:
        api_app.dependency_overrides.clear()
