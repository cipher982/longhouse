from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from uuid import uuid4

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from sqlalchemy.orm import sessionmaker

from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.models.agents import AgentSession
from zerg.models.agents import TimelineCard
from zerg.services.agents import AgentsStore
from zerg.services.agents import EventIngest
from zerg.services.agents import SessionIngest
from zerg.services.write_serializer import get_write_serializer


def _make_store(tmp_path):
    db_path = tmp_path / "opencode-provider-live.db"
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)
    factory = sessionmaker(bind=engine)
    get_write_serializer().configure(factory)
    db = factory()
    return AgentsStore(db), db


def _ingest_payload(
    session_id,
    *,
    environment: str,
    cwd: str,
    device_id: str = "shipper-cinder",
    origin_kind: str | None = None,
    launch_actor: str | None = None,
    launch_surface: str | None = None,
) -> SessionIngest:
    return SessionIngest(
        id=session_id,
        provider="opencode",
        environment=environment,
        project="workspace",
        device_id=device_id,
        cwd=cwd,
        started_at=datetime(2026, 6, 5, tzinfo=timezone.utc),
        provider_session_id="ses_provider_live",
        origin_kind=origin_kind,
        launch_actor=launch_actor,
        launch_surface=launch_surface,
    )


def _user_event(text: str, *, source_offset: int = 0) -> EventIngest:
    return EventIngest(
        role="user",
        content_text=text,
        timestamp=datetime(2026, 6, 5, 0, 0, source_offset, tzinfo=timezone.utc),
        source_path="/tmp/provider-live.jsonl",
        source_offset=source_offset,
    )


def test_opencode_provider_live_canary_reclassifies_existing_machine_environment(tmp_path):
    store, db = _make_store(tmp_path)
    session_id = uuid4()
    cwd = "/Users/davidrose/.longhouse/canaries/provider-live/opencode/20260605T164518Z/workspace"

    store.ingest_session(_ingest_payload(session_id, environment="cinder", cwd=cwd))
    store.ingest_session(_ingest_payload(session_id, environment="test", cwd=cwd))

    session = db.get(AgentSession, session_id)
    assert session is not None
    assert session.environment == "test"


def test_opencode_provider_live_canary_classifies_new_session_as_test(tmp_path):
    store, db = _make_store(tmp_path)
    session_id = uuid4()
    cwd = "/Users/davidrose/.longhouse/canaries/provider-live/opencode/20260605T164518Z/workspace"

    store.ingest_session(_ingest_payload(session_id, environment="cinder", cwd=cwd))

    session = db.get(AgentSession, session_id)
    card = db.get(TimelineCard, session_id)
    assert session is not None
    assert card is not None
    assert session.environment == "test"
    assert card.environment == "test"


def test_noreply_marker_text_alone_does_not_classify_session_as_test(tmp_path):
    """Prompt text is not a declaration.

    A harness keeps itself off the user timeline by declaring a test launch
    surface on ingest; writing a marker into the first user message does not.
    The preview is still kept verbatim — the text is content, not a signal.
    """

    store, db = _make_store(tmp_path)
    session_id = uuid4()
    payload = _ingest_payload(session_id, environment="cinder", cwd="/Users/davidrose/git/workspace")
    payload.events = [_user_event("LONGHOUSE_OPENCODE_NOREPLY_abc123")]

    store.ingest_session(payload, synchronous_projections=False, incremental_session_counts=True)

    session = db.get(AgentSession, session_id)
    card = db.get(TimelineCard, session_id)
    assert session is not None
    assert card is not None
    assert session.environment == "cinder"
    assert card.environment == "cinder"
    assert session.origin_kind is None
    assert session.first_user_message_preview == "LONGHOUSE_OPENCODE_NOREPLY_abc123"

    visible, visible_total = store.list_sessions(include_test=False, hide_autonomous=False)
    assert visible_total == 1
    assert [item.id for item in visible] == [session_id]


def test_provider_factory_machine_classifies_user_repo_as_test(tmp_path):
    store, db = _make_store(tmp_path)
    session_id = uuid4()
    payload = _ingest_payload(
        session_id,
        environment="cinder",
        cwd="/Users/davidrose/git/user-repo",
        device_id="provider-factory-resume",
    )
    payload.events = [_user_event("Review the deployment plan")]

    store.ingest_session(payload, synchronous_projections=False, incremental_session_counts=True)

    session = db.get(AgentSession, session_id)
    card = db.get(TimelineCard, session_id)
    assert session is not None
    assert card is not None
    assert session.environment == "test"
    assert session.origin_kind == "test_or_canary"
    assert session.hidden_from_default_timeline == 1
    assert card.hidden_from_default_timeline == 1


def test_declared_canary_origin_sets_hidden_canary_origin(tmp_path):
    """A canary that declares itself stays off the user timeline.

    The declaration travels on the ingest payload (origin_kind plus launch
    provenance); the workspace is an ordinary user repo and the prompt is
    ordinary user text, so the declaration is the only thing doing the work.
    """

    store, db = _make_store(tmp_path)
    session_id = uuid4()
    payload = _ingest_payload(
        session_id,
        environment="cinder",
        cwd="/Users/davidrose/git/workspace",
        origin_kind="test_or_canary",
        launch_actor="automation",
        launch_surface="test",
    )
    payload.events = [_user_event("Run the resume seed check.")]

    store.ingest_session(payload, synchronous_projections=False, incremental_session_counts=True)

    session = db.get(AgentSession, session_id)
    card = db.get(TimelineCard, session_id)
    assert session is not None
    assert card is not None
    assert session.origin_kind == "test_or_canary"
    assert session.hidden_from_default_timeline == 1
    assert session.launch_actor == "automation"
    assert session.launch_surface == "test"
    assert card.origin_kind == "test_or_canary"
    assert card.hidden_from_default_timeline == 1

    visible, visible_total = store.list_sessions(include_test=False, hide_autonomous=False)
    assert visible_total == 0
    assert visible == []


def test_declared_automation_actor_hides_without_reclassifying_console_origin(tmp_path):
    """Automation provenance hides a console run without rewriting its origin.

    Console sessions Longhouse dispatched for a proof still belong to the
    console origin; the automation launch actor is what keeps them out of the
    default timeline.
    """

    store, db = _make_store(tmp_path)
    session_id = uuid4()
    db.add(
        AgentSession(
            id=session_id,
            provider="opencode",
            environment="production",
            project="workspace",
            device_id="shipper-cinder",
            cwd="/Users/davidrose/git/workspace",
            started_at=datetime(2026, 6, 5, tzinfo=timezone.utc),
            origin_kind="console",
            hidden_from_default_timeline=0,
        )
    )
    db.commit()
    payload = _ingest_payload(
        session_id,
        environment="production",
        cwd="/Users/davidrose/git/workspace",
        launch_actor="automation",
        launch_surface="test",
    )
    payload.events = [_user_event("Confirm the warm idle path.")]

    store.ingest_session(payload, synchronous_projections=False, incremental_session_counts=True)

    session = db.get(AgentSession, session_id)
    assert session is not None
    assert session.environment == "production"
    assert session.origin_kind == "console"
    assert session.launch_actor == "automation"
    assert session.launch_surface == "test"

    visible, visible_total = store.list_sessions(include_test=False, hide_autonomous=False)
    assert visible_total == 0
    assert visible == []


def test_malformed_reply_exact_marker_remains_ordinary_user_text(tmp_path):
    store, db = _make_store(tmp_path)
    session_id = uuid4()
    payload = _ingest_payload(session_id, environment="cinder", cwd="/Users/davidrose/git/workspace")
    payload.events = [_user_event("Reply exactly LONGHOUSE_OPENCODE_RESUME_SEED_not-hex and nothing else.")]

    store.ingest_session(payload, synchronous_projections=False, incremental_session_counts=True)

    session = db.get(AgentSession, session_id)
    assert session is not None
    assert session.environment == "cinder"
    assert session.origin_kind is None
    visible, visible_total = store.list_sessions(include_test=False, hide_autonomous=False)
    assert visible_total == 1
    assert [item.id for item in visible] == [session_id]


def test_historical_factory_rows_are_hidden_by_listing_evidence_clause(tmp_path):
    """Rows written before classification existed are still hidden on read.

    Historical sessions carry no stored origin/hidden bits, so the listing
    clause re-derives the decision from durable evidence — here the
    provider-factory machine id the row was ingested with.
    """

    store, db = _make_store(tmp_path)
    session_id = uuid4()
    payload = _ingest_payload(
        session_id,
        environment="cinder",
        cwd="/Users/davidrose/git/user-repo",
        device_id="provider-factory-resume",
    )
    payload.events = [_user_event("Check the resume seed.")]

    store.ingest_session(payload, synchronous_projections=False, incremental_session_counts=True)

    session = db.get(AgentSession, session_id)
    card = db.get(TimelineCard, session_id)
    assert session is not None
    assert card is not None
    session.environment = "cinder"
    session.origin_kind = None
    session.launch_actor = None
    session.launch_surface = None
    session.hidden_from_default_timeline = 0
    card.environment = "cinder"
    card.origin_kind = None
    card.launch_actor = None
    card.launch_surface = None
    card.hidden_from_default_timeline = 0
    db.commit()

    visible, visible_total = store.list_sessions(include_test=False, hide_autonomous=False)
    assert visible_total == 0
    assert visible == []


def test_normal_user_text_about_proof_is_not_classified_as_test(tmp_path):
    store, db = _make_store(tmp_path)
    session_id = uuid4()
    payload = _ingest_payload(session_id, environment="cinder", cwd="/Users/davidrose/git/workspace")
    payload.events = [_user_event("Can you prove the no reply flow works for a real user?")]

    store.ingest_session(payload, synchronous_projections=False, incremental_session_counts=True)

    session = db.get(AgentSession, session_id)
    assert session is not None
    assert session.environment == "cinder"


def test_declared_test_environment_is_hidden_by_default_but_visible_with_include_test(tmp_path):
    """A declared test-scope session is hidden by default and revealed on request.

    Unlike automation/canary evidence, a plain test-scope declaration is the
    one hidden reason an explicit test-scope read discounts, so include_test
    alone brings the row back without also leaking automation traffic.
    """

    store, db = _make_store(tmp_path)
    session_id = uuid4()
    payload = _ingest_payload(session_id, environment="test", cwd="/Users/davidrose/git/workspace")
    payload.events = [_user_event("Exercise the opencode proof path.")]

    store.ingest_session(payload, synchronous_projections=False, incremental_session_counts=True)

    visible, visible_total = store.list_sessions(include_test=False, hide_autonomous=False)
    with_test, with_test_total = store.list_sessions(include_test=True, hide_autonomous=False)

    assert visible_total == 0
    assert visible == []
    assert with_test_total == 1
    assert [str(session.id) for session in with_test] == [str(session_id)]


def test_provider_live_cwd_sessions_are_visible_with_include_test(tmp_path):
    store, db = _make_store(tmp_path)
    session_id = uuid4()
    cwd = "/Users/davidrose/.longhouse/canaries/provider-live/opencode/20260605T164518Z/workspace"

    store.ingest_session(_ingest_payload(session_id, environment="cinder", cwd=cwd))

    visible, visible_total = store.list_sessions(include_test=False, hide_autonomous=False)
    with_test, with_test_total = store.list_sessions(include_test=True, hide_autonomous=False, include_automation=True)

    assert db.get(AgentSession, session_id).environment == "test"
    assert visible_total == 0
    assert visible == []
    assert with_test_total == 1
    assert [str(session.id) for session in with_test] == [str(session_id)]


def test_opencode_normal_workspace_does_not_reclassify_machine_environment(tmp_path):
    store, db = _make_store(tmp_path)
    session_id = uuid4()
    cwd = "/Users/davidrose/git/workspace"

    store.ingest_session(_ingest_payload(session_id, environment="cinder", cwd=cwd))
    store.ingest_session(_ingest_payload(session_id, environment="test", cwd=cwd))

    session = db.get(AgentSession, session_id)
    assert session is not None
    assert session.environment == "cinder"


def test_cursor_product_e2e_default_workspace_is_classified_as_proof_traffic():
    """The cursor Helm product E2E must not ship as ordinary user work.

    Its old default workspace (/tmp/longhouse-cursor-product-e2e) matched none
    of the proof signals, so fourteen canary runs landed at the top of a real
    dogfood timeline. Prompt text carries no classification weight, so the
    harness has to run inside the provider-live canary namespace.
    """
    from pathlib import Path

    from zerg.qa.cursor_helm_product_e2e import build_arg_parser
    from zerg.services.internal_sessions import classify_provider_proof_environment

    default_workspace = build_arg_parser().get_default("workspace")
    assert isinstance(default_workspace, Path)
    assert classify_provider_proof_environment(cwd=str(default_workspace)) == "test"
