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

from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from zerg.database import Base
from zerg.database import _ensure_agents_fts
from zerg.database import make_engine
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionThread
from zerg.services.agents import AgentsStore
from zerg.services.agents import EventIngest
from zerg.services.agents import SessionIngest

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


def _agent_payload(
    *,
    parent_provider_session_id: str | None = str(PARENT_ID),
    agent_session_id: UUID = AGENT_ID,
    subagent_id: str = "a049eaf15e4dbcae3",
    attribution_skill: str | None = "deep-research",
) -> SessionIngest:
    return SessionIngest(
        id=agent_session_id,
        provider="claude",
        environment="production",
        project="g55",
        device_id="cinder",
        cwd="/Users/davidrose/git/g55",
        git_branch="main",
        started_at=NOW,
        provider_session_id=str(agent_session_id),
        is_sidechain=True,
        parent_provider_session_id=parent_provider_session_id,
        subagent_id=subagent_id,
        workflow_run_id=RUN,
        attribution_agent="workflow-subagent",
        attribution_skill=attribution_skill,
        events=[
            EventIngest(
                role="user",
                content_text="decompose the research question",
                timestamp=NOW,
                source_path=f"{_PROJECT_DIR}/{PARENT_ID}/subagents/workflows/{RUN}/agent-{subagent_id}.jsonl",
                source_offset=0,
                raw_json=(
                    '{"type":"user","uuid":"agent-u1","isSidechain":true,'
                    f'"sessionId":"{PARENT_ID}","agentId":"{subagent_id}",'
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


def test_agent_before_parent_self_heals_on_parent_arrival(tmp_path):
    """Phase 2: a subagent ingested BEFORE its parent first becomes a standalone
    orphan, then is automatically re-parented when the parent arrives."""
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        # Agent arrives first — parent unknown -> orphan.
        result = store.ingest_session(_agent_payload())
        assert result.session_id == AGENT_ID, "orphan keeps its own session id"
        orphan_primary = (
            db.query(SessionThread)
            .filter(SessionThread.session_id == AGENT_ID, SessionThread.is_primary == 1)
            .one()
        )
        assert orphan_primary.branch_kind == "subagent"
        assert orphan_primary.parent_thread_id is None
        assert db.query(AgentSession).count() == 1

        # Parent arrives -> orphan is relinked under it, standalone session gone.
        store.ingest_session(_parent_payload())
        assert db.query(AgentSession).filter(AgentSession.id == AGENT_ID).first() is None
        assert db.query(AgentSession).count() == 1

        child = (
            db.query(SessionThread)
            .filter(SessionThread.session_id == PARENT_ID, SessionThread.branch_kind == "subagent")
            .one()
        )
        assert child.is_primary == 0
        # The subagent's event now lives under the parent + child thread.
        moved_event = db.query(AgentEvent).filter(AgentEvent.content_text == "decompose the research question").one()
        assert moved_event.session_id == PARENT_ID
        assert moved_event.thread_id == child.id


def test_relink_is_idempotent(tmp_path):
    """Re-ingesting the parent after a relink is a no-op (no duplicate threads,
    no resurrected orphan)."""
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(_agent_payload())
        store.ingest_session(_parent_payload())
        store.ingest_session(_parent_payload())  # second parent ingest

        assert db.query(AgentSession).count() == 1
        assert db.query(SessionThread).filter(SessionThread.branch_kind == "subagent").count() == 1


def test_relink_discards_exact_duplicate_rows_before_reparenting(tmp_path):
    """A retried sidechain may already exist on the parent under its source key.

    Relinking must preserve that durable copy instead of repeatedly failing the
    parent's event/source-line uniqueness constraints.
    """
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        agent = _agent_payload()
        store.ingest_session(agent)

        parent = _parent_payload()
        parent.events = [agent.events[0]]
        store.ingest_session(parent)

        assert db.query(AgentSession).filter(AgentSession.id == AGENT_ID).first() is None
        assert db.query(AgentEvent).filter(AgentEvent.session_id == PARENT_ID).count() == 1


def test_journal_before_parent_does_not_relink(tmp_path):
    """A journal ingested before the parent is dropped (not an orphan), and the
    parent's later arrival does not resurrect or relink anything."""
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(_journal_payload())  # dropped by the guard
        assert db.query(AgentSession).count() == 0

        store.ingest_session(_parent_payload())
        assert db.query(AgentSession).count() == 1
        assert db.query(SessionThread).filter(SessionThread.branch_kind == "subagent").count() == 0


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


# === Phase 3 (P2): workflow_run_id + attribution stored + queryable ===


def test_workflow_run_id_and_attribution_stored_as_thread_aliases(tmp_path):
    from zerg.models.agents import SessionThreadAlias

    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(_parent_payload())
        store.ingest_session(_agent_payload())

        child = (
            db.query(SessionThread)
            .filter(SessionThread.session_id == PARENT_ID, SessionThread.branch_kind == "subagent")
            .one()
        )
        aliases = {
            (row.alias_kind, row.alias_value)
            for row in db.query(SessionThreadAlias).filter(SessionThreadAlias.thread_id == child.id).all()
        }
        assert ("subagent_id", "a049eaf15e4dbcae3") in aliases
        assert ("claude_agent_id", "a049eaf15e4dbcae3") in aliases
        assert ("workflow_run_id", RUN) in aliases
        assert ("workflow_attribution_agent", "workflow-subagent") in aliases
        assert ("workflow_attribution_skill", "deep-research") in aliases


def test_multiple_agents_in_run_are_distinct_threads_not_collapsed(tmp_path):
    """The shared workflow_run_id / attribution must NOT collapse distinct
    agents onto one thread — each agent file is its own subagent thread."""
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(_parent_payload())
        store.ingest_session(
            _agent_payload(agent_session_id=AGENT_ID, subagent_id="a049eaf15e4dbcae3", attribution_skill=None)
        )
        store.ingest_session(
            _agent_payload(
                agent_session_id=UUID("88888888-0000-0000-0000-0000000000cc"),
                subagent_id="a04eaddc8e3b46986",
            )
        )

        subagent_threads = db.query(SessionThread).filter(SessionThread.branch_kind == "subagent").all()
        assert len(subagent_threads) == 2, "two agents in the run -> two distinct threads"

        run = store.get_workflow_run(RUN)
        assert run is not None
        assert run["agent_count"] == 2
        assert run["skill"] == "deep-research"
        assert run["parent_session_id"] == str(PARENT_ID)
        assert {a["agent_id"] for a in run["agents"]} == {"a049eaf15e4dbcae3", "a04eaddc8e3b46986"}


def test_get_workflow_run_unknown_returns_none(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        assert store.get_workflow_run("wf_does_not_exist") is None


def test_relink_failure_does_not_corrupt_committed_parent(tmp_path, monkeypatch):
    """If relink raises mid-ingest, the already-committed parent must survive and
    the transaction is rolled back cleanly (no half-relinked state)."""
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(_agent_payload())  # orphan exists

        def _boom(*_a, **_k):
            raise RuntimeError("relink blew up")

        monkeypatch.setattr(
            "zerg.services.agents.kernel_backfill.relink_orphan_subagents_for_parent",
            _boom,
        )
        # Parent ingest must still succeed despite the relink failure.
        result = store.ingest_session(_parent_payload())
        assert result.session_id == PARENT_ID
        # Parent persisted; orphan untouched (relink rolled back, not half-applied).
        assert db.query(AgentSession).filter(AgentSession.id == PARENT_ID).first() is not None
        assert db.query(AgentSession).filter(AgentSession.id == AGENT_ID).first() is not None


def test_relink_does_not_rebuild_entire_fts_index(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'workflow-ingest-fts.db'}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    _ensure_agents_fts(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    rebuild_statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _capture_rebuild(_conn, _cursor, statement, _parameters, _context, _executemany):
        sql = str(statement).strip()
        if sql.startswith("INSERT INTO events_fts(events_fts) VALUES('rebuild')"):
            rebuild_statements.append(sql)

    try:
        with SessionLocal() as db:
            store = AgentsStore(db)
            store.ingest_session(_agent_payload())
            store.ingest_session(_parent_payload())
            assert db.query(AgentSession).filter(AgentSession.id == AGENT_ID).first() is None
            assert not rebuild_statements
    finally:
        event.remove(engine, "before_cursor_execute", _capture_rebuild)


def test_list_workflow_runs_for_session(tmp_path):
    """The session detail data source: one entry per workflow run under the
    parent, with agent count + skill."""
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(_parent_payload())
        store.ingest_session(_agent_payload(agent_session_id=AGENT_ID, subagent_id="a049eaf15e4dbcae3"))
        store.ingest_session(
            _agent_payload(
                agent_session_id=UUID("88888888-0000-0000-0000-0000000000cc"),
                subagent_id="a04eaddc8e3b46986",
            )
        )

        runs = store.list_workflow_runs_for_session(PARENT_ID)
        assert len(runs) == 1
        assert runs[0]["workflow_run_id"] == RUN
        assert runs[0]["agent_count"] == 2
        assert runs[0]["skill"] == "deep-research"

        # A session with no workflow subagents -> empty list.
        assert store.list_workflow_runs_for_session(JOURNAL_ID) == []


def test_workflow_run_query_endpoint(tmp_path):
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from zerg.database import get_db
    from zerg.dependencies.agents_auth import require_single_tenant
    from zerg.dependencies.agents_auth import verify_agents_token
    from zerg.main import api_app

    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(_parent_payload())
        store.ingest_session(_agent_payload())
        db.commit()

    def override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    from zerg.dependencies.browser_auth import get_current_browser_user

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[verify_agents_token] = lambda: SimpleNamespace(device_id="d", id="t", owner_id=1)
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    api_app.dependency_overrides[get_current_browser_user] = lambda: SimpleNamespace(id=1, email="david010@example.com")
    try:
        client = TestClient(api_app)
        # Machine-facing /agents route.
        resp = client.get(f"/agents/workflows/{RUN}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["workflow_run_id"] == RUN
        assert body["agent_count"] == 1
        assert body["agents"][0]["attribution_skill"] == "deep-research"

        missing = client.get("/agents/workflows/wf_nope")
        assert missing.status_code == 404

        # Browser-facing /timeline mirror (what the web UI calls).
        t_resp = client.get(f"/timeline/workflows/{RUN}")
        assert t_resp.status_code == 200, t_resp.text
        assert t_resp.json()["workflow_run_id"] == RUN

        t_runs = client.get(f"/timeline/sessions/{PARENT_ID}/workflows")
        assert t_runs.status_code == 200, t_runs.text
        t_body = t_runs.json()
        assert len(t_body["workflow_runs"]) == 1
        assert t_body["workflow_runs"][0]["workflow_run_id"] == RUN
    finally:
        api_app.dependency_overrides.clear()
