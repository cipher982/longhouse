"""Tests for email config: resolve_email_config() and email config API endpoints.

Uses in-memory SQLite with inline setup (no shared conftest).
"""

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from zerg.database import Base, get_db, make_engine, make_sessionmaker
from zerg.models.models import JobSecret, User
from zerg.shared.email import _EMAIL_SECRET_KEYS
from zerg.utils.crypto import encrypt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path):
    """Create an in-memory SQLite DB with all tables, return session factory."""
    db_path = tmp_path / "test_email.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    SessionLocal = make_sessionmaker(engine)
    return SessionLocal


def _seed_user(db, user_id=1, email="test@local"):
    user = User(id=user_id, email=email, role="ADMIN")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_email_secret(db, key, value, owner_id=1):
    db.add(
        JobSecret(
            owner_id=owner_id,
            key=key,
            encrypted_value=encrypt(value),
            description="test",
        )
    )
    db.commit()


# ---------------------------------------------------------------------------
# Unit tests: resolve_email_config()
# ---------------------------------------------------------------------------


class TestResolveEmailConfig:
    """Test resolve_email_config() DB-first, env-fallback logic."""

    def test_env_fallback_when_no_db(self):
        """When DB is unavailable, falls back to env vars."""
        env = {
            "AWS_SES_ACCESS_KEY_ID": "AKIA_ENV",
            "AWS_SES_SECRET_ACCESS_KEY": "secret_env",
            "FROM_EMAIL": "from@env.com",
        }
        with patch.dict(os.environ, env, clear=False):
            # Patch at the source so the lazy import inside resolve_email_config picks it up
            with patch(
                "zerg.database.get_session_factory",
                side_effect=Exception("no db"),
            ):
                from zerg.shared.email import resolve_email_config

                result = resolve_email_config()

        assert result["AWS_SES_ACCESS_KEY_ID"] == "AKIA_ENV"
        assert result["AWS_SES_SECRET_ACCESS_KEY"] == "secret_env"
        assert result["FROM_EMAIL"] == "from@env.com"

    def test_db_takes_precedence(self, tmp_path):
        """DB secrets override env vars."""
        SessionLocal = _make_db(tmp_path)

        with SessionLocal() as db:
            _seed_user(db)
            _seed_email_secret(db, "AWS_SES_ACCESS_KEY_ID", "AKIA_DB")
            _seed_email_secret(db, "FROM_EMAIL", "from@db.com")

        env = {
            "AWS_SES_ACCESS_KEY_ID": "AKIA_ENV",
            "AWS_SES_SECRET_ACCESS_KEY": "secret_env",
            "FROM_EMAIL": "from@env.com",
        }

        with patch.dict(os.environ, env, clear=False):
            with patch(
                "zerg.database.get_session_factory",
                return_value=SessionLocal,
            ):
                from zerg.shared.email import resolve_email_config

                result = resolve_email_config()

        # DB wins for keys present in DB
        assert result["AWS_SES_ACCESS_KEY_ID"] == "AKIA_DB"
        assert result["FROM_EMAIL"] == "from@db.com"
        # Env fallback for keys NOT in DB
        assert result["AWS_SES_SECRET_ACCESS_KEY"] == "secret_env"

    def test_empty_result_when_nothing_configured(self):
        """Returns empty dict when no DB and no env vars."""
        # Clear all email-related env vars
        clean_env = {k: "" for k in [
            "AWS_SES_ACCESS_KEY_ID", "AWS_SES_SECRET_ACCESS_KEY",
            "AWS_SES_REGION", "FROM_EMAIL", "NOTIFY_EMAIL",
            "DIGEST_EMAIL", "ALERT_EMAIL",
        ]}
        with patch.dict(os.environ, clean_env, clear=False):
            with patch(
                "zerg.database.get_session_factory",
                side_effect=Exception("no db"),
            ):
                from zerg.shared.email import resolve_email_config

                result = resolve_email_config()

        # Empty strings from env don't count (os.environ.get returns "" which is falsy)
        assert "AWS_SES_ACCESS_KEY_ID" not in result


# ---------------------------------------------------------------------------
# HTTP-level tests: email config endpoints
# ---------------------------------------------------------------------------


class TestEmailConfigAPI:
    """Test email config CRUD endpoints."""

    @pytest.fixture()
    def client(self, tmp_path):
        """Build a TestClient with a fresh DB."""
        SessionLocal = _make_db(tmp_path)

        with SessionLocal() as db:
            _seed_user(db, user_id=1, email="admin@test.com")

        from zerg.main import api_app

        def override_db():
            with SessionLocal() as db:
                yield db

        def override_user():
            return User(id=1, email="admin@test.com", role="ADMIN")

        from zerg.dependencies.auth import get_current_user

        api_app.dependency_overrides[get_db] = override_db
        api_app.dependency_overrides[get_current_user] = override_user

        yield TestClient(api_app)

        api_app.dependency_overrides.clear()

    def test_status_empty(self, client):
        """Status shows not configured when nothing is set."""
        clean_env = {k: "" for k in [
            "AWS_SES_ACCESS_KEY_ID", "AWS_SES_SECRET_ACCESS_KEY",
            "AWS_SES_REGION", "FROM_EMAIL", "NOTIFY_EMAIL",
            "DIGEST_EMAIL", "ALERT_EMAIL",
        ]}
        with patch.dict(os.environ, clean_env, clear=False):
            resp = client.get("/system/email/status")
            assert resp.status_code == 200
            data = resp.json()
            assert data["configured"] is False

    def test_save_and_status(self, client):
        """Save config then check status shows configured from DB."""
        clean_env = {k: "" for k in [
            "AWS_SES_ACCESS_KEY_ID", "AWS_SES_SECRET_ACCESS_KEY",
            "AWS_SES_REGION", "FROM_EMAIL", "NOTIFY_EMAIL",
        ]}
        with patch.dict(os.environ, clean_env, clear=False):
            # Save
            resp = client.put(
                "/system/email/config",
                json={
                    "aws_ses_access_key_id": "AKIA_TEST",
                    "aws_ses_secret_access_key": "secret_test",
                    "from_email": "test@example.com",
                },
            )
            assert resp.status_code == 200
            assert resp.json()["keys_saved"] == 3

            # Status should now show configured
            resp = client.get("/system/email/status")
            assert resp.status_code == 200
            data = resp.json()
            assert data["configured"] is True
            assert data["source"] == "db"

    def test_delete_config(self, client):
        """Delete removes DB overrides."""
        clean_env = {k: "" for k in [
            "AWS_SES_ACCESS_KEY_ID", "AWS_SES_SECRET_ACCESS_KEY",
            "FROM_EMAIL",
        ]}
        with patch.dict(os.environ, clean_env, clear=False):
            # Save first
            client.put(
                "/system/email/config",
                json={
                    "aws_ses_access_key_id": "AKIA_TEST",
                    "aws_ses_secret_access_key": "secret_test",
                    "from_email": "test@example.com",
                },
            )

            # Delete
            resp = client.delete("/system/email/config")
            assert resp.status_code == 200
            assert resp.json()["keys_deleted"] == 3

            # Status should now show not configured
            resp = client.get("/system/email/status")
            data = resp.json()
            assert data["configured"] is False

    def test_empty_strings_not_saved(self, client):
        """Empty/whitespace-only values are not persisted as configured."""
        clean_env = {k: "" for k in [
            "AWS_SES_ACCESS_KEY_ID", "AWS_SES_SECRET_ACCESS_KEY",
            "FROM_EMAIL", "AWS_SES_REGION", "NOTIFY_EMAIL",
            "DIGEST_EMAIL", "ALERT_EMAIL",
        ]}
        with patch.dict(os.environ, clean_env, clear=False):
            resp = client.put(
                "/system/email/config",
                json={
                    "aws_ses_access_key_id": "  ",
                    "aws_ses_secret_access_key": "",
                    "from_email": "  \t  ",
                },
            )
            assert resp.status_code == 200
            assert resp.json()["keys_saved"] == 0

            # Status should still be unconfigured
            resp = client.get("/system/email/status")
            assert resp.json()["configured"] is False


# ---------------------------------------------------------------------------
# Tests: non-default user ID (owner_id != 1)
# ---------------------------------------------------------------------------


class TestNonDefaultOwnerID:
    """Verify resolve_email_config finds secrets saved under a non-1 user ID."""

    def test_multi_user_finds_secret_owner(self, tmp_path):
        """With users 1 and 5, secrets under user 5 are found (not first-by-id)."""
        SessionLocal = _make_db(tmp_path)

        with SessionLocal() as db:
            _seed_user(db, user_id=1, email="first@test.com")
            _seed_user(db, user_id=5, email="user5@test.com")
            # Secrets belong to user 5, not user 1
            _seed_email_secret(db, "AWS_SES_ACCESS_KEY_ID", "AKIA_U5", owner_id=5)
            _seed_email_secret(db, "AWS_SES_SECRET_ACCESS_KEY", "secret_u5", owner_id=5)
            _seed_email_secret(db, "FROM_EMAIL", "from@u5.com", owner_id=5)

        clean_env = {k: "" for k in _EMAIL_SECRET_KEYS}
        with patch.dict(os.environ, clean_env, clear=False):
            with patch(
                "zerg.database.get_session_factory",
                return_value=SessionLocal,
            ):
                from zerg.shared.email import resolve_email_config

                result = resolve_email_config()

        assert result["AWS_SES_ACCESS_KEY_ID"] == "AKIA_U5"
        assert result["AWS_SES_SECRET_ACCESS_KEY"] == "secret_u5"
        assert result["FROM_EMAIL"] == "from@u5.com"

    def test_resolve_finds_non_1_owner(self, tmp_path):
        """Secrets saved as user 2 are resolved when user 2 is the only user."""
        SessionLocal = _make_db(tmp_path)

        with SessionLocal() as db:
            _seed_user(db, user_id=2, email="user2@test.com")
            _seed_email_secret(db, "AWS_SES_ACCESS_KEY_ID", "AKIA_USER2", owner_id=2)
            _seed_email_secret(db, "AWS_SES_SECRET_ACCESS_KEY", "secret_user2", owner_id=2)
            _seed_email_secret(db, "FROM_EMAIL", "from@user2.com", owner_id=2)

        clean_env = {k: "" for k in [
            "AWS_SES_ACCESS_KEY_ID", "AWS_SES_SECRET_ACCESS_KEY",
            "FROM_EMAIL", "AWS_SES_REGION", "NOTIFY_EMAIL",
            "DIGEST_EMAIL", "ALERT_EMAIL",
        ]}
        with patch.dict(os.environ, clean_env, clear=False):
            with patch(
                "zerg.database.get_session_factory",
                return_value=SessionLocal,
            ):
                from zerg.shared.email import resolve_email_config

                result = resolve_email_config()

        assert result["AWS_SES_ACCESS_KEY_ID"] == "AKIA_USER2"
        assert result["AWS_SES_SECRET_ACCESS_KEY"] == "secret_user2"
        assert result["FROM_EMAIL"] == "from@user2.com"

    def test_api_saves_under_current_user_id(self, tmp_path):
        """API saves secrets under user 5, status endpoint finds them."""
        SessionLocal = _make_db(tmp_path)

        with SessionLocal() as db:
            _seed_user(db, user_id=5, email="user5@test.com")

        from zerg.main import api_app

        def override_db():
            with SessionLocal() as db:
                yield db

        def override_user():
            return User(id=5, email="user5@test.com", role="ADMIN")

        from zerg.dependencies.auth import get_current_user

        api_app.dependency_overrides[get_db] = override_db
        api_app.dependency_overrides[get_current_user] = override_user

        client = TestClient(api_app)
        clean_env = {k: "" for k in [
            "AWS_SES_ACCESS_KEY_ID", "AWS_SES_SECRET_ACCESS_KEY",
            "FROM_EMAIL", "AWS_SES_REGION", "NOTIFY_EMAIL",
            "DIGEST_EMAIL", "ALERT_EMAIL",
        ]}
        try:
            with patch.dict(os.environ, clean_env, clear=False):
                # Save as user 5
                resp = client.put(
                    "/system/email/config",
                    json={
                        "aws_ses_access_key_id": "AKIA_U5",
                        "aws_ses_secret_access_key": "secret_u5",
                        "from_email": "from@u5.com",
                    },
                )
                assert resp.status_code == 200
                assert resp.json()["keys_saved"] == 3

                # Status should show configured (secrets are under user 5)
                resp = client.get("/system/email/status")
                data = resp.json()
                assert data["configured"] is True
                assert data["source"] == "db"
        finally:
            api_app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Tests: mixed env + DB source resolution
# ---------------------------------------------------------------------------


class TestMixedSourceResolution:
    """Verify resolve_email_config combines DB and env sources correctly."""

    def test_mixed_db_and_env(self, tmp_path):
        """Some keys from DB, others from env — both contribute."""
        SessionLocal = _make_db(tmp_path)

        with SessionLocal() as db:
            _seed_user(db, user_id=1, email="test@local")
            # Only access key in DB
            _seed_email_secret(db, "AWS_SES_ACCESS_KEY_ID", "AKIA_DB")

        env = {
            "AWS_SES_SECRET_ACCESS_KEY": "secret_env",
            "FROM_EMAIL": "from@env.com",
            "NOTIFY_EMAIL": "notify@env.com",
        }

        with patch.dict(os.environ, env, clear=False):
            with patch(
                "zerg.database.get_session_factory",
                return_value=SessionLocal,
            ):
                from zerg.shared.email import resolve_email_config

                result = resolve_email_config()

        # DB source
        assert result["AWS_SES_ACCESS_KEY_ID"] == "AKIA_DB"
        # Env sources
        assert result["AWS_SES_SECRET_ACCESS_KEY"] == "secret_env"
        assert result["FROM_EMAIL"] == "from@env.com"
        assert result["NOTIFY_EMAIL"] == "notify@env.com"


# ---------------------------------------------------------------------------
# Tests: status endpoint validates values, not just row presence
# ---------------------------------------------------------------------------


class TestStatusValidatesValues:
    """Verify status checks decrypted value, not just DB row existence."""

    @pytest.fixture()
    def client(self, tmp_path):
        SessionLocal = _make_db(tmp_path)

        with SessionLocal() as db:
            _seed_user(db, user_id=1, email="admin@test.com")
            # Seed an empty-string secret (simulating a corrupt/legacy row)
            db.add(
                JobSecret(
                    owner_id=1,
                    key="AWS_SES_ACCESS_KEY_ID",
                    encrypted_value=encrypt(""),
                    description="empty value",
                )
            )
            db.add(
                JobSecret(
                    owner_id=1,
                    key="AWS_SES_SECRET_ACCESS_KEY",
                    encrypted_value=encrypt("   "),
                    description="whitespace value",
                )
            )
            db.commit()

        from zerg.main import api_app

        def override_db():
            with SessionLocal() as db:
                yield db

        def override_user():
            return User(id=1, email="admin@test.com", role="ADMIN")

        from zerg.dependencies.auth import get_current_user

        api_app.dependency_overrides[get_db] = override_db
        api_app.dependency_overrides[get_current_user] = override_user

        yield TestClient(api_app)

        api_app.dependency_overrides.clear()

    def test_empty_db_rows_not_configured(self, client):
        """DB rows with empty/whitespace values show as not configured."""
        clean_env = {k: "" for k in _EMAIL_SECRET_KEYS}
        with patch.dict(os.environ, clean_env, clear=False):
            resp = client.get("/system/email/status")
            assert resp.status_code == 200
            data = resp.json()
            # Overall should NOT be configured (empty values)
            assert data["configured"] is False
            # Individual keys with empty values should show not configured
            key_map = {k["key"]: k for k in data["keys"]}
            assert key_map["AWS_SES_ACCESS_KEY_ID"]["configured"] is False
            assert key_map["AWS_SES_SECRET_ACCESS_KEY"]["configured"] is False
