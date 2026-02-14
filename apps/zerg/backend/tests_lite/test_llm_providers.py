"""Tests for LLM Provider Configuration endpoints.

Covers:
- GET /api/capabilities/llm — public capability status
- GET /api/llm/providers — list configured providers
- PUT /api/llm/providers/{capability} — upsert provider config
- DELETE /api/llm/providers/{capability} — remove provider config

Uses in-memory SQLite with inline setup (no shared conftest).
"""

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from zerg.database import Base, get_db, make_engine, make_sessionmaker
from zerg.models.models import LlmProviderConfig, User


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path):
    """Create an in-memory SQLite DB with all tables, return session factory."""
    db_path = tmp_path / "test_llm.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    SessionLocal = make_sessionmaker(engine)
    return SessionLocal


def _seed_user(db, user_id=1, email="test@local"):
    """Insert a test user and return it."""
    user = User(id=user_id, email=email, role="ADMIN")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _get_client(session_factory):
    """Create a TestClient with DB and auth overrides."""
    from zerg.dependencies.auth import get_current_user
    from zerg.main import api_app

    db = session_factory()
    user = _seed_user(db)
    db.close()

    def _override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    def _override_user():
        db = session_factory()
        try:
            return db.query(User).first()
        finally:
            db.close()

    api_app.dependency_overrides[get_db] = _override_db
    api_app.dependency_overrides[get_current_user] = _override_user

    client = TestClient(api_app)
    yield client

    api_app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Tests: GET /capabilities/llm
# ---------------------------------------------------------------------------


class TestLlmCapabilities:
    def test_returns_both_capabilities(self, tmp_path):
        """Capabilities endpoint returns text and embedding status."""
        sf = _make_db(tmp_path)
        for client in _get_client(sf):
            resp = client.get("/capabilities/llm")
            assert resp.status_code == 200
            data = resp.json()
            assert "text" in data
            assert "embedding" in data
            assert isinstance(data["text"]["features"], list)
            assert isinstance(data["embedding"]["features"], list)

    @patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-123"})
    def test_env_var_makes_available(self, tmp_path):
        """When OPENAI_API_KEY is set, both text and embedding show available."""
        sf = _make_db(tmp_path)
        for client in _get_client(sf):
            resp = client.get("/capabilities/llm")
            data = resp.json()
            assert data["text"]["available"] is True
            assert data["text"]["source"] == "environment"
            assert data["embedding"]["available"] is True

    def test_db_config_makes_available(self, tmp_path):
        """When DB has a provider config, capability shows as available."""
        sf = _make_db(tmp_path)

        for client in _get_client(sf):
            # Seed a provider config via the API (user already seeded by _get_client)
            client.put(
                "/llm/providers/text",
                json={
                    "provider_name": "groq",
                    "api_key": "gsk-test",
                    "base_url": "https://api.groq.com/openai/v1",
                },
            )

            # Clear any env keys that might interfere
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("OPENAI_API_KEY", None)
                os.environ.pop("GROQ_API_KEY", None)
                resp = client.get("/capabilities/llm")
                data = resp.json()
                assert data["text"]["available"] is True
                assert data["text"]["source"] == "database"
                assert data["text"]["provider_name"] == "groq"


# ---------------------------------------------------------------------------
# Tests: PUT /llm/providers/{capability}
# ---------------------------------------------------------------------------


class TestUpsertProvider:
    def test_create_provider(self, tmp_path):
        """PUT creates a new provider config."""
        sf = _make_db(tmp_path)
        for client in _get_client(sf):
            resp = client.put(
                "/llm/providers/text",
                json={
                    "provider_name": "openai",
                    "api_key": "sk-test-123",
                    "base_url": None,
                },
            )
            assert resp.status_code == 200
            assert resp.json()["success"] is True

            # Verify it's listed
            resp = client.get("/llm/providers")
            assert resp.status_code == 200
            providers = resp.json()
            assert len(providers) == 1
            assert providers[0]["capability"] == "text"
            assert providers[0]["provider_name"] == "openai"

    def test_update_provider(self, tmp_path):
        """PUT updates existing provider config."""
        sf = _make_db(tmp_path)
        for client in _get_client(sf):
            # Create
            client.put(
                "/llm/providers/text",
                json={"provider_name": "openai", "api_key": "sk-1"},
            )
            # Update
            resp = client.put(
                "/llm/providers/text",
                json={"provider_name": "groq", "api_key": "gsk-2", "base_url": "https://api.groq.com/openai/v1"},
            )
            assert resp.status_code == 200

            providers = client.get("/llm/providers").json()
            assert len(providers) == 1
            assert providers[0]["provider_name"] == "groq"

    def test_invalid_capability(self, tmp_path):
        """PUT rejects invalid capability names."""
        sf = _make_db(tmp_path)
        for client in _get_client(sf):
            resp = client.put(
                "/llm/providers/invalid",
                json={"provider_name": "openai", "api_key": "sk-1"},
            )
            assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Tests: DELETE /llm/providers/{capability}
# ---------------------------------------------------------------------------


class TestDeleteProvider:
    def test_delete_existing(self, tmp_path):
        """DELETE removes provider config."""
        sf = _make_db(tmp_path)
        for client in _get_client(sf):
            # Create first
            client.put(
                "/llm/providers/embedding",
                json={"provider_name": "openai", "api_key": "sk-1"},
            )
            # Delete
            resp = client.delete("/llm/providers/embedding")
            assert resp.status_code == 204

            # Verify it's gone
            providers = client.get("/llm/providers").json()
            assert len(providers) == 0

    def test_delete_nonexistent(self, tmp_path):
        """DELETE returns 404 for non-existent config."""
        sf = _make_db(tmp_path)
        for client in _get_client(sf):
            resp = client.delete("/llm/providers/text")
            assert resp.status_code == 404
