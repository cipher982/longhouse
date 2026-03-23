from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi.testclient import TestClient

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.oikos_auth import get_current_oikos_user
from zerg.models import User
from zerg.models.enums import UserRole
from zerg.services.conversation_service import ConversationService


def _make_db(tmp_path):
    db_path = tmp_path / "test_oikos_conversations.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_user(db, email: str) -> User:
    user = User(email=email, role=UserRole.USER.value)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_client(db_session, current_user):
    from zerg.main import api_app
    from zerg.main import app

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    def override_current_user():
        return current_user

    api_app.dependency_overrides[get_db] = override_get_db
    api_app.dependency_overrides[get_current_oikos_user] = override_current_user
    return TestClient(app, backend="asyncio"), api_app


def test_oikos_conversations_list_and_detail_are_owner_scoped(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        owner = _seed_user(db, "owner@test.local")
        other = _seed_user(db, "other@test.local")

        owner_conv = ConversationService.get_or_create_by_binding(
            db,
            owner_id=owner.id,
            kind="email",
            surface_id="email",
            provider="gmail",
            binding_scope="owner@gmail.com",
            external_conversation_id="thread-owner",
            title="Owner thread",
        )
        ConversationService.append_message(
            db,
            owner_id=owner.id,
            conversation_id=owner_conv.id,
            role="user",
            content="Portugal flights",
            external_message_id="owner-msg-1",
        )

        other_conv = ConversationService.get_or_create_by_binding(
            db,
            owner_id=other.id,
            kind="email",
            surface_id="email",
            provider="gmail",
            binding_scope="other@gmail.com",
            external_conversation_id="thread-other",
            title="Other thread",
        )
        ConversationService.append_message(
            db,
            owner_id=other.id,
            conversation_id=other_conv.id,
            role="user",
            content="Secret thread",
            external_message_id="other-msg-1",
        )

        client, api_app_ref = _make_client(db, owner)
        try:
            list_resp = client.get("/api/oikos/conversations")
            assert list_resp.status_code == 200
            assert [row["id"] for row in list_resp.json()] == [owner_conv.id]

            detail_resp = client.get(f"/api/oikos/conversations/{owner_conv.id}")
            assert detail_resp.status_code == 200
            payload = detail_resp.json()
            assert payload["id"] == owner_conv.id
            assert payload["message_count"] == 1
            assert payload["bindings"][0]["external_conversation_id"] == "thread-owner"

            forbidden_resp = client.get(f"/api/oikos/conversations/{other_conv.id}")
            assert forbidden_resp.status_code == 404
        finally:
            api_app_ref.dependency_overrides = {}


def test_oikos_conversation_search_filters_to_owner(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        owner = _seed_user(db, "owner@test.local")
        other = _seed_user(db, "other@test.local")

        owner_conv = ConversationService.get_or_create_by_binding(
            db,
            owner_id=owner.id,
            kind="email",
            surface_id="email",
            provider="gmail",
            binding_scope="owner@gmail.com",
            external_conversation_id="thread-owner",
        )
        ConversationService.append_message(
            db,
            owner_id=owner.id,
            conversation_id=owner_conv.id,
            role="user",
            content="Remember the Portugal flights",
        )

        other_conv = ConversationService.get_or_create_by_binding(
            db,
            owner_id=other.id,
            kind="email",
            surface_id="email",
            provider="gmail",
            binding_scope="other@gmail.com",
            external_conversation_id="thread-other",
        )
        ConversationService.append_message(
            db,
            owner_id=other.id,
            conversation_id=other_conv.id,
            role="user",
            content="Remember the Portugal flights",
        )

        client, api_app_ref = _make_client(db, owner)
        try:
            resp = client.get("/api/oikos/conversations/search?q=Portugal")
            assert resp.status_code == 200
            assert [row["id"] for row in resp.json()] == [owner_conv.id]
        finally:
            api_app_ref.dependency_overrides = {}


def test_oikos_conversation_messages_hide_internal_by_default(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        owner = _seed_user(db, "owner@test.local")
        conversation = ConversationService.get_or_create_by_binding(
            db,
            owner_id=owner.id,
            kind="operator",
            surface_id="operator",
            external_conversation_id="operator:main",
        )
        ConversationService.append_message(
            db,
            owner_id=owner.id,
            conversation_id=conversation.id,
            role="assistant",
            content="Visible note",
            direction="outgoing",
            sender_kind="agent",
        )
        ConversationService.append_message(
            db,
            owner_id=owner.id,
            conversation_id=conversation.id,
            role="system",
            content="Internal note",
            direction="internal",
            sender_kind="system",
            internal=True,
        )

        client, api_app_ref = _make_client(db, owner)
        try:
            visible_resp = client.get(f"/api/oikos/conversations/{conversation.id}/messages")
            assert visible_resp.status_code == 200
            assert [row["content"] for row in visible_resp.json()] == ["Visible note"]

            all_resp = client.get(f"/api/oikos/conversations/{conversation.id}/messages?include_internal=true")
            assert all_resp.status_code == 200
            assert [row["content"] for row in all_resp.json()] == ["Visible note", "Internal note"]
        finally:
            api_app_ref.dependency_overrides = {}
