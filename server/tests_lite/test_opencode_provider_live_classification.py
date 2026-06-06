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
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import SessionIngest
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


def test_opencode_provider_live_canary_reclassifies_existing_machine_environment(tmp_path):
    store, db = _make_store(tmp_path)
    session_id = uuid4()
    cwd = "/Users/davidrose/.longhouse/canaries/provider-live/opencode/20260605T164518Z/workspace"

    store.ingest_session(_ingest_payload(session_id, environment="cinder", cwd=cwd))
    store.ingest_session(_ingest_payload(session_id, environment="test", cwd=cwd))

    session = db.get(AgentSession, session_id)
    assert session is not None
    assert session.environment == "test"


def test_opencode_normal_workspace_does_not_reclassify_machine_environment(tmp_path):
    store, db = _make_store(tmp_path)
    session_id = uuid4()
    cwd = "/Users/davidrose/git/workspace"

    store.ingest_session(_ingest_payload(session_id, environment="cinder", cwd=cwd))
    store.ingest_session(_ingest_payload(session_id, environment="test", cwd=cwd))

    session = db.get(AgentSession, session_id)
    assert session is not None
    assert session.environment == "cinder"
