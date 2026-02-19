"""E2E tests for datetime serialization.

Verifies that actual API endpoints return datetime fields with "Z" suffix
so that JavaScript clients parse them correctly as UTC.
"""

from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zerg.database import Base, get_db, make_engine, make_sessionmaker
from zerg.models.agents import AgentsBase
from zerg.models.models import User
from zerg.models.agents import AgentSession, AgentEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path):
    """Create an in-memory SQLite DB with agents and user tables."""
    db_path = tmp_path / "test_datetime_e2e.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)  # User table
    AgentsBase.metadata.create_all(bind=engine)  # Agent tables
    SessionLocal = make_sessionmaker(engine)
    return SessionLocal


def _seed_session(db):
    """Seed a test session with naive UTC datetimes (simulating SQLite storage)."""
    import uuid
    from datetime import timedelta
    session_id = str(uuid.uuid4())

    # Use recent timestamps so days_back filter doesn't exclude them
    now = datetime.utcnow()
    started_at = now - timedelta(hours=1)
    ended_at = now - timedelta(minutes=30)

    session = AgentSession(
        id=session_id,
        provider="claude",
        environment="test",
        project="test-project",
        started_at=started_at,  # Naive datetime (recent)
        ended_at=ended_at,  # Naive datetime
        user_messages=2,
        assistant_messages=3,
        tool_calls=1,
    )
    db.add(session)

    # Add an event with naive datetime
    event = AgentEvent(
        session_id=session_id,
        timestamp=started_at + timedelta(minutes=5),  # Naive datetime
        role="user",
        content_text="Test message",
    )
    db.add(event)
    db.commit()
    return session


def _seed_user(db):
    """Seed an admin user for auth bypass."""
    user = User(id=1, email="test@local", role="ADMIN")
    db.add(user)
    db.commit()
    return user


def _make_client(db_session):
    """Create a TestClient with DB override."""
    from zerg.dependencies.auth import require_admin
    from zerg.main import api_app, app

    _seed_user(db_session)

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    def override_require_admin():
        return User(id=1, email="test@local", role="ADMIN")

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[require_admin] = override_require_admin

    client = TestClient(app, backend="asyncio")
    return client, api_app


def _find_datetime_strings(obj, path=""):
    """Recursively find all string values that look like ISO datetimes."""
    datetime_fields = []

    if isinstance(obj, dict):
        for key, value in obj.items():
            current_path = f"{path}.{key}" if path else key
            datetime_fields.extend(_find_datetime_strings(value, current_path))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            current_path = f"{path}[{i}]"
            datetime_fields.extend(_find_datetime_strings(item, current_path))
    elif isinstance(obj, str):
        # Check if it looks like an ISO datetime
        if "T" in obj and (":" in obj or "-" in obj):
            # Heuristic: has T separator and time/date components
            datetime_fields.append((path, obj))

    return datetime_fields


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_sessions_endpoint_datetime_has_z_suffix(tmp_path):
    """GET /api/agents/sessions should return all datetimes with Z suffix."""
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        session = _seed_session(db)

        # Verify session exists in DB
        db_session = db.query(AgentSession).filter(AgentSession.id == session.id).first()
        assert db_session is not None, "Session not found in DB after seeding"

        client, api_app_ref = _make_client(db)

        try:
            response = client.get("/api/agents/sessions?include_test=true")
            assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"

            data = response.json()

            # Find all datetime-like string fields in the response
            datetime_fields = _find_datetime_strings(data)

            # Assert we found some datetimes (sanity check)
            assert len(datetime_fields) > 0, f"Expected to find datetime fields in response. Got: {data}"

            # Assert all datetime strings end with "Z"
            failures = []
            for path, value in datetime_fields:
                if not value.endswith("Z"):
                    failures.append(f"{path} = {value} (missing Z suffix)")

            assert not failures, f"Found datetime fields without Z suffix:\n" + "\n".join(failures)

        finally:
            api_app_ref.dependency_overrides = {}


def test_session_detail_endpoint_datetime_has_z_suffix(tmp_path):
    """GET /api/agents/sessions/:id should return all datetimes with Z suffix."""
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        session = _seed_session(db)
        client, api_app_ref = _make_client(db)

        try:
            response = client.get(f"/api/agents/sessions/{session.id}")
            assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"

            data = response.json()

            # Find all datetime-like string fields
            datetime_fields = _find_datetime_strings(data)

            # Should find datetimes in session metadata and events
            assert len(datetime_fields) > 0, "Expected to find datetime fields in response"

            # Assert all end with Z
            failures = []
            for path, value in datetime_fields:
                if not value.endswith("Z"):
                    failures.append(f"{path} = {value} (missing Z suffix)")

            assert not failures, f"Found datetime fields without Z suffix:\n" + "\n".join(failures)

        finally:
            api_app_ref.dependency_overrides = {}
