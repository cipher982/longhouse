"""The access log must never write a working share token to disk.

A share token is the entire authority to read a shared transcript, and it
travels in the URL path. The access log records the path of every request, so
an unredacted line hands anyone with log access a live credential — including
whoever ships those logs somewhere else.

This drives the real outer app (the middleware is installed there, not on
``api_app``) so the assertions cover the wire paths as the middleware sees them.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from unittest.mock import patch
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ["JWT_SECRET"] = "lh-access-log-tests-secret"
os.environ.setdefault("INTERNAL_API_SECRET", "lh-test-internal")
os.environ.setdefault("GOOGLE_CLIENT_ID", "lh-test-google-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "lh-test-google-client")

import zerg.dependencies.auth as auth_deps  # noqa: E402
from zerg.auth.session_tokens import SESSION_COOKIE_NAME  # noqa: E402
from zerg.auth.session_tokens import SESSION_TOKEN_KIND  # noqa: E402
from zerg.auth.session_tokens import _encode_jwt  # noqa: E402
from zerg.database import Base  # noqa: E402
from zerg.database import get_db  # noqa: E402
from zerg.database import make_engine  # noqa: E402
from zerg.database import make_sessionmaker  # noqa: E402
from zerg.dependencies.agents_auth import require_single_tenant  # noqa: E402
from zerg.main import api_app  # noqa: E402
from zerg.main import app  # noqa: E402
from zerg.models import User  # noqa: E402
from zerg.models.agents import AgentSession  # noqa: E402
from zerg.models.agents import SessionInput  # noqa: E402
from zerg.services.session_hot_cards import upsert_timeline_card_from_session  # noqa: E402
from zerg.services.session_workspace import get_legacy_workspace_session_factory  # noqa: E402

auth_deps.JWT_SECRET = "lh-access-log-tests-secret"

ACCESS_LOGGER = "zerg.middleware.access_log"


def _make_db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'access_log_shares.db'}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed(session_local) -> str:
    with session_local() as db:
        db.add(User(id=1, email="viewer@example.com", display_name="Viewer", role="USER"))
        db.add(User(id=2, email="sharer@example.com", display_name="Sharer", role="USER"))
        db.commit()
        session = AgentSession(
            id=uuid4(),
            provider="codex",
            environment="development",
            project="access-log",
            device_name="cinder",
            summary_title="Redaction Test",
            cwd="/tmp/access-log",
            started_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            ended_at=None,
            user_messages=1,
            assistant_messages=1,
            tool_calls=0,
        )
        db.add(session)
        db.flush()
        upsert_timeline_card_from_session(db, session)
        db.add(
            SessionInput(
                session_id=session.id,
                owner_id=2,
                body="shareable prompt",
                intent="auto",
                status="delivered",
            )
        )
        db.commit()
        return str(session.id)


def _cookie(user_id: int) -> str:
    return _encode_jwt(
        {"sub": str(user_id), "typ": SESSION_TOKEN_KIND, "exp": int(time.time()) + 300},
        auth_deps.get_settings().jwt_secret,
    )


def test_access_log_redacts_the_share_token_from_every_path_that_carries_it(tmp_path, caplog):
    session_local = _make_db(tmp_path)
    session_id = _seed(session_local)

    api_app.dependency_overrides.clear()

    def override_db():
        with session_local() as db:
            yield db

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[get_legacy_workspace_session_factory] = lambda: session_local
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    client = TestClient(app)

    try:
        auth_deps._strategy_cache.clear()
        with patch.object(auth_deps, "AUTH_DISABLED", False):
            client.cookies.set(SESSION_COOKIE_NAME, _cookie(2))
            created = client.post(
                f"/api/timeline/sessions/{session_id}/shares",
                json={"note": None, "expires_in_days": 30},
            )
            assert created.status_code == 200, created.text
            token = created.json()["token"]

            with caplog.at_level(logging.INFO, logger=ACCESS_LOGGER):
                preview = client.get(f"/api/public/session-shares/{token}/preview")
                client.cookies.set(SESSION_COOKIE_NAME, _cookie(1))
                resolved = client.get(f"/api/timeline/session-shares/{token}/resolve")
                # The share URL a recipient actually clicks: the SPA landing
                # page carries the same token in the same position.
                client.get(f"/share/{token}")

        assert preview.status_code == 200, preview.text
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["session_id"] == session_id

        lines = [record.getMessage() for record in caplog.records if record.name == ACCESS_LOGGER]
        assert len(lines) >= 3, lines
        # The credential itself is gone, and the line still says which route
        # was read so the log keeps its forensic value.
        assert not any(token in line for line in lines)
        assert not any(token in str(record.__dict__) for record in caplog.records)
        assert "GET /api/public/session-shares/[redacted]/preview 200" in lines
        assert "GET /api/timeline/session-shares/[redacted]/resolve 200" in lines
        assert any(line.startswith("GET /share/[redacted] ") for line in lines)
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()
