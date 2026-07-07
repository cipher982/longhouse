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
    with_test, with_test_total = store.list_sessions(include_test=True, hide_autonomous=False)

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
