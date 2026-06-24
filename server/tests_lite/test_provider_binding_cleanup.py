from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from uuid import uuid4

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("TESTING", "1")

from sqlalchemy.orm import sessionmaker

from zerg.database import Base
from zerg.database import make_engine
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionThread
from zerg.services.agents.provider_binding_cleanup import detect_duplicate_sessions_by_provider_binding
from zerg.services.session_observations import OBS_KIND_PROVIDER_BINDING_CONFLICT
from zerg.services.session_observations import SOURCE_DOMAIN_SERVER
from zerg.services.session_observations import record_session_observation

NOW = datetime(2026, 6, 23, 12, 0, 0, tzinfo=timezone.utc)


def _session_factory(tmp_path, name="provider-binding-cleanup.db"):
    engine = make_engine(f"sqlite:///{tmp_path / name}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _make_session_with_thread(db, provider="opencode"):
    session = AgentSession(id=uuid4(), provider=provider, environment="prod", started_at=NOW)
    db.add(session)
    db.flush()
    thread = SessionThread(session_id=session.id, provider=provider, branch_kind="root", is_primary=1)
    db.add(thread)
    db.flush()
    return session, thread


def _seed_conflict(db, *, provider_session_id, session_id, existing_thread_id, requested_thread_id):
    record_session_observation(
        db,
        observation_id=f"server:provider_binding_conflict:opencode:{provider_session_id}:{existing_thread_id}:{requested_thread_id}",
        session_id=session_id,
        thread_id=None,
        runtime_key=None,
        provider="opencode",
        device_id="cinder",
        source_domain=SOURCE_DOMAIN_SERVER,
        source="ingest",
        kind=OBS_KIND_PROVIDER_BINDING_CONFLICT,
        observed_at=NOW,
        load_observation=False,
        payload={
            "reason": "provider_binding_conflict",
            "provider": "opencode",
            "provider_session_id": provider_session_id,
            "existing_thread_id": str(existing_thread_id),
            "requested_thread_id": str(requested_thread_id),
        },
    )


def test_clean_db_returns_no_candidates(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    db = SessionLocal()
    try:
        groups = detect_duplicate_sessions_by_provider_binding(db)
    finally:
        db.close()
    assert groups == []


def test_conflict_across_two_sessions_is_flagged(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    db = SessionLocal()
    try:
        session_a, thread_a = _make_session_with_thread(db)
        session_b, thread_b = _make_session_with_thread(db)
        _seed_conflict(
            db,
            provider_session_id="ses_split",
            session_id=session_a.id,
            existing_thread_id=thread_a.id,
            requested_thread_id=thread_b.id,
        )
        db.flush()
        groups = detect_duplicate_sessions_by_provider_binding(db)
    finally:
        db.close()

    assert len(groups) == 1
    group = groups[0]
    assert group.provider == "opencode"
    assert group.provider_session_id == "ses_split"
    assert set(group.session_ids) == {str(session_a.id), str(session_b.id)}
    assert set(group.thread_ids) == {str(thread_a.id), str(thread_b.id)}


def test_conflict_resolved_to_single_session_is_not_flagged(tmp_path):
    # Both competing threads now live under the SAME session (already converged),
    # or the loser thread no longer exists -> not a split row, do not flag.
    SessionLocal = _session_factory(tmp_path)
    db = SessionLocal()
    try:
        session_a, thread_a = _make_session_with_thread(db)
        # Second thread attached to the same session (converged).
        thread_b = SessionThread(session_id=session_a.id, provider="opencode", branch_kind="root", is_primary=0)
        db.add(thread_b)
        db.flush()
        _seed_conflict(
            db,
            provider_session_id="ses_converged",
            session_id=session_a.id,
            existing_thread_id=thread_a.id,
            requested_thread_id=thread_b.id,
        )
        db.flush()
        groups = detect_duplicate_sessions_by_provider_binding(db)
    finally:
        db.close()

    assert groups == []


def test_conflict_with_vanished_thread_is_not_flagged(tmp_path):
    # The migration may have deleted the loser thread; with only one surviving
    # session the detector stays quiet.
    SessionLocal = _session_factory(tmp_path)
    db = SessionLocal()
    try:
        session_a, thread_a = _make_session_with_thread(db)
        vanished_thread_id = uuid4()
        _seed_conflict(
            db,
            provider_session_id="ses_half",
            session_id=session_a.id,
            existing_thread_id=thread_a.id,
            requested_thread_id=vanished_thread_id,
        )
        db.flush()
        groups = detect_duplicate_sessions_by_provider_binding(db)
    finally:
        db.close()

    assert groups == []
