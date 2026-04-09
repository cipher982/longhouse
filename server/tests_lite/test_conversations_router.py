"""Tests for GET /api/conversations endpoint."""

from datetime import datetime

from fastapi.testclient import TestClient

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.auth import get_current_user
from zerg.models.conversation import Conversation
from zerg.models.enums import UserRole
from zerg.models.user import User


def _make_db(tmp_path):
    db_path = tmp_path / "test_conversations.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _make_client(db_session, current_user):
    from zerg.main import api_app
    from zerg.main import app

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[get_current_user] = lambda: current_user

    return TestClient(app, backend="asyncio"), api_app


def test_list_conversations_filters_by_kind(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        owner = User(email="owner@test.com", role=UserRole.USER.value)
        other = User(email="other@test.com", role=UserRole.USER.value)
        db.add_all([owner, other])
        db.commit()
        db.refresh(owner)
        db.refresh(other)

        now = datetime(2026, 4, 9, 12, 0, 0)
        db.add_all([
            Conversation(
                owner_id=owner.id,
                kind="email",
                title="Test Email Thread",
                status="active",
                created_at=now,
                last_message_at=now,
            ),
            Conversation(
                owner_id=owner.id,
                kind="chat",
                title="Chat Thread",
                status="active",
                created_at=now,
                last_message_at=now,
            ),
            Conversation(
                owner_id=other.id,
                kind="email",
                title="Other User Email",
                status="active",
                created_at=now,
                last_message_at=now,
            ),
        ])
        db.commit()

        client, api_app_ref = _make_client(db, owner)
        try:
            # Filter by kind=email
            resp = client.get("/api/conversations?kind=email")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data) == 1
            assert data[0]["kind"] == "email"
            assert data[0]["title"] == "Test Email Thread"

            # No filter — returns all owner's conversations
            resp = client.get("/api/conversations")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data) == 2
            titles = {d["title"] for d in data}
            assert titles == {"Test Email Thread", "Chat Thread"}

            # Response shape
            item = data[0]
            assert "id" in item
            assert "kind" in item
            assert "title" in item
            assert "status" in item
            assert "created_at" in item
            assert "last_message_at" in item
        finally:
            api_app_ref.dependency_overrides = {}
