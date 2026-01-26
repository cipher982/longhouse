"""Tests for internal API authentication (require_internal_call)."""

import pytest
from fastapi.testclient import TestClient

from zerg.config import get_settings
from zerg.dependencies import auth as auth_deps
from zerg.dependencies.auth import require_internal_call
from zerg.main import app


class MockRequest:
    """Mock request for testing auth dependency."""

    def __init__(self, headers: dict[str, str] | None = None):
        self.headers = headers or {}


@pytest.fixture
def client():
    """Test client for internal endpoints."""
    return TestClient(app)


def test_internal_auth_dev_mode_allows_all(monkeypatch):
    """In dev mode (auth disabled), internal calls should be allowed without token."""
    settings = get_settings()
    settings.override(auth_disabled=True)
    monkeypatch.setattr(auth_deps, "get_settings", lambda: settings)

    request = MockRequest(headers={})
    result = require_internal_call(request)
    assert result is True


def test_internal_auth_missing_token(monkeypatch):
    """In production mode, missing token should return 403."""
    settings = get_settings()
    settings.override(auth_disabled=False, internal_api_secret="super-secret-token-123456")
    monkeypatch.setattr(auth_deps, "get_settings", lambda: settings)

    request = MockRequest(headers={})
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        require_internal_call(request)

    assert exc_info.value.status_code == 403
    assert "external access forbidden" in exc_info.value.detail.lower()


def test_internal_auth_wrong_token(monkeypatch):
    """In production mode, wrong token should return 403."""
    settings = get_settings()
    settings.override(auth_disabled=False, internal_api_secret="super-secret-token-123456")
    monkeypatch.setattr(auth_deps, "get_settings", lambda: settings)

    request = MockRequest(headers={"X-Internal-Token": "wrong-token"})
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        require_internal_call(request)

    assert exc_info.value.status_code == 403
    assert "external access forbidden" in exc_info.value.detail.lower()


def test_internal_auth_correct_token(monkeypatch):
    """In production mode, correct token should allow access."""
    settings = get_settings()
    settings.override(auth_disabled=False, internal_api_secret="super-secret-token-123456")
    monkeypatch.setattr(auth_deps, "get_settings", lambda: settings)

    expected_token = settings.internal_api_secret
    request = MockRequest(headers={"X-Internal-Token": expected_token})
    result = require_internal_call(request)
    assert result is True


def test_internal_auth_header_not_spoofable(monkeypatch):
    """X-Forwarded-For header should not grant access (verify fix for spoofing)."""
    settings = get_settings()
    settings.override(auth_disabled=False, internal_api_secret="super-secret-token-123456")
    monkeypatch.setattr(auth_deps, "get_settings", lambda: settings)

    # Try spoofing with X-Forwarded-For
    request = MockRequest(headers={"X-Forwarded-For": "127.0.0.1"})
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        require_internal_call(request)

    assert exc_info.value.status_code == 403
    # Verify the old approach (host-based) is not used
    assert "external access forbidden" in exc_info.value.detail.lower()


def test_internal_endpoint_requires_token(monkeypatch, client, db_session):
    """Internal endpoints should be blocked before business logic runs."""
    settings = get_settings()
    settings.override(auth_disabled=False, internal_api_secret="super-secret-token-123456")
    monkeypatch.setattr(auth_deps, "get_settings", lambda: settings)

    resp = client.post(
        "/api/internal/runs/123/continue",
        json={"job_id": 1, "commis_id": "w1", "status": "success", "result_summary": "ok"},
    )
    assert resp.status_code == 403

    resp_ok = client.post(
        "/api/internal/runs/123/continue",
        headers={"X-Internal-Token": "super-secret-token-123456"},
        json={"job_id": 1, "commis_id": "w1", "status": "success", "result_summary": "ok"},
    )
    assert resp_ok.status_code == 404
