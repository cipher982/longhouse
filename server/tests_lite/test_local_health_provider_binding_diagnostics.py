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
from zerg.services import local_health
from zerg.services.longhouse_paths import get_agent_db_path
from zerg.services.session_observations import OBS_KIND_PROVIDER_BINDING_MISSING
from zerg.services.session_observations import SOURCE_DOMAIN_SERVER
from zerg.services.session_observations import record_session_observation

NOW = datetime(2026, 6, 23, 12, 0, 0, tzinfo=timezone.utc)


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


def test_local_health_reports_unavailable_without_an_agent_db(tmp_path):
    missing_dir = tmp_path / "no-such-home"
    result = local_health._collect_provider_binding_diagnostics(missing_dir, now=NOW)
    assert result["status"] == "unavailable"


def test_local_health_reader_cutoff_matches_sqlalchemy_storage(tmp_path):
    # Regression: SQLite DateTime is stored as 'YYYY-MM-DD HH:MM:SS.ffffff'
    # (space, no tz). A naive lexical compare against an ISO 'T/+00:00' cutoff
    # dropped valid rows near the boundary. Seed via the ORM (production storage
    # format) at the real agent db path, then read via the raw-sqlite reader.
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
        # On the cutoff date, one hour inside the window. Stored as
        # 'YYYY-MM-DD HH:MM:SS'; a lexical compare against the ISO cutoff
        # ('YYYY-MM-DDTHH:MM:SS+00:00') sorts ' ' before 'T' and drops it.
        _seed_missing(
            db,
            provider_session_id="ses_boundary",
            session_id=uuid4(),
            observed_at=NOW - timedelta(days=7) + timedelta(hours=1),
        )
        # Outside the window (30 days old) -> must be excluded.
        _seed_missing(db, provider_session_id="ses_old", session_id=uuid4(), observed_at=NOW - timedelta(days=30))
        db.commit()
    finally:
        db.close()

    result = local_health._collect_provider_binding_diagnostics(base_dir, now=NOW)
    assert result["status"] == "ok"
    assert result["missing_count"] == 2
    assert sorted(result["affected_provider_session_ids"]) == ["ses_boundary", "ses_recent"]
