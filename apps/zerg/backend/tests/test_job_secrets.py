"""Tests for job secrets store, repo config API, and context injection.

Covers:
- JobContext.require_secret() / get_secret() behaviour
- resolve_secrets() DB-first, env-var fallback
- Secrets CRUD via API (PUT/GET/DELETE)
- Repo config CRUD via API
- Signature detection: zero-arg vs ctx-arg dispatch
"""

from __future__ import annotations

import asyncio
import os
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest

from zerg.jobs.context import JobContext
from zerg.models.models import JobRepoConfig, JobSecret
from zerg.utils.crypto import encrypt


# ---------------------------------------------------------------------------
# Unit: JobContext
# ---------------------------------------------------------------------------


class TestJobContext:
    def test_require_secret_present(self):
        ctx = JobContext(job_id="test-job", secrets={"MY_KEY": "my-value"})
        assert ctx.require_secret("MY_KEY") == "my-value"

    def test_require_secret_missing_raises(self):
        ctx = JobContext(job_id="test-job", secrets={"OTHER": "val"})
        with pytest.raises(RuntimeError, match="Secret 'MISSING' not available"):
            ctx.require_secret("MISSING")

    def test_get_secret_present(self):
        ctx = JobContext(job_id="test-job", secrets={"K": "V"})
        assert ctx.get_secret("K") == "V"

    def test_get_secret_missing_returns_default(self):
        ctx = JobContext(job_id="test-job", secrets={})
        assert ctx.get_secret("MISSING") is None
        assert ctx.get_secret("MISSING", "fallback") == "fallback"

    def test_secrets_property_returns_copy(self):
        original = {"A": "1", "B": "2"}
        ctx = JobContext(job_id="test-job", secrets=original)
        copy = ctx.secrets
        copy["C"] = "3"
        # Original should be unchanged
        assert "C" not in ctx._secrets

    def test_job_id_property(self):
        ctx = JobContext(job_id="my-job", secrets={})
        assert ctx.job_id == "my-job"


# ---------------------------------------------------------------------------
# Unit: resolve_secrets
# ---------------------------------------------------------------------------


class TestResolveSecrets:
    def test_empty_declared_keys(self, db_session):
        from zerg.jobs.secret_resolver import resolve_secrets

        result = resolve_secrets(owner_id=1, declared_keys=[], db=db_session)
        assert result == {}

    def test_db_value_returned(self, db_session, test_user):
        from zerg.jobs.secret_resolver import resolve_secrets

        # Store a secret in DB
        secret = JobSecret(
            owner_id=test_user.id,
            key="DB_SECRET",
            encrypted_value=encrypt("db-value"),
        )
        db_session.add(secret)
        db_session.commit()

        result = resolve_secrets(
            owner_id=test_user.id,
            declared_keys=["DB_SECRET"],
            db=db_session,
        )
        assert result == {"DB_SECRET": "db-value"}

    def test_env_fallback(self, db_session, test_user):
        from zerg.jobs.secret_resolver import resolve_secrets

        # No DB entry, but env var exists
        with patch.dict(os.environ, {"ENV_ONLY_SECRET": "env-value"}):
            result = resolve_secrets(
                owner_id=test_user.id,
                declared_keys=["ENV_ONLY_SECRET"],
                db=db_session,
            )
        assert result == {"ENV_ONLY_SECRET": "env-value"}

    def test_db_takes_precedence_over_env(self, db_session, test_user):
        from zerg.jobs.secret_resolver import resolve_secrets

        # DB entry exists AND env var exists — DB should win
        secret = JobSecret(
            owner_id=test_user.id,
            key="DUAL_SECRET",
            encrypted_value=encrypt("from-db"),
        )
        db_session.add(secret)
        db_session.commit()

        with patch.dict(os.environ, {"DUAL_SECRET": "from-env"}):
            result = resolve_secrets(
                owner_id=test_user.id,
                declared_keys=["DUAL_SECRET"],
                db=db_session,
            )
        assert result == {"DUAL_SECRET": "from-db"}

    def test_missing_key_omitted(self, db_session, test_user):
        from zerg.jobs.secret_resolver import resolve_secrets

        # Key neither in DB nor env → omitted from result
        result = resolve_secrets(
            owner_id=test_user.id,
            declared_keys=["NONEXISTENT_KEY_XYZ"],
            db=db_session,
        )
        assert "NONEXISTENT_KEY_XYZ" not in result


# ---------------------------------------------------------------------------
# Integration: Secrets API
# ---------------------------------------------------------------------------


class TestSecretsAPI:
    def test_put_and_list(self, client):
        # Create a secret
        resp = client.put(
            "/api/jobs/secrets/MY_KEY",
            json={"value": "test-value-123", "description": "A test secret"},
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        # List should show the key (but not the value)
        resp = client.get("/api/jobs/secrets")
        assert resp.status_code == 200
        secrets = resp.json()
        assert len(secrets) == 1
        assert secrets[0]["key"] == "MY_KEY"
        assert secrets[0]["description"] == "A test secret"
        assert "value" not in secrets[0]

    def test_upsert_updates_existing(self, client):
        # Create
        client.put("/api/jobs/secrets/UPD_KEY", json={"value": "v1"})
        # Update
        resp = client.put(
            "/api/jobs/secrets/UPD_KEY",
            json={"value": "v2", "description": "updated"},
        )
        assert resp.status_code == 200

        # List should show one entry
        secrets = client.get("/api/jobs/secrets").json()
        assert len(secrets) == 1
        assert secrets[0]["description"] == "updated"

    def test_delete(self, client):
        client.put("/api/jobs/secrets/DEL_KEY", json={"value": "to-delete"})

        resp = client.delete("/api/jobs/secrets/DEL_KEY")
        assert resp.status_code == 204

        # Should be gone
        secrets = client.get("/api/jobs/secrets").json()
        assert len(secrets) == 0

    def test_delete_nonexistent_returns_404(self, client):
        resp = client.delete("/api/jobs/secrets/NO_SUCH_KEY")
        assert resp.status_code == 404

    def test_key_validation(self, client):
        # Empty key path should result in 404 (FastAPI path param)
        resp = client.put(
            "/api/jobs/secrets/" + "x" * 300,
            json={"value": "too-long-key"},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Integration: Repo Config API
# ---------------------------------------------------------------------------


class TestRepoConfigAPI:
    def test_no_config_returns_404(self, client):
        resp = client.get("/api/jobs/repo/config")
        # With no DB config and no env var, should 404
        assert resp.status_code == 404

    def test_set_and_get_config(self, client):
        # Set config
        resp = client.post(
            "/api/jobs/repo/config",
            json={"repo_url": "https://github.com/user/jobs.git", "branch": "main", "token": "ghp_test"},
        )
        assert resp.status_code == 200

        # Get config
        resp = client.get("/api/jobs/repo/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["repo_url"] == "https://github.com/user/jobs.git"
        assert data["branch"] == "main"
        assert data["has_token"] is True
        assert data["source"] == "database"
        # Token should never be exposed
        assert "token" not in data

    def test_update_config_clears_sync_state(self, client, db_session, test_user):
        # Create config with fake sync state
        config = JobRepoConfig(
            owner_id=test_user.id,
            repo_url="https://example.com/old.git",
            branch="main",
            last_sync_sha="abc123",
        )
        db_session.add(config)
        db_session.commit()

        # Update via API
        resp = client.post(
            "/api/jobs/repo/config",
            json={"repo_url": "https://example.com/new.git"},
        )
        assert resp.status_code == 200

        # Sync state should be cleared
        resp = client.get("/api/jobs/repo/config")
        data = resp.json()
        assert data["repo_url"] == "https://example.com/new.git"
        assert data["last_sync_sha"] is None

    def test_delete_config(self, client):
        # Create
        client.post(
            "/api/jobs/repo/config",
            json={"repo_url": "https://example.com/repo.git"},
        )
        # Delete
        resp = client.delete("/api/jobs/repo/config")
        assert resp.status_code == 204

        # Should be gone
        resp = client.get("/api/jobs/repo/config")
        assert resp.status_code == 404

    def test_delete_nonexistent_returns_404(self, client):
        resp = client.delete("/api/jobs/repo/config")
        assert resp.status_code == 404

    def test_public_repo_no_token(self, client):
        resp = client.post(
            "/api/jobs/repo/config",
            json={"repo_url": "https://github.com/public/repo.git"},
        )
        assert resp.status_code == 200

        data = client.get("/api/jobs/repo/config").json()
        assert data["has_token"] is False


# ---------------------------------------------------------------------------
# Unit: Signature detection dispatch
# ---------------------------------------------------------------------------


class TestSignatureDetection:
    def test_zero_arg_dispatch(self):
        """Jobs with run() (no params) should be called with no args."""
        from zerg.jobs.registry import JobConfig, _invoke_job_func

        call_log = []

        async def legacy_run():
            call_log.append("legacy")
            return {"ok": True}

        config = JobConfig(id="legacy-job", cron="* * * * *", func=legacy_run)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_invoke_job_func(config))
        finally:
            loop.close()
        assert call_log == ["legacy"]

    def test_ctx_arg_dispatch(self, db_session):
        """Jobs with run(ctx) should receive a JobContext."""
        from zerg.jobs.registry import JobConfig, _invoke_job_func

        received_ctx = []

        async def new_style_run(ctx: JobContext):
            received_ctx.append(ctx)
            return {"ok": True}

        config = JobConfig(
            id="new-job",
            cron="* * * * *",
            func=new_style_run,
            secrets=["TEST_SECRET"],
        )

        # Build a contextmanager that yields the test db_session
        @contextmanager
        def fake_db_session():
            yield db_session

        with patch("zerg.database.db_session", fake_db_session):
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(_invoke_job_func(config))
            finally:
                loop.close()

        assert len(received_ctx) == 1
        assert received_ctx[0].job_id == "new-job"
        assert isinstance(received_ctx[0], JobContext)
