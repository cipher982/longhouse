"""Dynamic-workflow ingest behavior.

Claude Code "dynamic workflows" fan out many subagents and write a new on-disk
layout under ``<SID>/subagents/workflows/<run>/``:

- ``agent-<id>.jsonl``  — one real subagent transcript per agent (``isSidechain:true``)
- ``journal.jsonl``     — a control ledger (``{type:"started"|"result"}`` only, NO role events)

The engine ships each ``*.jsonl`` as its own ``SessionIngest``. This module pins
the behavior end-to-end on the server side.

Phase 0 tests (``baseline_*``) assert TODAY's broken behavior so later phases can
invert them; the non-baseline tests assert invariants that must hold throughout.
"""

from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from uuid import UUID

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("TESTING", "1")

from sqlalchemy.orm import sessionmaker

from zerg.database import Base
from zerg.database import make_engine
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionThread
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest

# A fixed parent session id (matches the committed engine fixture).
PARENT_ID = UUID("11111111-2222-3333-4444-555555555555")
JOURNAL_ID = UUID("99999999-0000-0000-0000-0000000000aa")
AGENT_ID = UUID("88888888-0000-0000-0000-0000000000bb")
RUN = "wf_testrun01"
NOW = datetime(2026, 6, 7, 22, 11, 9, tzinfo=timezone.utc)

_PROJECT_DIR = "/Users/davidrose/.claude/projects/-Users-davidrose-git-g55"
_JOURNAL_PATH = f"{_PROJECT_DIR}/{PARENT_ID}/subagents/workflows/{RUN}/journal.jsonl"
_AGENT_PATH = f"{_PROJECT_DIR}/{PARENT_ID}/subagents/workflows/{RUN}/agent-a049eaf15e4dbcae3.jsonl"


def _session_factory(tmp_path, name="workflow-ingest.db"):
    engine = make_engine(f"sqlite:///{tmp_path / name}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _parent_payload() -> SessionIngest:
    return SessionIngest(
        id=PARENT_ID,
        provider="claude",
        environment="production",
        project="g55",
        device_id="cinder",
        cwd="/Users/davidrose/git/g55",
        git_branch="main",
        started_at=NOW,
        provider_session_id=str(PARENT_ID),
        events=[
            EventIngest(
                role="user",
                content_text="research the g55 transmission",
                timestamp=NOW,
                source_path=f"{_PROJECT_DIR}/{PARENT_ID}.jsonl",
                source_offset=0,
                raw_json='{"type":"user","uuid":"root-u1","message":{"content":"research"}}',
            )
        ],
    )


def _journal_payload() -> SessionIngest:
    """What the engine ships TODAY for journal.jsonl: a session with NO events,
    only archived source lines (the control-ledger lines)."""
    return SessionIngest(
        id=JOURNAL_ID,
        provider="claude",
        environment="production",
        project="g55",
        device_id="cinder",
        cwd="/Users/davidrose/git/g55",
        started_at=NOW,
        provider_session_id=str(JOURNAL_ID),
        events=[],
        source_lines=[
            {
                "source_path": _JOURNAL_PATH,
                "source_offset": 0,
                "raw_json": '{"type":"started","key":"v2:abc","agentId":"a049eaf15e4dbcae3"}',
            },
            {
                "source_path": _JOURNAL_PATH,
                "source_offset": 64,
                "raw_json": '{"type":"result","key":"v2:abc","agentId":"a049eaf15e4dbcae3","result":{"summary":"x"}}',
            },
        ],
    )


def _agent_payload(*, parent_provider_session_id: str | None = str(PARENT_ID)) -> SessionIngest:
    return SessionIngest(
        id=AGENT_ID,
        provider="claude",
        environment="production",
        project="g55",
        device_id="cinder",
        cwd="/Users/davidrose/git/g55",
        git_branch="main",
        started_at=NOW,
        provider_session_id=str(AGENT_ID),
        is_sidechain=True,
        parent_provider_session_id=parent_provider_session_id,
        subagent_id="a049eaf15e4dbcae3",
        events=[
            EventIngest(
                role="user",
                content_text="decompose the research question",
                timestamp=NOW,
                source_path=_AGENT_PATH,
                source_offset=0,
                raw_json=(
                    '{"type":"user","uuid":"agent-u1","isSidechain":true,'
                    f'"sessionId":"{PARENT_ID}","agentId":"a049eaf15e4dbcae3",'
                    '"message":{"content":"decompose"}}'
                ),
            )
        ],
    )


# === Phase 0 characterization: TODAY's behavior ===


def test_baseline_workflow_journal_creates_timeline_visible_session(tmp_path):
    """BASELINE (inverted in Phase 1): a journal-only ingest produces a session
    that is visible in the default timeline because it has 0 user messages but
    ``ended_at IS NULL`` (the filter admits open sessions)."""
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        result = store.ingest_session(_journal_payload())
        assert result.session_id == JOURNAL_ID

        session = db.query(AgentSession).filter(AgentSession.id == JOURNAL_ID).one()
        assert session.user_messages == 0
        assert session.ended_at is None

        total, rows = store.list_timeline_thread_page(hide_autonomous=True, include_test=True)
        visible_ids = {row[1] for row in rows}
        assert str(JOURNAL_ID) in visible_ids, "BASELINE: journal junk session pollutes the timeline today"


def test_baseline_agent_before_parent_becomes_orphan(tmp_path):
    """BASELINE (inverted in Phase 2): a subagent ingested BEFORE its parent
    becomes a standalone orphan session and does NOT self-heal when the parent
    arrives later."""
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        # Agent arrives first — parent unknown.
        result = store.ingest_session(_agent_payload())
        assert result.session_id == AGENT_ID, "orphan keeps its own session id"

        primary = (
            db.query(SessionThread)
            .filter(SessionThread.session_id == AGENT_ID, SessionThread.is_primary == 1)
            .one()
        )
        assert primary.branch_kind == "subagent"
        assert primary.parent_thread_id is None

        # Parent arrives later — today nothing re-parents the orphan.
        store.ingest_session(_parent_payload())
        assert db.query(AgentSession).filter(AgentSession.id == AGENT_ID).first() is not None, (
            "BASELINE: orphan does not self-heal on parent arrival"
        )
        assert db.query(AgentSession).count() == 2


def test_agent_after_parent_attaches_as_subagent_thread(tmp_path):
    """INVARIANT: when the parent is already known, the agent attaches as a
    subagent thread under it (no standalone session)."""
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(_parent_payload())
        result = store.ingest_session(_agent_payload())

        assert result.session_id == PARENT_ID
        assert db.query(AgentSession).count() == 1
        child = (
            db.query(SessionThread)
            .filter(SessionThread.session_id == PARENT_ID, SessionThread.branch_kind == "subagent")
            .one()
        )
        assert child.is_primary == 0
