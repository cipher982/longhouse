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


def _seed_legacy_journal_session(db):
    """Insert a journal-junk session as it would have been ingested BEFORE the
    Phase 1 guard existed: a session row + a branch + one source line whose path
    is a workflow journal, and zero events."""
    from zerg.models.agents import AgentSession
    from zerg.models.agents import AgentSessionBranch
    from zerg.models.agents import AgentSourceLine
    from zerg.models.agents import SessionObservation

    db.add(
        AgentSession(
            id=JOURNAL_ID,
            provider="claude",
            environment="production",
            project="g55",
            device_id="cinder",
            cwd="/Users/davidrose/git/g55",
            started_at=NOW,
            user_messages=0,
        )
    )
    db.flush()
    branch = AgentSessionBranch(session_id=JOURNAL_ID, branch_reason="root", is_head=1)
    db.add(branch)
    db.flush()
    db.add(
        AgentSourceLine(
            session_id=JOURNAL_ID,
            source_path=_JOURNAL_PATH,
            source_offset=0,
            branch_id=branch.id,
            raw_json='{"type":"started","key":"v2:abc","agentId":"a049eaf15e4dbcae3"}',
            line_hash="0" * 64,
        )
    )
    # Source-line ingest also records a session-scoped observation per line.
    db.add(
        SessionObservation(
            observation_id="obs-journal-1",
            session_id=JOURNAL_ID,
            provider="claude",
            source_domain="transcript",
            kind="provider_source_line",
            source="agents_ingest",
            observed_at=NOW,
        )
    )
    db.flush()


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


# === Phase 1: journal.jsonl no longer pollutes the timeline ===


def test_workflow_journal_ingest_creates_no_session(tmp_path):
    """A journal-only ingest is dropped at the store: no session, no source
    lines, nothing visible in the timeline."""
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        result = store.ingest_session(_journal_payload())
        assert result.events_inserted == 0
        assert result.session_created is False

        assert db.query(AgentSession).filter(AgentSession.id == JOURNAL_ID).first() is None
        assert db.query(AgentSession).count() == 0

        total, rows = store.list_timeline_thread_page(hide_autonomous=True, include_test=True)
        assert total == 0
        assert rows == ()


def test_cleanup_removes_already_ingested_journal_session(tmp_path):
    """The cleanup sweep removes a journal-junk session that was ingested
    before the guard existed, and is idempotent."""
    from zerg.services.agents.kernel_backfill import cleanup_workflow_journal_sessions

    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        # Real parent + agent must survive the sweep untouched.
        store.ingest_session(_parent_payload())
        store.ingest_session(_agent_payload())

        # Simulate a pre-fix leaked journal session directly (bypass the guard).
        _seed_legacy_journal_session(db)
        assert db.query(AgentSession).filter(AgentSession.id == JOURNAL_ID).first() is not None

        report = cleanup_workflow_journal_sessions(db)
        assert report["sessions_removed"] == 1
        assert db.query(AgentSession).filter(AgentSession.id == JOURNAL_ID).first() is None

        # No residue: source lines AND observations for the journal session gone.
        from zerg.models.agents import AgentSourceLine
        from zerg.models.agents import SessionObservation

        assert db.query(AgentSourceLine).filter(AgentSourceLine.session_id == JOURNAL_ID).count() == 0
        assert db.query(SessionObservation).filter(SessionObservation.session_id == JOURNAL_ID).count() == 0

        # Parent + its subagent thread untouched.
        assert db.query(AgentSession).filter(AgentSession.id == PARENT_ID).first() is not None
        assert db.query(SessionThread).filter(SessionThread.branch_kind == "subagent").count() == 1

        second = cleanup_workflow_journal_sessions(db)
        assert second["sessions_removed"] == 0


def test_backfill_does_not_relink_workflow_journal(tmp_path):
    """A journal source path under /subagents/ is NOT a subagent relink
    candidate, so the backfill never re-parents journal junk."""
    from zerg.services.agents.kernel_backfill import backfill_subagent_child_threads

    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(_parent_payload())
        _seed_legacy_journal_session(db)

        report = backfill_subagent_child_threads(db)
        # The only /subagents/ path present is the journal -> not a candidate.
        assert report["candidates_resolved"] == 0
        # Journal session still stands alone (cleanup, not backfill, removes it).
        assert db.query(AgentSession).filter(AgentSession.id == JOURNAL_ID).first() is not None
        assert db.query(SessionThread).filter(SessionThread.branch_kind == "subagent").count() == 0


def test_agent_before_parent_becomes_orphan_baseline(tmp_path):
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


# === Phase 1: shared journal predicate (used by router short-circuit + store guard) ===


def test_is_workflow_journal_only_payload_predicate():
    from zerg.routers.agents_ingest import is_workflow_journal_only_payload

    # Pure journal payload -> True
    assert is_workflow_journal_only_payload(_journal_payload()) is True

    # An agent transcript (has events) -> False, even though it is under subagents/
    assert is_workflow_journal_only_payload(_agent_payload()) is False

    # A normal session -> False
    assert is_workflow_journal_only_payload(_parent_payload()) is False

    # A non-workflow file literally named journal.jsonl -> False (path guard).
    not_workflow = SessionIngest(
        id=UUID("77777777-0000-0000-0000-0000000000cc"),
        provider="claude",
        environment="production",
        project="g55",
        started_at=NOW,
        events=[],
        source_lines=[
            {
                "source_path": f"{_PROJECT_DIR}/{PARENT_ID}/journal.jsonl",
                "source_offset": 0,
                "raw_json": '{"type":"started"}',
            }
        ],
    )
    assert is_workflow_journal_only_payload(not_workflow) is False
