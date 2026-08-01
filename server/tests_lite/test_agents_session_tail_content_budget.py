"""HTTP-level tests for the per-event content budget on GET /api/agents/sessions/{id}/tail.

Overrides dependencies on ``api_app`` (not ``app``) per the tests_lite convention.

Why this exists: tail used to slice content at a bare 4000 chars. A real
overnight session's closing message was cut mid-word at exactly that boundary
with nothing in the response to say so, and an agent reading the tail concluded
the archive had lost the text. The archive was intact. Truncation must be
visible and the caller must be able to ask for the rest.
"""

from __future__ import annotations

import os
from datetime import datetime
from datetime import timedelta
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

_TS = datetime(2026, 8, 1, 2, 46, 0, tzinfo=timezone.utc)

# The real message was 4345 chars and the cut landed at 4000, losing 345.
_LONG_TAIL = "and a real question I don't have a confident answer to: should dispatching a new turn clear unread"
_LONG_CONTENT = ("x" * 4000) + _LONG_TAIL


def _setup_app(tmp_path):
    db_path = tmp_path / "test_session_tail_content_budget.db"
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


def _add_session_with_long_final_message(factory):
    with factory() as db:
        sess = AgentSession(id=uuid4(), provider="claude", environment="test", started_at=_TS)
        db.add(sess)
        db.flush()
        db.add(
            AgentEvent(
                session_id=sess.id,
                role="user",
                content_text="short prompt",
                timestamp=_TS,
                raw_json='{"role":"user"}',
            )
        )
        db.add(
            AgentEvent(
                session_id=sess.id,
                role="assistant",
                content_text=_LONG_CONTENT,
                timestamp=_TS + timedelta(seconds=1),
                raw_json='{"role":"assistant"}',
            )
        )
        db.commit()
        return sess.id


def test_over_budget_content_is_annotated_not_silently_cut(tmp_path):
    factory, cleanup = _setup_app(tmp_path)
    session_id = _add_session_with_long_final_message(factory)
    client = TestClient(api_app)
    try:
        resp = client.get(f"/agents/sessions/{session_id}/tail", params={"limit": 10})
        assert resp.status_code == 200
        final = resp.json()["events"][-1]
        assert len(final["content"]) == 4000
        assert final["_content_truncated"] is True
        assert final["_content_full_chars"] == len(_LONG_CONTENT)
    finally:
        cleanup()


def test_raising_the_budget_returns_the_rest(tmp_path):
    """The annotation promises the caller can re-request. It has to be true."""
    factory, cleanup = _setup_app(tmp_path)
    session_id = _add_session_with_long_final_message(factory)
    client = TestClient(api_app)
    try:
        resp = client.get(
            f"/agents/sessions/{session_id}/tail",
            params={"limit": 10, "max_content_chars": 20000},
        )
        assert resp.status_code == 200
        final = resp.json()["events"][-1]
        assert final["content"] == _LONG_CONTENT
        assert final["content"].endswith("clear unread")
        assert "_content_truncated" not in final
    finally:
        cleanup()


def test_under_budget_content_carries_no_annotation(tmp_path):
    factory, cleanup = _setup_app(tmp_path)
    session_id = _add_session_with_long_final_message(factory)
    client = TestClient(api_app)
    try:
        resp = client.get(f"/agents/sessions/{session_id}/tail", params={"limit": 10})
        assert resp.status_code == 200
        first = resp.json()["events"][0]
        assert first["content"] == "short prompt"
        assert "_content_truncated" not in first
        assert "_content_full_chars" not in first
    finally:
        cleanup()
