"""Provider-native session ids resolve on the agents read surface.

Overrides dependencies on ``api_app`` (not ``app``) per the tests_lite convention.

Why this exists: the one id a user actually possesses is the provider-native one
(``claude --resume <uuid>``). Longhouse mints its own session ids for managed
launches, and until this fix no read path accepted the native id — the recovery
workflow 404'd on the only id the user had. These tests pin the resolve-then-404
contract: primary key first, ``provider_session_id`` thread alias second.
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
from zerg.services.agents.session_graph_writes import ensure_primary_thread
from zerg.services.agents.session_graph_writes import record_thread_alias
from zerg.services.session_kernel_projection import resolve_session_id_by_provider_session_id

_TS = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)


def _setup_app(tmp_path):
    db_path = tmp_path / "test_native_id_resolution.db"
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


def _add_helm_session(factory, *, native_id: str):
    """A managed-launch-shaped session: Longhouse id != provider-native id."""
    with factory() as db:
        sess = AgentSession(id=uuid4(), provider="claude", environment="test", started_at=_TS)
        db.add(sess)
        db.flush()
        db.add(
            AgentEvent(
                session_id=sess.id,
                role="user",
                content_text="run the migration and tell me if anything breaks",
                timestamp=_TS,
                raw_json='{"role":"user"}',
            )
        )
        thread = ensure_primary_thread(db, sess)
        record_thread_alias(
            db,
            thread=thread,
            provider="claude",
            alias_kind="provider_session_id",
            alias_value=native_id,
        )
        db.commit()
        return sess.id


def test_tail_resolves_provider_native_id(tmp_path):
    factory, cleanup = _setup_app(tmp_path)
    native_id = str(uuid4())
    longhouse_id = _add_helm_session(factory, native_id=native_id)
    client = TestClient(api_app)
    try:
        resp = client.get(f"/agents/sessions/{native_id}/tail", params={"roles": "user,assistant"})
        assert resp.status_code == 200
        payload = resp.json()
        # The response teaches the caller the canonical id.
        assert payload["session_id"] == str(longhouse_id)
        assert payload["events"][0]["content"].startswith("run the migration")
    finally:
        cleanup()


def test_get_session_resolves_native_id_and_carries_it_in_the_body(tmp_path):
    factory, cleanup = _setup_app(tmp_path)
    native_id = str(uuid4())
    longhouse_id = _add_helm_session(factory, native_id=native_id)
    client = TestClient(api_app)
    try:
        resp = client.get(f"/agents/sessions/{native_id}")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["id"] == str(longhouse_id)
        assert payload["provider_session_id"] == native_id
        assert resp.headers.get("X-Provider-Session-ID") == native_id
    finally:
        cleanup()


def test_export_resolves_provider_native_id(tmp_path):
    factory, cleanup = _setup_app(tmp_path)
    native_id = str(uuid4())
    _add_helm_session(factory, native_id=native_id)
    client = TestClient(api_app)
    try:
        resp = client.get(f"/agents/sessions/{native_id}/export")
        assert resp.status_code == 200
        assert resp.headers.get("X-Provider-Session-ID") == native_id
    finally:
        cleanup()


def test_unknown_id_still_404s(tmp_path):
    factory, cleanup = _setup_app(tmp_path)
    _add_helm_session(factory, native_id=str(uuid4()))
    client = TestClient(api_app)
    try:
        stranger = uuid4()
        assert client.get(f"/agents/sessions/{stranger}/tail").status_code == 404
        assert client.get(f"/agents/sessions/{stranger}").status_code == 404
    finally:
        cleanup()


def test_primary_key_wins_over_a_colliding_alias(tmp_path):
    """A Longhouse id that appears as another session's alias must never reroute."""
    factory, cleanup = _setup_app(tmp_path)
    with factory() as db:
        session_a = AgentSession(id=uuid4(), provider="claude", environment="test", started_at=_TS)
        db.add(session_a)
        db.flush()
        db.add(
            AgentEvent(
                session_id=session_a.id,
                role="user",
                content_text="session A content",
                timestamp=_TS,
                raw_json='{"role":"user"}',
            )
        )
        a_id = session_a.id
        db.commit()
    # Session B claims A's Longhouse id as its provider-native alias.
    b_id = _add_helm_session(factory, native_id=str(a_id))
    client = TestClient(api_app)
    try:
        resp = client.get(f"/agents/sessions/{a_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == str(a_id), "PK lookup must win over the alias"
        # The alias still resolves for a direct resolver call.
        with factory() as db:
            assert resolve_session_id_by_provider_session_id(db, str(a_id)) == b_id
    finally:
        cleanup()


def test_resolver_handles_blank_and_missing_values(tmp_path):
    factory, cleanup = _setup_app(tmp_path)
    try:
        with factory() as db:
            assert resolve_session_id_by_provider_session_id(db, None) is None
            assert resolve_session_id_by_provider_session_id(db, "  ") is None
            assert resolve_session_id_by_provider_session_id(db, str(uuid4())) is None
    finally:
        cleanup()
