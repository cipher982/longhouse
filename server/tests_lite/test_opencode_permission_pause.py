"""The OpenCode plugin emits a pause_request runtime event for permission.asked;
the server must ingest it as an answerable permission_prompt pause request with
the managed-push reply transport (so Phase 2 dispatch pushes the answer back via
the bridge)."""

from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-opencode-perm")
os.environ.setdefault("INTERNAL_API_SECRET", Fernet.generate_key().decode())

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSession
from zerg.services.session_pause_requests import is_pull_reply_transport
from zerg.services.session_pause_requests import is_user_facing_pause_request
from zerg.services.session_pause_requests import load_active_pause_request_for_session
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import ingest_runtime_events


def _make_db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'opencode_perm.db'}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed(db):
    session = AgentSession(
        provider="opencode",
        environment="test",
        project="opencode-perm",
        started_at=datetime.now(timezone.utc),
        user_messages=1,
        assistant_messages=1,
    )
    db.add(session)
    db.flush()
    db.refresh(session)
    return session


def test_opencode_permission_asked_becomes_answerable_push_pause_request(tmp_path):
    SF = _make_db(tmp_path)
    with SF() as db:
        session = _seed(db)
        runtime_key = f"opencode:{session.id}"
        # Mirror exactly what the embedded opencode plugin emits for permission.asked.
        ingest_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session.id,
                    provider="opencode",
                    device_id="cinder",
                    source="opencode_event",
                    kind="pause_request",
                    occurred_at=datetime.now(timezone.utc),
                    dedupe_key="oc-perm-1",
                    payload={
                        "request_id": "perm-abc",
                        "provider_request_id": "perm-abc",
                        "kind": "permission_prompt",
                        "can_respond": True,
                        "provider_ref": {
                            "source": "opencode_bridge",
                            "reply_transport": "managed_push",
                            "opencode_request_id": "perm-abc",
                        },
                        "tool_name": "bash",
                        "title": "Permission: bash",
                        "summary": "OpenCode wants to use bash",
                    },
                )
            ],
        )
        db.commit()

        row = load_active_pause_request_for_session(db, session.id)
        assert row is not None
        assert row.kind == "permission_prompt"
        assert row.can_respond is True
        assert row.provider_request_id == "perm-abc"
        assert is_user_facing_pause_request(row) is True
        # OpenCode answers PUSH over the bridge — must NOT resolve in place.
        assert is_pull_reply_transport(row) is False
        assert (row.provider_ref_json or {}).get("reply_transport") == "managed_push"
