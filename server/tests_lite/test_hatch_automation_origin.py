from __future__ import annotations

import json
import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
from uuid import UUID

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from typer.testing import CliRunner

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("TESTING", "1")

from zerg.database import get_db
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.dependencies.browser_auth import get_current_browser_user
from zerg.main import api_app
from zerg.main import app
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionEdge
from zerg.models.agents import SessionThread
from zerg.models.agents import TimelineCard
from zerg.services.agents import AgentsStore
from zerg.services.agents import EventIngest
from zerg.services.agents import SessionIngest
from zerg.services.agents.automation_backfill import classify_reviewed_hatch_automation_sessions
from zerg.services.apns_sender import APNSDeviceTarget
from zerg.services.apns_sender import prepare_session_attention_push
from zerg.services.session_coordination import query_wall_sessions

NOW = datetime(2026, 7, 8, 17, 31, tzinfo=timezone.utc)
PARENT_ID = UUID("aaaaaaaa-1111-4111-8111-111111111111")
HATCH_ID = UUID("bbbbbbbb-2222-4222-8222-222222222222")
PROVIDER_CHILD_ID = UUID("cccccccc-3333-4333-8333-333333333333")


def _session_factory(tmp_path, name: str = "hatch-automation-origin.db"):
    engine = make_engine(f"sqlite:///{tmp_path / name}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    initialize_database(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _make_client(SessionLocal):
    def override_get_db():
        with SessionLocal() as db:
            yield db

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[verify_agents_token] = lambda: SimpleNamespace(device_id="testclient", id="token-1")
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    api_app.dependency_overrides[get_current_browser_user] = lambda: SimpleNamespace(id=1)
    return TestClient(app, backend="asyncio"), api_app


def _root_payload(
    *,
    session_id: UUID = PARENT_ID,
    provider_session_id: str | None = None,
    text: str = "Parent user task",
) -> SessionIngest:
    return SessionIngest(
        id=session_id,
        provider="opencode",
        environment="production",
        project="longhouse",
        device_id="cinder",
        cwd="/Users/davidrose/git/zerg/longhouse",
        started_at=NOW - timedelta(minutes=5),
        provider_session_id=provider_session_id or f"ses_parent_{session_id.hex[:8]}",
        events=[
            EventIngest(
                role="user",
                content_text=text,
                timestamp=NOW - timedelta(minutes=5),
                source_path=f"/tmp/{session_id}.jsonl",
                source_offset=0,
                raw_json=f'{{"role":"user","content":"{text}"}}',
            )
        ],
    )


def _hatch_payload(
    *,
    session_id: UUID = HATCH_ID,
    provider_session_id: str = "ses_hatch_child",
    parent_longhouse_session_id: UUID | None = None,
    parent_thread_id: UUID | None = None,
    parent_provider_session_id: str | None = None,
    is_sidechain: bool = False,
    text: str = "Hatch automation unique review",
) -> SessionIngest:
    return SessionIngest(
        id=session_id,
        provider="opencode",
        environment="production",
        project="longhouse",
        device_id="cinder",
        cwd="/Users/davidrose/git/zerg/longhouse",
        started_at=NOW,
        provider_session_id=provider_session_id,
        origin_kind="hatch_automation",
        hatch_run_id="hatch-run-1",
        parent_longhouse_session_id=parent_longhouse_session_id,
        parent_thread_id=parent_thread_id,
        parent_provider_session_id=parent_provider_session_id,
        is_sidechain=is_sidechain,
        events=[
            EventIngest(
                role="user",
                content_text=text,
                timestamp=NOW,
                source_path=f"/tmp/{session_id}.jsonl",
                source_offset=0,
                raw_json=f'{{"role":"user","content":"{text}"}}',
            )
        ],
    )


def test_hatch_automation_ingest_persists_sticky_hidden_origin_and_edge(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(_root_payload())
        parent_thread = (
            db.query(SessionThread)
            .filter(SessionThread.session_id == PARENT_ID, SessionThread.is_primary == 1)
            .one()
        )

        result = store.ingest_session(
            _hatch_payload(
                parent_longhouse_session_id=PARENT_ID,
                parent_thread_id=parent_thread.id,
                parent_provider_session_id="ses_parent_aaaaaaaa",
                is_sidechain=True,
            )
        )

        assert result.session_id == HATCH_ID
        hatch_session = db.get(AgentSession, HATCH_ID)
        assert hatch_session.origin_kind == "hatch_automation"
        assert hatch_session.hidden_from_default_timeline == 1

        hatch_thread = (
            db.query(SessionThread)
            .filter(SessionThread.session_id == HATCH_ID, SessionThread.is_primary == 1)
            .one()
        )
        assert hatch_thread.branch_kind == "root"
        assert hatch_thread.origin_kind == "hatch_automation"
        assert hatch_thread.hidden_from_default_timeline == 1

        card = db.get(TimelineCard, HATCH_ID)
        assert card.origin_kind == "hatch_automation"
        assert card.hidden_from_default_timeline == 1

        edge = db.query(SessionEdge).filter(SessionEdge.edge_kind == "automation_child").one()
        assert edge.visibility == "hidden"
        assert edge.source_thread_id == parent_thread.id
        assert edge.target_thread_id == hatch_thread.id
        assert edge.provider_edge_id == "hatch-run-1"
        assert edge.metadata_json["origin_kind"] == "hatch_automation"

        total, rows = store.list_timeline_thread_page(hide_autonomous=True, include_test=True)
        assert total == 1
        assert rows[0][1] == str(PARENT_ID)

        relaxed_total, relaxed_rows = store.list_timeline_thread_page(hide_autonomous=False, include_test=True)
        assert relaxed_total == 1
        assert relaxed_rows[0][1] == str(PARENT_ID)

        include_total, include_rows = store.list_timeline_thread_page(
            hide_autonomous=False,
            include_automation=True,
            include_test=True,
        )
        assert include_total == 2
        assert {row[1] for row in include_rows} == {str(PARENT_ID), str(HATCH_ID)}

        sessions, total = store.list_sessions(query="unique review", include_test=True, hide_autonomous=True)
        assert total == 0
        assert sessions == []

        sessions, total = store.list_sessions(query="unique review", include_test=True, hide_autonomous=False)
        assert total == 0
        assert sessions == []

        sessions, total = store.list_sessions(
            query="unique review",
            include_test=True,
            hide_autonomous=False,
            include_automation=True,
        )
        assert total == 1
        assert sessions[0].id == HATCH_ID

        store.ingest_session(
            _root_payload(
                session_id=HATCH_ID,
                provider_session_id="ses_hatch_child",
                text="Follow-up plain ingest",
            )
        )
        db.refresh(hatch_session)
        db.refresh(card)
        assert hatch_session.origin_kind == "hatch_automation"
        assert hatch_session.hidden_from_default_timeline == 1
        assert card.origin_kind == "hatch_automation"
        assert card.hidden_from_default_timeline == 1


def test_hatch_automation_hides_from_timeline_api_and_wall_by_default(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(_root_payload())
        store.ingest_session(_hatch_payload())

        wall = query_wall_sessions(db, project="longhouse", days=1, limit=10)
        assert [item.session_id for item in wall] == [str(PARENT_ID)]

        wall_with_automation = query_wall_sessions(db, project="longhouse", days=1, limit=10, include_automation=True)
        assert {item.session_id for item in wall_with_automation} == {str(PARENT_ID), str(HATCH_ID)}

    client, api_ref = _make_client(SessionLocal)
    try:
        default_resp = client.get(
            "/api/timeline/sessions",
            params={"project": "longhouse", "days_back": 1, "limit": 10},
        )
        assert default_resp.status_code == 200
        default_ids = [card["detail"]["id"] for card in default_resp.json()["sessions"]]
        assert default_ids == [str(PARENT_ID)]

        include_resp = client.get(
            "/api/timeline/sessions",
            params={"project": "longhouse", "days_back": 1, "limit": 10, "include_automation": "true"},
        )
        assert include_resp.status_code == 200
        include_ids = {card["detail"]["id"] for card in include_resp.json()["sessions"]}
        assert include_ids == {str(PARENT_ID), str(HATCH_ID)}
    finally:
        api_ref.dependency_overrides = {}


def test_hatch_automation_hides_from_active_sessions_live_path(tmp_path, monkeypatch):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(_root_payload())
        store.ingest_session(_hatch_payload())

    monkeypatch.setattr(
        "zerg.routers.agents_sessions._active_live_session_candidates",
        lambda **_kwargs: [HATCH_ID, PARENT_ID],
    )
    client, api_ref = _make_client(SessionLocal)
    try:
        default_resp = client.get("/api/agents/sessions/active", params={"limit": 10})
        assert default_resp.status_code == 200
        assert [item["id"] for item in default_resp.json()["sessions"]] == [str(PARENT_ID)]

        include_resp = client.get(
            "/api/agents/sessions/active",
            params={"limit": 10, "include_automation": "true"},
        )
        assert include_resp.status_code == 200
        assert {item["id"] for item in include_resp.json()["sessions"]} == {str(PARENT_ID), str(HATCH_ID)}
    finally:
        api_ref.dependency_overrides = {}


def test_hatch_automation_does_not_prepare_attention_push(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        AgentsStore(db).ingest_session(_hatch_payload())

        push = prepare_session_attention_push(
            db,
            owner_id=1,
            session_id=HATCH_ID,
            previous_state="idle",
            current_state="blocked",
            occurred_at=NOW,
            targets=(APNSDeviceTarget(device_token="d" * 64, push_environment="sandbox"),),
        )

        assert push is None
        hatch_session = db.get(AgentSession, HATCH_ID)
        assert hatch_session.last_attention_push_at is None
        assert hatch_session.last_attention_push_state is None


def test_provider_subagent_lineage_wins_when_hatch_origin_is_also_present(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(_root_payload(provider_session_id="ses_parent"))
        parent = db.get(AgentSession, PARENT_ID)
        parent_thread = (
            db.query(SessionThread)
            .filter(SessionThread.session_id == PARENT_ID, SessionThread.is_primary == 1)
            .one()
        )

        result = store.ingest_session(
            _hatch_payload(
                session_id=PROVIDER_CHILD_ID,
                provider_session_id="ses_provider_subagent",
                parent_longhouse_session_id=PARENT_ID,
                parent_thread_id=parent_thread.id,
                parent_provider_session_id="ses_parent",
                is_sidechain=True,
                text="Provider subagent launched through Hatch",
            ).model_copy(
                update={
                    "lineage_kind": "task_child",
                    "subagent_id": "explore",
                    "subagent_tool_use_id": "call_task",
                }
            )
        )

        assert result.session_id == PARENT_ID
        db.refresh(parent)
        assert parent.origin_kind is None
        assert parent.hidden_from_default_timeline == 0
        assert db.get(TimelineCard, PARENT_ID).hidden_from_default_timeline == 0

        child_thread = (
            db.query(SessionThread)
            .filter(SessionThread.session_id == PARENT_ID, SessionThread.branch_kind == "subagent")
            .one()
        )
        assert child_thread.parent_thread_id == parent_thread.id
        assert child_thread.origin_kind == "hatch_automation"
        assert child_thread.hidden_from_default_timeline == 1

        edge_kinds = {edge.edge_kind for edge in db.query(SessionEdge).all()}
        assert {"task_child", "automation_child"} <= edge_kinds

        total, rows = store.list_timeline_thread_page(hide_autonomous=True, include_test=True)
        assert total == 1
        assert rows[0][1] == str(PARENT_ID)


def test_historical_hatch_backfill_reports_candidates_but_only_hides_reviewed_ids(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(
            _root_payload(
                session_id=HATCH_ID,
                provider_session_id="ses_historical_hatch",
                text="Final code review for the Hatch automation origin branch",
            )
        )
        store.ingest_session(
            _root_payload(
                session_id=PARENT_ID,
                provider_session_id="ses_real_user_task",
                text="Build the real user-visible feature",
            )
        )

        report = classify_reviewed_hatch_automation_sessions(db, session_ids=[], apply=False)
        assert [item["session_id"] for item in report.heuristic_candidates] == [str(HATCH_ID)]
        assert db.get(AgentSession, HATCH_ID).hidden_from_default_timeline == 0

        total, rows = store.list_timeline_thread_page(hide_autonomous=False, include_test=True)
        assert total == 2
        assert {row[1] for row in rows} == {str(PARENT_ID), str(HATCH_ID)}

        applied = classify_reviewed_hatch_automation_sessions(db, session_ids=[HATCH_ID], apply=True)
        assert applied.applied_session_ids == [str(HATCH_ID)]

        db.expire_all()
        hatch_session = db.get(AgentSession, HATCH_ID)
        assert hatch_session.origin_kind == "hatch_automation"
        assert hatch_session.hidden_from_default_timeline == 1
        assert db.get(TimelineCard, HATCH_ID).hidden_from_default_timeline == 1
        hatch_thread = (
            db.query(SessionThread)
            .filter(SessionThread.session_id == HATCH_ID, SessionThread.is_primary == 1)
            .one()
        )
        assert hatch_thread.hidden_from_default_timeline == 1

        total, rows = store.list_timeline_thread_page(hide_autonomous=False, include_test=True)
        assert total == 1
        assert rows[0][1] == str(PARENT_ID)

        include_total, include_rows = store.list_timeline_thread_page(
            hide_autonomous=False,
            include_test=True,
            include_automation=True,
        )
        assert include_total == 2
        assert {row[1] for row in include_rows} == {str(PARENT_ID), str(HATCH_ID)}


def test_db_classify_automation_cli_applies_reviewed_session_ids(tmp_path):
    from zerg.cli.main import app as cli_app

    db_path = tmp_path / "hatch-automation-cli.db"
    db_url = f"sqlite:///{db_path}"
    engine = make_engine(db_url).execution_options(schema_translate_map={"agents": None})
    initialize_database(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    with SessionLocal() as db:
        AgentsStore(db).ingest_session(
            _root_payload(
                session_id=HATCH_ID,
                provider_session_id="ses_cli_reviewed_hatch",
                text="Quick phase review for Hatch classification",
            )
        )

    result = CliRunner().invoke(
        cli_app,
        [
            "db",
            "classify-automation",
            "--database-url",
            db_url,
            "--session-id",
            str(HATCH_ID),
            "--apply",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["applied_session_ids"] == [str(HATCH_ID)]
    assert payload["heuristic_candidate_count"] == 0

    with SessionLocal() as db:
        assert db.get(AgentSession, HATCH_ID).origin_kind == "hatch_automation"
        assert db.get(TimelineCard, HATCH_ID).hidden_from_default_timeline == 1
