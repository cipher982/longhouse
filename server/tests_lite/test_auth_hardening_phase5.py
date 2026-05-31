from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.main import _enforce_single_tenant_startup
from zerg.main import api_app


def _make_db(tmp_path):
    db_path = tmp_path / "auth_hardening_phase5.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def test_agents_routes_allow_missing_device_token_when_auth_disabled(tmp_path):
    session_local = _make_db(tmp_path)

    def override_db():
        with session_local() as db:
            yield db

    api_app.dependency_overrides[get_db] = override_db

    try:
        client = TestClient(api_app)
        response = client.get("/agents/sessions")
        assert response.status_code == 200
        assert response.json()["total"] == 0
    finally:
        api_app.dependency_overrides.clear()


def test_agents_ingest_allows_missing_device_token_when_auth_disabled(tmp_path):
    session_local = _make_db(tmp_path)

    def override_db():
        with session_local() as db:
            yield db

    api_app.dependency_overrides[get_db] = override_db

    try:
        client = TestClient(api_app)
        response = client.post(
            "/agents/ingest",
            json={
                "provider": "claude",
                "environment": "development",
                "project": "provider-smoke",
                "device_id": "auth-disabled-dev",
                "cwd": "/tmp/provider-smoke",
                "started_at": "2026-03-18T00:00:00Z",
                "events": [
                    {
                        "role": "user",
                        "content_text": "seed",
                        "timestamp": "2026-03-18T00:00:01Z",
                        "source_path": "/tmp/provider-smoke.jsonl",
                        "source_offset": 0,
                        "raw_json": "{\"type\":\"user\",\"text\":\"seed\"}",
                    }
                ],
            },
        )

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["events_inserted"] == 1
    finally:
        api_app.dependency_overrides.clear()


def test_internal_calls_require_shared_secret_even_when_auth_disabled():
    from zerg.dependencies.auth import require_internal_call

    request = Request({"type": "http", "headers": []})
    settings = SimpleNamespace(auth_disabled=True, internal_api_secret="test-internal-secret")

    with patch("zerg.dependencies.auth.get_settings", return_value=settings):
        with pytest.raises(Exception) as exc_info:
            require_internal_call(request)

    exc = exc_info.value
    assert getattr(exc, "status_code", None) == 403
    assert getattr(exc, "detail", None) == "Internal endpoint - external access forbidden"


def test_single_tenant_config_requires_explicit_owner_email():
    from zerg.services.single_tenant import validate_single_tenant_config

    # No OWNER_EMAIL and no password auth configured → must fail closed.
    settings = SimpleNamespace(
        single_tenant=True,
        auth_disabled=False,
        admin_emails="admin@example.com",
        longhouse_password="",
        longhouse_password_hash="",
    )

    with (
        patch("zerg.services.single_tenant.get_settings", return_value=settings),
        patch.dict(os.environ, {}, clear=True),
    ):
        error = validate_single_tenant_config()

    assert error is not None
    assert "OWNER_EMAIL" in error


def test_single_tenant_config_allows_password_auth_without_owner_email():
    """B9: password-auth self-hosters may enable auth without OWNER_EMAIL."""
    from zerg.services.single_tenant import validate_single_tenant_config

    settings = SimpleNamespace(
        single_tenant=True,
        auth_disabled=False,
        admin_emails="",
        longhouse_password="",
        longhouse_password_hash="pbkdf2_sha256$600000$abc$def",
    )

    with (
        patch("zerg.services.single_tenant.get_settings", return_value=settings),
        patch.dict(os.environ, {}, clear=True),
    ):
        assert validate_single_tenant_config() is None


def test_single_tenant_startup_fails_fast_on_owner_misconfig():
    app = FastAPI()

    with (
        patch("zerg.lifespan._settings", SimpleNamespace(single_tenant=True, testing=False)),
        patch("zerg.services.single_tenant.validate_single_tenant_config", return_value="OWNER_EMAIL missing"),
    ):
        with pytest.raises(RuntimeError, match="OWNER_EMAIL missing"):
            _enforce_single_tenant_startup(app)

    assert app.state.single_tenant_violation == "OWNER_EMAIL missing"
