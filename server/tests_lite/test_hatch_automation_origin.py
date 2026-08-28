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

import pytest

from tests_lite.live_catalog_harness import live_catalog  # noqa: F401
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: F401
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
from zerg.services.agents.automation_backfill import reconcile_legacy_session_visibility
from zerg.services.apns_sender import APNSDeviceTarget
from zerg.services.apns_sender import prepare_session_attention_push


@pytest.fixture(autouse=True)
def _restore_api_app_dependency_overrides():
    """An override installed here must not outlive this test.

    ``api_app`` is a process-global, so an override left behind keeps
    answering for every later test in the run. This file used to leave
    ``verify_agents_token`` returning device ``usage-stats``, and an unrelated
    storage-v2 test several hundred tests later failed with
    ``identity_mismatch``. Nothing catches that until an edit elsewhere
    reorders the suite, so each test puts back what it found.
    """

    from zerg.main import api_app

    saved = dict(api_app.dependency_overrides)
    try:
        yield
    finally:
        api_app.dependency_overrides.clear()
        api_app.dependency_overrides.update(saved)


# These fixtures exercise live timeline and wall windows, so keep their
# activity anchored to the current test run instead of a date that ages out.
NOW = datetime.now(timezone.utc)
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


def test_visibility_reconcile_evaluates_all_rows_and_preserves_raw_facts(tmp_path):
    SessionLocal = _session_factory(tmp_path, "visibility-reconcile.db")
    prompt = "Hatch execution contract:\nThis is a single bounded, non-interactive run. A human is waiting for a useful answer."
    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(_root_payload(text=prompt))
        session = db.get(AgentSession, PARENT_ID)
        session.hidden_from_default_timeline = 0
        session.launch_actor = "user"
        session.launch_surface = "terminal"
        session.origin_kind = None
        card = db.get(TimelineCard, PARENT_ID)
        card.hidden_from_default_timeline = 0
        primary = db.query(SessionThread).filter(SessionThread.session_id == PARENT_ID, SessionThread.is_primary == 1).one()
        primary.hidden_from_default_timeline = 0
        db.commit()

        preview = reconcile_legacy_session_visibility(db, apply=False)
        assert preview.evaluated == 1
        assert preview.actionable_session_ids == [str(PARENT_ID)]
        assert db.get(AgentSession, PARENT_ID).hidden_from_default_timeline == 0

        applied = reconcile_legacy_session_visibility(db, apply=True)
        assert applied.actionable_session_ids == [str(PARENT_ID)]
        db.refresh(session)
        assert session.hidden_from_default_timeline == 1
        assert session.launch_actor == "user"
        assert session.launch_surface == "terminal"
        assert session.origin_kind is None

        converged = reconcile_legacy_session_visibility(db, apply=False)
        assert converged.actionable_session_ids == []


def test_hatch_automation_ingest_persists_sticky_hidden_origin_and_edge(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(_root_payload())
        parent_thread = db.query(SessionThread).filter(SessionThread.session_id == PARENT_ID, SessionThread.is_primary == 1).one()

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

        hatch_thread = db.query(SessionThread).filter(SessionThread.session_id == HATCH_ID, SessionThread.is_primary == 1).one()
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


def test_hatch_execution_contract_ingest_recovers_missing_origin_metadata(tmp_path):
    SessionLocal = _session_factory(tmp_path, name="hatch-contract-origin.db")
    contract = "Hatch execution contract:\nThis is a single bounded, non-interactive run. A human is waiting for a useful answer."
    with SessionLocal() as db:
        store = AgentsStore(db)
        initial_payload = _root_payload(
            session_id=HATCH_ID,
            provider_session_id="cursor-hatch-contract",
            text="ordinary imported session",
        ).model_copy(update={"provider": "cursor", "origin_kind": None})
        store.ingest_session(initial_payload)
        contract_event = initial_payload.events[0].model_copy(update={"content_text": contract})
        store.ingest_session(initial_payload.model_copy(update={"events": [contract_event]}))

        session = db.get(AgentSession, HATCH_ID)
        card = db.get(TimelineCard, HATCH_ID)
        assert session.origin_kind == "hatch_automation"
        assert session.hidden_from_default_timeline == 1
        assert session.launch_actor == "automation"
        assert session.launch_surface == "hatch"
        assert card.origin_kind == "hatch_automation"
        assert card.hidden_from_default_timeline == 1


LIVE_OWNER_EMAIL = "owner@hatch-origin.test"
LIVE_DEVICE_ID = "cinder"


def _ship_live_session(
    live_catalog,
    live_catalog_client,
    token: str,
    *,
    session_id: UUID,
    text: str,
    origin_kind: str,
    hidden: bool,
) -> None:
    """Ship one transcript the way a Machine Agent ships it.

    Origin and the hidden bit it implies travel in the session facts of the
    envelope, so the catalog decides visibility from what the machine
    declared rather than from anything the reader passes.
    """

    body = live_catalog.envelope_body(
        session_id=session_id,
        device_id=LIVE_DEVICE_ID,
        texts=(text,),
        project="longhouse",
    )
    body["session"]["origin_kind"] = origin_kind
    body["session"]["hidden_from_default_timeline"] = hidden
    shipped = live_catalog_client.post(
        "/agents/storage/v2/envelopes",
        json=body,
        headers={"X-Agents-Token": token, "X-Longhouse-Storage-Lane": "live"},
    )
    assert shipped.status_code == 200, shipped.text


def test_hatch_automation_hides_from_timeline_by_default(live_catalog, live_catalog_client):
    """Hatch automation stays out of the default timeline; the flag reveals it.

    The wall query this test also drove is gone from every served path. What
    remains is the timeline, and it is a catalogd snapshot: ``include_automation``
    is a parameter of the catalog read, matched against the origin the shipping
    machine declared.
    """

    owner_id = live_catalog.create_user(LIVE_OWNER_EMAIL)
    token = live_catalog.create_device_token(owner_id=owner_id, device_id=LIVE_DEVICE_ID)
    cookies = {"longhouse_session": live_catalog.browser_cookie(owner_id=owner_id, email=LIVE_OWNER_EMAIL)}

    _ship_live_session(
        live_catalog,
        live_catalog_client,
        token,
        session_id=PARENT_ID,
        text="Parent user task",
        origin_kind="shadow",
        hidden=False,
    )
    _ship_live_session(
        live_catalog,
        live_catalog_client,
        token,
        session_id=HATCH_ID,
        text="Hatch automation unique review",
        origin_kind="hatch_automation",
        hidden=True,
    )

    params = {"project": "longhouse", "days_back": 1, "limit": 10}
    default_resp = live_catalog_client.get("/timeline/sessions", params=params, cookies=cookies)
    assert default_resp.status_code == 200, default_resp.text
    assert [card["detail"]["id"] for card in default_resp.json()["sessions"]] == [str(PARENT_ID)]

    include_resp = live_catalog_client.get(
        "/timeline/sessions",
        params={**params, "include_automation": "true"},
        cookies=cookies,
    )
    assert include_resp.status_code == 200, include_resp.text
    include_ids = {card["detail"]["id"] for card in include_resp.json()["sessions"]}
    assert include_ids == {str(PARENT_ID), str(HATCH_ID)}


def test_test_or_canary_origin_hides_from_default_timeline(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    with SessionLocal() as db:
        store = AgentsStore(db)
        store.ingest_session(_root_payload())
        store.ingest_session(
            _root_payload(
                session_id=HATCH_ID,
                provider_session_id="ses_codex_probe",
                text="What is 5+5? Reply with just the number.",
            ).model_copy(update={"origin_kind": "test_or_canary"})
        )

        probe_session = db.get(AgentSession, HATCH_ID)
        assert probe_session.origin_kind == "test_or_canary"
        assert probe_session.hidden_from_default_timeline == 1
        assert db.get(TimelineCard, HATCH_ID).hidden_from_default_timeline == 1

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
        parent_thread = db.query(SessionThread).filter(SessionThread.session_id == PARENT_ID, SessionThread.is_primary == 1).one()

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

        child_thread = db.query(SessionThread).filter(SessionThread.session_id == PARENT_ID, SessionThread.branch_kind == "subagent").one()
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
        assert hatch_session.launch_actor == "automation"
        assert hatch_session.launch_surface == "hatch"
        assert db.get(TimelineCard, HATCH_ID).hidden_from_default_timeline == 1
        assert db.get(TimelineCard, HATCH_ID).launch_actor == "automation"
        assert db.get(TimelineCard, HATCH_ID).launch_surface == "hatch"
        hatch_thread = db.query(SessionThread).filter(SessionThread.session_id == HATCH_ID, SessionThread.is_primary == 1).one()
        assert hatch_thread.hidden_from_default_timeline == 1

        hatch_session.launch_actor = None
        hatch_session.launch_surface = None
        db.commit()
        repaired = classify_reviewed_hatch_automation_sessions(db, session_ids=[HATCH_ID], apply=True)
        assert repaired.applied_session_ids == [str(HATCH_ID)]
        db.refresh(hatch_session)
        assert hatch_session.launch_actor == "automation"
        assert hatch_session.launch_surface == "hatch"

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


def test_db_reconcile_visibility_cli_reports_every_session_without_mutation(tmp_path):
    from zerg.cli.main import app as cli_app

    db_path = tmp_path / "visibility-reconcile-cli.db"
    db_url = f"sqlite:///{db_path}"
    engine = make_engine(db_url).execution_options(schema_translate_map={"agents": None})
    initialize_database(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    with SessionLocal() as db:
        AgentsStore(db).ingest_session(
            _root_payload(
                session_id=HATCH_ID,
                text=(
                    "Hatch execution contract:\n"
                    "This is a single bounded, non-interactive run. A human is waiting for a useful answer."
                ),
            )
        )
        session = db.get(AgentSession, HATCH_ID)
        session.hidden_from_default_timeline = 0
        db.get(TimelineCard, HATCH_ID).hidden_from_default_timeline = 0
        db.commit()

    result = CliRunner().invoke(
        cli_app,
        ["db", "reconcile-session-visibility", "--database-url", db_url, "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["evaluated"] == 1
    assert payload["actionable_session_ids"] == [str(HATCH_ID)]
    with SessionLocal() as db:
        assert db.get(AgentSession, HATCH_ID).hidden_from_default_timeline == 0


def test_db_classify_automation_cli_applies_reviewed_test_or_canary_ids(tmp_path):
    from zerg.cli.main import app as cli_app

    db_path = tmp_path / "test-canary-cli.db"
    db_url = f"sqlite:///{db_path}"
    engine = make_engine(db_url).execution_options(schema_translate_map={"agents": None})
    initialize_database(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    with SessionLocal() as db:
        AgentsStore(db).ingest_session(
            _root_payload(
                session_id=HATCH_ID,
                provider_session_id="ses_cli_reviewed_probe",
                text="What is the largest planet? Reply with just the planet name.",
            )
        )

    result = CliRunner().invoke(
        cli_app,
        [
            "db",
            "classify-automation",
            "--database-url",
            db_url,
            "--origin-kind",
            "test_or_canary",
            "--session-id",
            str(HATCH_ID),
            "--apply",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["origin_kind"] == "test_or_canary"
    assert payload["applied_session_ids"] == [str(HATCH_ID)]

    with SessionLocal() as db:
        assert db.get(AgentSession, HATCH_ID).origin_kind == "test_or_canary"
        assert db.get(TimelineCard, HATCH_ID).hidden_from_default_timeline == 1
