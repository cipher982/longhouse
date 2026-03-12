"""HTTP tests for canonical conversations APIs."""

import os

os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi.testclient import TestClient

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.user import User
from zerg.services.conversation_service import ConversationService


def _make_db(tmp_path):
    db_path = tmp_path / "test_conversations_router.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_user(db, user_id: int, email: str) -> User:
    user = User(id=user_id, email=email, role="USER")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_conversations_list_search_detail_and_messages(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        owner = _seed_user(db, 1, "owner@test.local")
        other = _seed_user(db, 2, "other@test.local")

        owner_conversation = ConversationService.get_or_create_by_binding(
            db,
            owner_id=owner.id,
            kind="email",
            surface_id="email",
            provider="gmail",
            binding_scope="owner@gmail.com",
            external_conversation_id="thread-owner",
            title="Rollout Thread",
        )
        other_conversation = ConversationService.get_or_create_by_binding(
            db,
            owner_id=other.id,
            kind="email",
            surface_id="email",
            provider="gmail",
            binding_scope="other@gmail.com",
            external_conversation_id="thread-other",
            title="Private Thread",
        )
        ConversationService.append_message(
            db,
            owner_id=owner.id,
            conversation_id=owner_conversation.id,
            role="external",
            direction="incoming",
            sender_kind="external_contact",
            content="Please review the rollout notes",
        )
        ConversationService.append_message(
            db,
            owner_id=other.id,
            conversation_id=other_conversation.id,
            role="external",
            direction="incoming",
            sender_kind="external_contact",
            content="This should not leak",
        )
        other_conversation_id = other_conversation.id

    from zerg.dependencies.auth import get_current_user
    from zerg.main import api_app

    def override_db():
        with SessionLocal() as db:
            yield db

    def override_user():
        return User(id=1, email="owner@test.local", role="USER")

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[get_current_user] = override_user

    client = TestClient(api_app)
    try:
        list_response = client.get("/conversations")
        assert list_response.status_code == 200
        list_payload = list_response.json()
        assert list_payload["total"] == 1
        assert list_payload["conversations"][0]["title"] == "Rollout Thread"
        assert list_payload["conversations"][0]["binding_count"] == 1
        assert list_payload["conversations"][0]["message_count"] == 1

        search_response = client.get("/conversations/search", params={"q": "rollout"})
        assert search_response.status_code == 200
        search_payload = search_response.json()
        assert search_payload["total"] == 1
        assert search_payload["conversations"][0]["id"] == list_payload["conversations"][0]["id"]

        conversation_id = list_payload["conversations"][0]["id"]
        detail_response = client.get(f"/conversations/{conversation_id}")
        assert detail_response.status_code == 200
        detail_payload = detail_response.json()
        assert detail_payload["bindings"][0]["provider"] == "gmail"
        assert detail_payload["bindings"][0]["binding_scope"] == "owner@gmail.com"

        messages_response = client.get(f"/conversations/{conversation_id}/messages")
        assert messages_response.status_code == 200
        messages_payload = messages_response.json()
        assert messages_payload["total"] == 1
        assert messages_payload["messages"][0]["content"] == "Please review the rollout notes"

        hidden_response = client.get(f"/conversations/{other_conversation_id}")
        assert hidden_response.status_code == 404
    finally:
        api_app.dependency_overrides.clear()
