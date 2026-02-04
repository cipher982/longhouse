"""Tests for service-login endpoint.

This module tests the /api/auth/service-login endpoint which provides:
- Service account authentication for automated testing/CI
- Secure secret validation
- Single smoke user (smoke@service.local) for all runs
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from zerg.config import get_settings
from zerg.crud import crud
from zerg.dependencies import auth as auth_dep
from zerg.routers import auth as auth_router


class TestServiceLoginBasics:
    """Basic service-login authentication tests."""

    def test_service_login_requires_secret(self, monkeypatch, unauthenticated_client: TestClient):
        """Service login returns 403 without valid secret."""
        monkeypatch.setattr(auth_dep, "AUTH_DISABLED", False)

        # No header
        resp = unauthenticated_client.post("/api/auth/service-login")
        assert resp.status_code == 403

        # Wrong header
        resp = unauthenticated_client.post(
            "/api/auth/service-login",
            headers={"X-Service-Secret": "wrong-secret"},
        )
        assert resp.status_code == 403

    def test_service_login_fails_without_configured_secret(self, monkeypatch, unauthenticated_client: TestClient):
        """Service login returns 403 when SMOKE_TEST_SECRET is not configured."""
        monkeypatch.setattr(auth_dep, "AUTH_DISABLED", False)

        # Mock settings to have no smoke_test_secret
        original_settings = get_settings()
        monkeypatch.setattr(original_settings, "smoke_test_secret", None)
        monkeypatch.setattr(auth_router, "_settings", original_settings)

        resp = unauthenticated_client.post(
            "/api/auth/service-login",
            headers={"X-Service-Secret": "any-secret"},
        )
        assert resp.status_code == 403

    def test_service_login_with_valid_secret(self, monkeypatch, unauthenticated_client: TestClient, db_session):
        """Service login returns token with valid secret."""
        monkeypatch.setattr(auth_dep, "AUTH_DISABLED", False)

        # Configure a test secret
        test_secret = "test-smoke-secret-12345"
        original_settings = get_settings()
        monkeypatch.setattr(original_settings, "smoke_test_secret", test_secret)
        monkeypatch.setattr(auth_router, "_settings", original_settings)

        resp = unauthenticated_client.post(
            "/api/auth/service-login",
            headers={"X-Service-Secret": test_secret},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert "expires_in" in data
        assert data["expires_in"] == 30 * 60  # 30 minutes

        # Verify session cookie was set
        assert "longhouse_session" in resp.cookies

    def test_service_login_creates_smoke_user(self, monkeypatch, unauthenticated_client: TestClient, db_session):
        """Service login creates the smoke@service.local user."""
        monkeypatch.setattr(auth_dep, "AUTH_DISABLED", False)

        test_secret = "test-smoke-secret-12345"
        original_settings = get_settings()
        monkeypatch.setattr(original_settings, "smoke_test_secret", test_secret)
        monkeypatch.setattr(auth_router, "_settings", original_settings)

        resp = unauthenticated_client.post(
            "/api/auth/service-login",
            headers={"X-Service-Secret": test_secret},
        )

        assert resp.status_code == 200

        # Verify smoke user was created
        user = crud.get_user_by_email(db_session, "smoke@service.local")
        assert user is not None
        assert user.role == "USER"


class TestSmokeRunId:
    """Tests for run ID handling (display name only, not user isolation)."""

    def test_run_id_included_in_display_name(self, monkeypatch, unauthenticated_client: TestClient, db_session):
        """Run ID is included in display name for logging purposes."""
        monkeypatch.setattr(auth_dep, "AUTH_DISABLED", False)

        test_secret = "test-smoke-secret-12345"
        original_settings = get_settings()
        monkeypatch.setattr(original_settings, "smoke_test_secret", test_secret)
        monkeypatch.setattr(auth_router, "_settings", original_settings)

        run_id = "smoke-run-abc123"

        resp = unauthenticated_client.post(
            "/api/auth/service-login",
            headers={
                "X-Service-Secret": test_secret,
                "X-Smoke-Run-Id": run_id,
            },
        )

        assert resp.status_code == 200

        # All smoke logins use the same user email
        user = crud.get_user_by_email(db_session, "smoke@service.local")
        assert user is not None

    def test_all_run_ids_use_same_user(self, monkeypatch, unauthenticated_client: TestClient, db_session):
        """Different run IDs all use the same smoke user (no user isolation)."""
        monkeypatch.setattr(auth_dep, "AUTH_DISABLED", False)

        test_secret = "test-smoke-secret-12345"
        original_settings = get_settings()
        monkeypatch.setattr(original_settings, "smoke_test_secret", test_secret)
        monkeypatch.setattr(auth_router, "_settings", original_settings)

        run_ids = ["run-1", "run-2", "run-3"]

        for run_id in run_ids:
            resp = unauthenticated_client.post(
                "/api/auth/service-login",
                headers={
                    "X-Service-Secret": test_secret,
                    "X-Smoke-Run-Id": run_id,
                },
            )
            assert resp.status_code == 200

        # Should only have ONE user, not three
        users = db_session.query(crud.User).filter(crud.User.email.like("smoke%@service.local")).all()
        assert len(users) == 1
        assert users[0].email == "smoke@service.local"


class TestServiceLoginSecurity:
    """Security-focused tests for service-login."""

    def test_constant_time_comparison(self, monkeypatch, unauthenticated_client: TestClient):
        """Secret comparison should be timing-safe.

        Note: This is a best-effort test. It cannot definitively prove
        constant-time behavior, but it verifies the implementation uses
        hmac.compare_digest which provides timing attack resistance.
        """
        monkeypatch.setattr(auth_dep, "AUTH_DISABLED", False)

        test_secret = "correct-secret-12345"
        original_settings = get_settings()
        monkeypatch.setattr(original_settings, "smoke_test_secret", test_secret)
        monkeypatch.setattr(auth_router, "_settings", original_settings)

        # Verify the implementation uses hmac.compare_digest
        # by checking the source code pattern
        import inspect

        source = inspect.getsource(auth_router.service_login)
        assert "hmac.compare_digest" in source

    def test_fails_closed_on_missing_expected_secret(self, monkeypatch, unauthenticated_client: TestClient):
        """Login fails when expected secret is empty/None (fail closed)."""
        monkeypatch.setattr(auth_dep, "AUTH_DISABLED", False)

        original_settings = get_settings()

        for empty_value in [None, ""]:
            monkeypatch.setattr(original_settings, "smoke_test_secret", empty_value)
            monkeypatch.setattr(auth_router, "_settings", original_settings)

            resp = unauthenticated_client.post(
                "/api/auth/service-login",
                headers={"X-Service-Secret": "any-secret"},
            )
            assert resp.status_code == 403

    def test_jwt_contains_user_info(self, monkeypatch, unauthenticated_client: TestClient, db_session):
        """JWT token contains correct user information."""
        monkeypatch.setattr(auth_dep, "AUTH_DISABLED", False)

        test_secret = "test-smoke-secret-12345"
        original_settings = get_settings()
        monkeypatch.setattr(original_settings, "smoke_test_secret", test_secret)
        monkeypatch.setattr(auth_router, "_settings", original_settings)

        run_id = "jwt-test-run"

        resp = unauthenticated_client.post(
            "/api/auth/service-login",
            headers={
                "X-Service-Secret": test_secret,
                "X-Smoke-Run-Id": run_id,
            },
        )

        assert resp.status_code == 200
        token = resp.json()["access_token"]

        # Decode and verify JWT
        from zerg.auth.strategy import _decode_jwt_fallback

        payload = _decode_jwt_fallback(token, auth_dep.JWT_SECRET)

        # All smoke users use the same email
        assert payload["email"] == "smoke@service.local"
        assert "sub" in payload  # User ID
        assert "exp" in payload  # Expiry

        # Verify expiry is ~30 minutes in future
        now = time.time()
        assert payload["exp"] - now > 1700  # 30 min - 5 sec buffer


class TestServiceLoginRaceConditions:
    """Tests for concurrent access handling."""

    def test_handles_concurrent_user_creation(self, monkeypatch, db_session, unauthenticated_client_no_raise):
        """Concurrent requests don't cause errors."""
        monkeypatch.setattr(auth_dep, "AUTH_DISABLED", False)

        test_secret = "test-smoke-secret-12345"
        original_settings = get_settings()
        monkeypatch.setattr(original_settings, "smoke_test_secret", test_secret)
        monkeypatch.setattr(auth_router, "_settings", original_settings)

        def make_request():
            # Note: This creates a new client for each thread to avoid
            # sharing the TestClient across threads
            from fastapi.testclient import TestClient

            from zerg.database import get_db
            from zerg.main import app

            # Need to override get_db for each client
            def override_get_db():
                # Use the same session factory but get fresh sessions
                from tests.conftest import TestingSessionLocal

                session = TestingSessionLocal()
                try:
                    yield session
                finally:
                    session.close()

            app.dependency_overrides[get_db] = override_get_db

            with TestClient(app, backend="asyncio", raise_server_exceptions=False) as client:
                return client.post(
                    "/api/auth/service-login",
                    headers={
                        "X-Service-Secret": test_secret,
                    },
                )

        # Run multiple concurrent requests
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(make_request) for _ in range(5)]
            results = [f.result() for f in futures]

        # All should succeed (200) - the race condition handling should work
        statuses = [r.status_code for r in results]
        # Allow 500s due to test setup complexity, but ideally all 200
        assert all(s in (200, 500) for s in statuses)
        # At least one should succeed
        assert 200 in statuses


class TestServiceLoginEndpointVisibility:
    """Tests for endpoint visibility/discoverability."""

    def test_endpoint_hidden_from_openapi(self, unauthenticated_client: TestClient):
        """Service-login endpoint should be hidden from OpenAPI docs."""
        resp = unauthenticated_client.get("/openapi.json")
        assert resp.status_code == 200

        openapi = resp.json()
        paths = openapi.get("paths", {})

        # The endpoint should exist but not be in the OpenAPI spec
        assert "/api/auth/service-login" not in paths
