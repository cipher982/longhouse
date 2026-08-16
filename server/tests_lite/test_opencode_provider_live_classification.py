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


def _ingest_payload(session_id, *, environment: str, cwd: str) -> SessionIngest:
    return SessionIngest(
        id=session_id,
        provider="opencode",
        environment=environment,
        project="workspace",
        device_id="shipper-cinder",
        cwd=cwd,
        started_at=datetime(2026, 6, 5, tzinfo=timezone.utc),
        provider_session_id="ses_provider_live",
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


def test_provider_noreply_marker_classifies_session_as_test(tmp_path):
    store, db = _make_store(tmp_path)
    session_id = uuid4()
    payload = _ingest_payload(session_id, environment="cinder", cwd="/Users/davidrose/git/workspace")
    payload.events = [_user_event("LONGHOUSE_OPENCODE_NOREPLY_abc123")]

    store.ingest_session(payload, synchronous_projections=False, incremental_session_counts=True)

    session = db.get(AgentSession, session_id)
    card = db.get(TimelineCard, session_id)
    assert session is not None
    assert card is not None
    assert session.environment == "test"
    assert card.environment == "test"
    assert session.first_user_message_preview == "LONGHOUSE_OPENCODE_NOREPLY_abc123"


def test_provider_reply_exact_marker_sets_hidden_canary_origin(tmp_path):
    store, db = _make_store(tmp_path)
    session_id = uuid4()
    payload = _ingest_payload(session_id, environment="cinder", cwd="/Users/davidrose/git/workspace")
    payload.events = [_user_event("Reply exactly LONGHOUSE_OPENCODE_RESUME_SEED_abc123456789 and nothing else.")]

    store.ingest_session(payload, synchronous_projections=False, incremental_session_counts=True)

    session = db.get(AgentSession, session_id)
    card = db.get(TimelineCard, session_id)
    assert session is not None
    assert card is not None
    assert session.environment == "test"
    assert session.origin_kind == "test_or_canary"
    assert session.hidden_from_default_timeline == 1
    assert session.launch_actor == "automation"
    assert session.launch_surface == "test"
    assert card.origin_kind == "test_or_canary"
    assert card.hidden_from_default_timeline == 1


def test_provider_reply_exact_marker_does_not_reclassify_console_origin(tmp_path):
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
    payload = _ingest_payload(session_id, environment="production", cwd="/Users/davidrose/git/workspace")
    payload.events = [_user_event("Reply exactly WARM_IDLE_OK.")]

    store.ingest_session(payload, synchronous_projections=False, incremental_session_counts=True)

    session = db.get(AgentSession, session_id)
    assert session is not None
    assert session.environment == "production"
    assert session.origin_kind == "console"
    assert session.hidden_from_default_timeline == 0


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


def test_historical_reply_exact_marker_is_hidden_by_listing_clause(tmp_path):
    store, db = _make_store(tmp_path)
    session_id = uuid4()
    marker = "Reply exactly LONGHOUSE_OPENCODE_RESUME_SEED_94afb881e8684faca669fefd44ec40 and nothing else."
    payload = _ingest_payload(session_id, environment="cinder", cwd="/Users/davidrose/git/workspace")
    payload.events = [_user_event(marker)]

    store.ingest_session(payload, synchronous_projections=False, incremental_session_counts=True)

    session = db.get(AgentSession, session_id)
    card = db.get(TimelineCard, session_id)
    assert session is not None
    assert card is not None
    session.environment = "cinder"
    session.origin_kind = None
    session.hidden_from_default_timeline = 0
    card.environment = "cinder"
    card.origin_kind = None
    card.hidden_from_default_timeline = 0
    db.commit()

    visible, visible_total = store.list_sessions(include_test=False, hide_autonomous=False)
    assert visible_total == 0
    assert visible == []


def test_historical_bare_reply_exact_marker_is_hidden_by_listing_clause(tmp_path):
    store, db = _make_store(tmp_path)
    session_id = uuid4()
    payload = _ingest_payload(session_id, environment="cinder", cwd="/Users/davidrose/git/workspace")
    payload.events = [_user_event("Reply exactly WARM_IDLE_OK.")]

    store.ingest_session(payload, synchronous_projections=False, incremental_session_counts=True)

    session = db.get(AgentSession, session_id)
    card = db.get(TimelineCard, session_id)
    assert session is not None
    assert card is not None
    session.environment = "cinder"
    session.origin_kind = None
    session.hidden_from_default_timeline = 0
    card.environment = "cinder"
    card.origin_kind = None
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


def test_provider_proof_sessions_are_hidden_by_default_but_visible_with_include_test(tmp_path):
    store, db = _make_store(tmp_path)
    session_id = uuid4()
    payload = _ingest_payload(session_id, environment="cinder", cwd="/Users/davidrose/git/workspace")
    payload.events = [_user_event("LONGHOUSE_OPENCODE_NOREPLY_hidden")]

    store.ingest_session(payload, synchronous_projections=False, incremental_session_counts=True)

    visible, visible_total = store.list_sessions(include_test=False, hide_autonomous=False)
    with_test, with_test_total = store.list_sessions(include_test=True, hide_autonomous=False, include_automation=True)

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
    dogfood timeline. The prompts cannot carry the `_NOREPLY_` marker instead:
    that pattern is anchored at the start of the first user message and these
    prompts open with "Reply with exactly ...".
    """
    from pathlib import Path

    from zerg.qa.cursor_helm_product_e2e import build_arg_parser
    from zerg.services.internal_sessions import classify_provider_proof_environment

    default_workspace = build_arg_parser().get_default("workspace")
    assert isinstance(default_workspace, Path)
    assert (
        classify_provider_proof_environment(
            cwd=str(default_workspace),
            first_user_text="Reply with exactly LONGHOUSE_CURSOR_PRODUCT_ONE_deadbeef",
        )
        == "test"
    )
