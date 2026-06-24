from __future__ import annotations

import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from uuid import uuid4

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("TESTING", "1")

from sqlalchemy.orm import sessionmaker

from zerg.database import Base
from zerg.database import make_engine
from zerg.services.agents.provider_binding_diagnostics import summarize_provider_binding_diagnostics
from zerg.services.session_observations import OBS_KIND_PROVIDER_BINDING_CONFLICT
from zerg.services.session_observations import OBS_KIND_PROVIDER_BINDING_MISSING
from zerg.services.session_observations import SOURCE_DOMAIN_SERVER
from zerg.services.session_observations import record_session_observation

NOW = datetime(2026, 6, 23, 12, 0, 0, tzinfo=timezone.utc)


def _session_factory(tmp_path, name="provider-binding-diagnostics.db"):
    engine = make_engine(f"sqlite:///{tmp_path / name}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed_conflict(db, *, provider_session_id, session_id, existing_thread_id, requested_thread_id, observed_at):
    record_session_observation(
        db,
        observation_id=f"server:provider_binding_conflict:{provider_session_id}:{existing_thread_id}:{requested_thread_id}",
        session_id=session_id,
        thread_id=None,
        runtime_key=None,
        provider="opencode",
        device_id="cinder",
        source_domain=SOURCE_DOMAIN_SERVER,
        source="ingest",
        kind=OBS_KIND_PROVIDER_BINDING_CONFLICT,
        observed_at=observed_at,
        load_observation=False,
        payload={
            "reason": "provider_binding_conflict",
            "provider": "opencode",
            "provider_session_id": provider_session_id,
            "existing_thread_id": str(existing_thread_id),
            "requested_thread_id": str(requested_thread_id),
        },
    )


def _seed_missing(db, *, provider_session_id, session_id, observed_at):
    record_session_observation(
        db,
        observation_id=f"server:provider_binding_missing:opencode:{provider_session_id}:{session_id}",
        session_id=session_id,
        thread_id=None,
        runtime_key=None,
        provider="opencode",
        device_id="cinder",
        source_domain=SOURCE_DOMAIN_SERVER,
        source="ingest",
        kind=OBS_KIND_PROVIDER_BINDING_MISSING,
        observed_at=observed_at,
        load_observation=False,
        payload={
            "reason": "provider_binding_missing",
            "provider": "opencode",
            "provider_session_id": provider_session_id,
            "resolved_session_id": str(session_id),
        },
    )


def test_clean_db_returns_empty_summary(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    db = SessionLocal()
    try:
        summary = summarize_provider_binding_diagnostics(db, now=NOW)
    finally:
        db.close()

    assert summary.total == 0
    assert summary.conflict_count == 0
    assert summary.missing_count == 0
    assert summary.affected_session_ids == []
    assert summary.affected_provider_session_ids == []
    assert summary.most_recent_observed_at is None
    assert summary.samples == []
    assert summary.to_dict()["status"] == "ok"


def test_counts_and_samples_seeded_diagnostics(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    db = SessionLocal()
    conflict_session = uuid4()
    missing_session = uuid4()
    existing_thread = uuid4()
    requested_thread = uuid4()
    try:
        _seed_conflict(
            db,
            provider_session_id="ses_conflict",
            session_id=conflict_session,
            existing_thread_id=existing_thread,
            requested_thread_id=requested_thread,
            observed_at=NOW - timedelta(hours=2),
        )
        _seed_missing(
            db,
            provider_session_id="ses_missing",
            session_id=missing_session,
            observed_at=NOW - timedelta(hours=1),
        )
        db.flush()

        summary = summarize_provider_binding_diagnostics(db, now=NOW)
    finally:
        db.close()

    assert summary.conflict_count == 1
    assert summary.missing_count == 1
    assert summary.total == 2
    assert set(summary.affected_session_ids) == {str(conflict_session), str(missing_session)}
    assert set(summary.affected_provider_session_ids) == {"ses_conflict", "ses_missing"}
    # Ordered observed_at DESC -> the more recent missing row is most_recent.
    assert summary.most_recent_observed_at == (NOW - timedelta(hours=1)).isoformat()

    by_kind = {sample.kind: sample for sample in summary.samples}
    assert by_kind[OBS_KIND_PROVIDER_BINDING_CONFLICT].provider_session_id == "ses_conflict"
    assert by_kind[OBS_KIND_PROVIDER_BINDING_CONFLICT].existing_thread_id == str(existing_thread)
    assert by_kind[OBS_KIND_PROVIDER_BINDING_CONFLICT].requested_thread_id == str(requested_thread)
    assert by_kind[OBS_KIND_PROVIDER_BINDING_MISSING].provider_session_id == "ses_missing"


def test_lookback_window_excludes_old_observations(tmp_path):
    SessionLocal = _session_factory(tmp_path)
    db = SessionLocal()
    try:
        _seed_missing(
            db,
            provider_session_id="ses_old",
            session_id=uuid4(),
            observed_at=NOW - timedelta(days=30),
        )
        db.flush()
        summary = summarize_provider_binding_diagnostics(db, now=NOW)
    finally:
        db.close()

    assert summary.total == 0


def test_local_health_section_omits_orm_and_reports_unavailable(tmp_path, monkeypatch):
    # local-health must use its own guarded sqlite3 path, never the ORM.
    from zerg.services import local_health

    # Point the agent DB path at a non-existent file -> unavailable, never raises.
    missing_dir = tmp_path / "no-such-home"
    result = local_health._collect_provider_binding_diagnostics(missing_dir, now=NOW, fast=False)
    assert result["status"] == "unavailable"

    # Fast path is skipped.
    fast = local_health._collect_provider_binding_diagnostics(missing_dir, now=NOW, fast=True)
    assert fast["status"] == "skipped"


def test_local_health_reader_cutoff_matches_sqlalchemy_storage(tmp_path):
    # Regression: SQLite DateTime is stored as 'YYYY-MM-DD HH:MM:SS.ffffff'
    # (space, no tz). A naive lexical compare against an ISO 'T/+00:00' cutoff
    # dropped valid rows near the boundary. Seed via the ORM (production storage
    # format) at the real agent db path, then read via the raw-sqlite reader.
    from datetime import timedelta

    from zerg.services import local_health
    from zerg.services.longhouse_paths import get_agent_db_path

    base_dir = tmp_path / "home"
    db_path = get_agent_db_path(base_dir)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    db = SessionLocal()
    try:
        # Inside the 7-day window (2 days old) -> must be counted.
        _seed_missing(db, provider_session_id="ses_recent", session_id=uuid4(), observed_at=NOW - timedelta(days=2))
        # Outside the window (30 days old) -> must be excluded.
        _seed_missing(db, provider_session_id="ses_old", session_id=uuid4(), observed_at=NOW - timedelta(days=30))
        db.commit()
    finally:
        db.close()

    result = local_health._collect_provider_binding_diagnostics(base_dir, now=NOW, fast=False)
    assert result["status"] == "ok"
    assert result["missing_count"] == 1
    assert result["affected_provider_session_ids"] == ["ses_recent"]
