"""Tests for runtime SSO key fetching (services/sso_keys.py).

Covers:
- Cache returns env var fallback when no CONTROL_PLANE_URL
- Cache TTL expiry triggers refetch
- Stale cache used on fetch failure
- accept_token validates with CP-fetched keys (HTTP-level)

Uses in-memory SQLite with inline setup (no shared conftest).
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from zerg.services import sso_keys as sso_keys_mod


@pytest.fixture(autouse=True)
def _reset_cache():
    """Reset module-level cache between tests."""
    sso_keys_mod._reset_cache()
    yield
    sso_keys_mod._reset_cache()


# ---------------------------------------------------------------------------
# Unit tests: get_sso_keys()
# ---------------------------------------------------------------------------


def test_fallback_to_env_var_when_no_cp_url():
    """When CONTROL_PLANE_URL is unset, return CONTROL_PLANE_JWT_SECRET."""
    mock_settings = MagicMock()
    mock_settings.control_plane_url = None
    mock_settings.control_plane_jwt_secret = "env-secret-123"

    with patch.object(sso_keys_mod, "get_settings", return_value=mock_settings):
        keys = sso_keys_mod.get_sso_keys()

    assert keys == ["env-secret-123"]


def test_empty_when_no_cp_url_and_no_env_var():
    """When neither CONTROL_PLANE_URL nor CONTROL_PLANE_JWT_SECRET is set."""
    mock_settings = MagicMock()
    mock_settings.control_plane_url = None
    mock_settings.control_plane_jwt_secret = None

    with patch.object(sso_keys_mod, "get_settings", return_value=mock_settings):
        keys = sso_keys_mod.get_sso_keys()

    assert keys == []


def test_fetches_from_cp_on_first_call():
    """First call with CONTROL_PLANE_URL fetches from CP."""
    mock_settings = MagicMock()
    mock_settings.control_plane_url = "https://control.example.com"
    mock_settings.app_public_url = "https://test.example.com"
    mock_settings.internal_api_secret = "internal-secret"
    mock_settings.control_plane_jwt_secret = "fallback"

    with (
        patch.object(sso_keys_mod, "get_settings", return_value=mock_settings),
        patch.object(
            sso_keys_mod,
            "_fetch_keys_from_cp",
            return_value=(["key-a", "key-b"], 300.0),
        ) as mock_fetch,
    ):
        keys = sso_keys_mod.get_sso_keys()

    assert keys == ["key-a", "key-b"]
    mock_fetch.assert_called_once()


def test_cache_hit_avoids_refetch():
    """Second call within TTL uses cached keys."""
    mock_settings = MagicMock()
    mock_settings.control_plane_url = "https://control.example.com"
    mock_settings.app_public_url = "https://test.example.com"
    mock_settings.internal_api_secret = "internal-secret"
    mock_settings.control_plane_jwt_secret = "fallback"

    with (
        patch.object(sso_keys_mod, "get_settings", return_value=mock_settings),
        patch.object(
            sso_keys_mod,
            "_fetch_keys_from_cp",
            return_value=(["key-a"], 300.0),
        ) as mock_fetch,
    ):
        sso_keys_mod.get_sso_keys()
        sso_keys_mod.get_sso_keys()

    assert mock_fetch.call_count == 1


def test_ttl_expiry_triggers_refetch():
    """After TTL expires, next call refetches."""
    mock_settings = MagicMock()
    mock_settings.control_plane_url = "https://control.example.com"
    mock_settings.app_public_url = "https://test.example.com"
    mock_settings.internal_api_secret = "internal-secret"
    mock_settings.control_plane_jwt_secret = "fallback"

    with (
        patch.object(sso_keys_mod, "get_settings", return_value=mock_settings),
        patch.object(
            sso_keys_mod,
            "_fetch_keys_from_cp",
            return_value=(["key-a"], 1.0),  # 1 second TTL
        ) as mock_fetch,
    ):
        sso_keys_mod.get_sso_keys()
        assert mock_fetch.call_count == 1

        # Simulate TTL expiry
        sso_keys_mod._cached_at = time.monotonic() - 2.0
        sso_keys_mod.get_sso_keys()
        assert mock_fetch.call_count == 2


def test_stale_cache_on_fetch_failure():
    """When fetch fails, stale cache is served within grace period."""
    mock_settings = MagicMock()
    mock_settings.control_plane_url = "https://control.example.com"
    mock_settings.app_public_url = "https://test.example.com"
    mock_settings.internal_api_secret = "internal-secret"
    mock_settings.control_plane_jwt_secret = "fallback"

    call_count = 0

    def _mock_fetch():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return (["fresh-key"], 1.0)
        raise ConnectionError("CP unreachable")

    with (
        patch.object(sso_keys_mod, "get_settings", return_value=mock_settings),
        patch.object(sso_keys_mod, "_fetch_keys_from_cp", side_effect=_mock_fetch),
    ):
        # First call succeeds
        keys = sso_keys_mod.get_sso_keys()
        assert keys == ["fresh-key"]

        # Expire the TTL but stay within stale grace period
        sso_keys_mod._cached_at = time.monotonic() - 2.0

        # Second call fails fetch, returns stale
        keys = sso_keys_mod.get_sso_keys()
        assert keys == ["fresh-key"]


def test_env_var_fallback_when_cache_fully_expired():
    """When cache + grace period both expired and fetch fails, fall back to env var."""
    mock_settings = MagicMock()
    mock_settings.control_plane_url = "https://control.example.com"
    mock_settings.app_public_url = "https://test.example.com"
    mock_settings.internal_api_secret = "internal-secret"
    mock_settings.control_plane_jwt_secret = "env-fallback"

    call_count = 0

    def _mock_fetch():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return (["fresh-key"], 1.0)
        raise ConnectionError("CP unreachable")

    with (
        patch.object(sso_keys_mod, "get_settings", return_value=mock_settings),
        patch.object(sso_keys_mod, "_fetch_keys_from_cp", side_effect=_mock_fetch),
    ):
        sso_keys_mod.get_sso_keys()

        # Expire cache + grace period completely
        sso_keys_mod._cached_at = time.monotonic() - 500.0

        keys = sso_keys_mod.get_sso_keys()
        assert keys == ["env-fallback"]


# ---------------------------------------------------------------------------
# HTTP-level test: accept_token uses CP-fetched keys
# ---------------------------------------------------------------------------


def test_accept_token_validates_with_cp_keys(tmp_path):
    """accept_token should validate tokens signed with CP-fetched keys."""
    import hashlib
    import hmac
    import json
    import base64
    import os

    from fastapi.testclient import TestClient

    from zerg.database import Base, get_db, make_engine, make_sessionmaker
    from zerg.main import api_app
    from zerg.models.models import User

    # Set up in-memory DB
    db_path = tmp_path / "test_sso.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    SessionLocal = make_sessionmaker(engine)

    def _override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = _override_db

    # Seed a user
    with SessionLocal() as db:
        user = User(id=1, email="test@example.com", role="ADMIN")
        db.add(user)
        db.commit()

    # Create a JWT signed with a "CP secret" that the instance doesn't have as env var
    cp_secret = "cp-rotated-secret-xyz"

    def _b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload_dict = {
        "sub": "99",
        "email": "test@example.com",
        "exp": int(time.time()) + 300,
    }
    payload = _b64url(json.dumps(payload_dict, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode()
    sig = _b64url(hmac.new(cp_secret.encode(), signing_input, hashlib.sha256).digest())
    token = f"{header}.{payload}.{sig}"

    # Mock get_sso_keys to return the CP secret
    # The import happens inside the function, so patch at the source module
    with patch("zerg.services.sso_keys.get_sso_keys", return_value=[cp_secret]):
        client = TestClient(api_app)
        resp = client.post("/auth/accept-token", json={"token": token})

    assert resp.status_code == 200, resp.json()
    data = resp.json()
    assert "access_token" in data

    # Cleanup
    api_app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# Instance claim validation tests
# ---------------------------------------------------------------------------


def _make_jwt(payload_dict: dict, secret: str) -> str:
    """Create a minimal HS256 JWT for testing."""
    import hashlib
    import hmac
    import json
    import base64

    def _b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(json.dumps(payload_dict, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode()
    sig = _b64url(hmac.new(secret.encode(), signing_input, hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def _setup_test_db(tmp_path):
    """Set up a test DB with a user, returning (override_fn, cleanup_fn)."""
    from fastapi.testclient import TestClient
    from zerg.database import Base, get_db, make_engine, make_sessionmaker
    from zerg.main import api_app
    from zerg.models.models import User

    db_path = tmp_path / "test_instance_claim.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    SessionLocal = make_sessionmaker(engine)

    def _override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = _override_db

    with SessionLocal() as db:
        user = User(id=1, email="alice@example.com", role="ADMIN")
        db.add(user)
        db.commit()

    return api_app, lambda: api_app.dependency_overrides.pop(get_db, None)


def test_accept_token_rejects_wrong_instance_claim(tmp_path):
    """Token with instance=alice should be rejected when INSTANCE_ID=bob."""
    from fastapi.testclient import TestClient

    app, cleanup = _setup_test_db(tmp_path)
    secret = "test-secret"

    token = _make_jwt(
        {"sub": "99", "email": "alice@example.com", "instance": "alice", "exp": int(time.time()) + 300},
        secret,
    )

    with (
        patch("zerg.services.sso_keys.get_sso_keys", return_value=[secret]),
        patch.dict("os.environ", {"INSTANCE_ID": "bob"}),
    ):
        client = TestClient(app)
        resp = client.post("/auth/accept-token", json={"token": token})

    assert resp.status_code == 401
    assert "not intended for this instance" in resp.json()["detail"]
    cleanup()


def test_accept_token_accepts_correct_instance_claim(tmp_path):
    """Token with instance=alice should be accepted when INSTANCE_ID=alice."""
    from fastapi.testclient import TestClient

    app, cleanup = _setup_test_db(tmp_path)
    secret = "test-secret"

    token = _make_jwt(
        {"sub": "99", "email": "alice@example.com", "instance": "alice", "exp": int(time.time()) + 300},
        secret,
    )

    with (
        patch("zerg.services.sso_keys.get_sso_keys", return_value=[secret]),
        patch.dict("os.environ", {"INSTANCE_ID": "alice"}),
    ):
        client = TestClient(app)
        resp = client.post("/auth/accept-token", json={"token": token})

    assert resp.status_code == 200
    assert "access_token" in resp.json()
    cleanup()


def test_accept_token_accepts_no_instance_claim(tmp_path):
    """Token without instance claim should be accepted (backward compat)."""
    from fastapi.testclient import TestClient

    app, cleanup = _setup_test_db(tmp_path)
    secret = "test-secret"

    token = _make_jwt(
        {"sub": "99", "email": "alice@example.com", "exp": int(time.time()) + 300},
        secret,
    )

    with (
        patch("zerg.services.sso_keys.get_sso_keys", return_value=[secret]),
        patch.dict("os.environ", {"INSTANCE_ID": "alice"}),
    ):
        client = TestClient(app)
        resp = client.post("/auth/accept-token", json={"token": token})

    assert resp.status_code == 200
    assert "access_token" in resp.json()
    cleanup()


def test_accept_token_accepts_instance_claim_when_no_instance_id_env(tmp_path):
    """Token with instance claim should be accepted when INSTANCE_ID env is missing (OSS)."""
    from fastapi.testclient import TestClient

    app, cleanup = _setup_test_db(tmp_path)
    secret = "test-secret"

    token = _make_jwt(
        {"sub": "99", "email": "alice@example.com", "instance": "alice", "exp": int(time.time()) + 300},
        secret,
    )

    with (
        patch("zerg.services.sso_keys.get_sso_keys", return_value=[secret]),
        patch.dict("os.environ", {}, clear=False),
    ):
        # Ensure INSTANCE_ID is not set
        import os
        os.environ.pop("INSTANCE_ID", None)

        client = TestClient(app)
        resp = client.post("/auth/accept-token", json={"token": token})

    assert resp.status_code == 200
    assert "access_token" in resp.json()
    cleanup()


def test_auth_methods_returns_sso_when_cp_url_set():
    """/auth/methods should return sso: true when CONTROL_PLANE_URL is set."""
    from fastapi.testclient import TestClient
    from zerg.main import api_app

    mock_settings = MagicMock()
    mock_settings.google_client_id = None
    mock_settings.longhouse_password = None
    mock_settings.longhouse_password_hash = None
    mock_settings.control_plane_url = "https://control.longhouse.ai"

    with patch("zerg.routers.auth.get_settings", return_value=mock_settings):
        client = TestClient(api_app)
        resp = client.get("/auth/methods")

    assert resp.status_code == 200
    data = resp.json()
    assert data["sso"] is True
    assert data["sso_url"] == "https://control.longhouse.ai"
    assert data["google"] is False
    assert data["password"] is False
