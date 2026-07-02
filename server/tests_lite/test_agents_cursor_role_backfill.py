"""HTTP-level tests for POST /api/agents/backfill-cursor-roles.

Overrides dependencies on ``api_app`` (not ``app``) per the tests_lite
convention. Validates the endpoint wraps the service function correctly:
dry_run reports without writing, write repairs rows, and pagination via
after_id terminates.
"""

from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from uuid import uuid4

from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.main import api_app
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession

_INJECTION = (
    "<user_info>\nOS Version: darwin 25.5.0\n\n"
    "<rules>\n<always_applied_workspace_rule>x</...>\n"
    "<agent_transcripts>past</agent_transcripts>"
)
_TS = datetime(2026, 7, 1, 16, 0, 0, tzinfo=timezone.utc)


def _setup_app(tmp_path, monkeypatch):
    db_path = tmp_path / "test_cursor_role_backfill.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    factory = make_sessionmaker(engine)

    def _override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = _override_db
    api_app.dependency_overrides[verify_agents_token] = lambda: None
    api_app.dependency_overrides[require_single_tenant] = lambda: None

    def _cleanup():
        api_app.dependency_overrides.pop(get_db, None)
        api_app.dependency_overrides.pop(verify_agents_token, None)
        api_app.dependency_overrides.pop(require_single_tenant, None)

    return factory, _cleanup


def _add_cursor_session_with_injection(factory):
    with factory() as db:
        sess = AgentSession(id=uuid4(), provider="cursor", environment="test", started_at=_TS)
        db.add(sess)
        db.flush()
        ev = AgentEvent(
            session_id=sess.id,
            role="user",
            content_text=_INJECTION,
            timestamp=_TS,
            raw_json='{"role":"user"}',
        )
        db.add(ev)
        db.commit()
        db.refresh(ev)
        return ev.id


def test_backfill_cursor_roles_dry_run_does_not_write(tmp_path, monkeypatch):
    factory, cleanup = _setup_app(tmp_path, monkeypatch)
    ev_id = _add_cursor_session_with_injection(factory)
    client = TestClient(api_app)
    try:
        resp = client.post(
            "/agents/backfill-cursor-roles",
            params={"dry_run": True, "batch_size": 100},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["dry_run"] is True
        assert body["scanned"] == 1
        assert body["re_roleed"] == 1
        assert body["unwrapped"] == 0

        with factory() as db:
            ev = db.get(AgentEvent, ev_id)
            assert ev.role == "user"  # unchanged
    finally:
        cleanup()


def test_backfill_cursor_roles_write_repairs_rows(tmp_path, monkeypatch):
    factory, cleanup = _setup_app(tmp_path, monkeypatch)
    ev_id = _add_cursor_session_with_injection(factory)
    client = TestClient(api_app)
    try:
        resp = client.post(
            "/agents/backfill-cursor-roles",
            params={"dry_run": False, "batch_size": 100},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["dry_run"] is False
        assert body["re_roleed"] == 1

        with factory() as db:
            ev = db.get(AgentEvent, ev_id)
            assert ev.role == "system"
            assert ev.raw_json == '{"role":"user"}'  # ground truth preserved
    finally:
        cleanup()


def test_backfill_cursor_roles_pagination_terminates(tmp_path, monkeypatch):
    factory, cleanup = _setup_app(tmp_path, monkeypatch)
    # Add 3 leaky rows.
    with factory() as db:
        for _ in range(3):
            sess = AgentSession(id=uuid4(), provider="cursor", environment="test", started_at=_TS)
            db.add(sess)
            db.flush()
            db.add(
                AgentEvent(
                    session_id=sess.id,
                    role="user",
                    content_text=_INJECTION,
                    timestamp=_TS,
                    raw_json='{"role":"user"}',
                )
            )
        db.commit()
    client = TestClient(api_app)
    try:
        after = 0
        total_re_roleed = 0
        for _ in range(10):
            resp = client.post(
                "/agents/backfill-cursor-roles",
                params={"dry_run": False, "batch_size": 1, "after_id": after},
            )
            assert resp.status_code == 200
            body = resp.json()
            if body["scanned"] == 0:
                break
            after = body["last_id"]
            total_re_roleed += body["re_roleed"]
        assert total_re_roleed == 3
    finally:
        cleanup()
