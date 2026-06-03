"""Startup backfill that repairs ghost device_id labels from a machine rename.

Verifies the generic, tenant-safe normalization in ``_migrate_agents_columns``:
a session whose dead ``device_id`` is not an enrolled device but whose
``environment`` names an enrolled device adopts the enrolled name. Rows where
neither/both sides are enrolled are left untouched.
"""

from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from uuid import uuid4

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from zerg.database import initialize_database  # noqa: E402
from zerg.database import make_engine  # noqa: E402
from zerg.models import User  # noqa: E402
from zerg.models.agents import AgentSession  # noqa: E402
from zerg.models.device_token import DeviceToken  # noqa: E402


def _add_session(db, *, device_id, environment, cwd):
    db.add(
        AgentSession(
            id=uuid4(),
            provider="codex",
            environment=environment,
            device_id=device_id,
            cwd=cwd,
            started_at=datetime.now(timezone.utc),
            needs_embedding=0,
        )
    )


def test_backfill_rewrites_ghost_device_id_to_enrolled(tmp_path):
    db_path = tmp_path / "zerg.db"
    engine = make_engine(f"sqlite:///{db_path}")
    initialize_database(engine)
    SessionLocal = sessionmaker(bind=engine)

    with SessionLocal() as db:
        db.add(User(id=1, email="u1@example.com", role="ADMIN"))
        db.flush()
        db.add(DeviceToken(owner_id=1, device_id="cinder", token_hash="hash-cinder"))
        # Ghost: dead device_id, environment names the enrolled machine -> rewrite.
        _add_session(db, device_id="shipper-laptop", environment="cinder", cwd="/Users/d/git/zerg")
        # Already correct -> untouched.
        _add_session(db, device_id="cinder", environment="cinder", cwd="/Users/d/git/me")
        # Neither side enrolled (normal environment label) -> untouched.
        _add_session(db, device_id="other-box", environment="production", cwd="/Users/d/git/x")
        db.commit()

    # Re-run the migrator (idempotent always-run normalization).
    initialize_database(engine)

    with engine.connect() as conn:
        rows = dict(conn.execute(text("SELECT cwd, device_id FROM sessions")).fetchall())

    assert rows["/Users/d/git/zerg"] == "cinder"  # ghost adopted enrolled name
    assert rows["/Users/d/git/me"] == "cinder"  # unchanged
    assert rows["/Users/d/git/x"] == "other-box"  # unchanged
