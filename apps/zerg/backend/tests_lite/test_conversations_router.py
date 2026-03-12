"""HTTP tests for canonical conversations APIs."""

import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite://")

import tiktoken
from fastapi.testclient import TestClient

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models import Connector
from zerg.models.user import User
from zerg.services import conversation_archive
from zerg.services import gmail_api
from zerg.services.conversation_service import ConversationService
from zerg.services.email_conversation_ingest import EmailConversationIngest
from zerg.services.email_conversation_ingest import EmailConversationIngestService
from zerg.utils import crypto


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


class _DummyEncoding:
    def encode(self, text: str):
        return list(text.encode("utf-8"))

    def decode(self, tokens):
        return bytes(tokens).decode("utf-8", errors="ignore")


def _stub_tiktoken(monkeypatch):
    monkeypatch.setattr(tiktoken, "get_encoding", lambda _name: _DummyEncoding())


def test_conversations_list_search_detail_and_messages(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    _stub_tiktoken(monkeypatch)

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


def test_conversations_activity_is_owner_scoped(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    _stub_tiktoken(monkeypatch)

    with SessionLocal() as db:
        owner = _seed_user(db, 1, "owner@test.local")
        other = _seed_user(db, 2, "other@test.local")

        web_conversation = ConversationService.get_or_create_by_binding(
            db,
            owner_id=owner.id,
            kind="web",
            surface_id="web",
            external_conversation_id="web:main",
        )
        telegram_conversation = ConversationService.get_or_create_by_binding(
            db,
            owner_id=owner.id,
            kind="telegram",
            surface_id="telegram",
            external_conversation_id="telegram:42",
        )
        other_conversation = ConversationService.get_or_create_by_binding(
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
            owner_id=owner.id,
            conversation_id=web_conversation.id,
            role="user",
            content="Web question",
            message_metadata={"surface": {"origin_surface_id": "web", "delivery_surface_id": "web"}},
        )
        ConversationService.append_message(
            db,
            owner_id=owner.id,
            conversation_id=telegram_conversation.id,
            role="assistant",
            content="Telegram follow-up",
            message_metadata={"surface": {"origin_surface_id": "telegram", "delivery_surface_id": "telegram"}},
        )
        ConversationService.append_message(
            db,
            owner_id=other.id,
            conversation_id=other_conversation.id,
            role="assistant",
            content="Private message",
            message_metadata={"surface": {"origin_surface_id": "email", "delivery_surface_id": "email"}},
        )

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
        response = client.get("/conversations/activity", params={"limit": 10})
        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 2
        assert [message["content"] for message in payload["messages"]] == [
            "Web question",
            "Telegram follow-up",
        ]
        assert payload["messages"][1]["message_metadata"]["surface"]["origin_surface_id"] == "telegram"
    finally:
        api_app.dependency_overrides.clear()


def test_conversation_reply_endpoint_sends_reply_and_returns_message(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    archive_root = tmp_path / "data"
    _stub_tiktoken(monkeypatch)

    monkeypatch.setattr(conversation_archive, "get_settings", lambda: SimpleNamespace(data_dir=archive_root))
    monkeypatch.setattr(crypto, "decrypt", lambda value: "refresh-token")
    monkeypatch.setattr(gmail_api, "exchange_refresh_token", lambda refresh_token: "access-token")
    monkeypatch.setattr(
        gmail_api,
        "send_thread_reply",
        lambda access_token, **kwargs: {"id": "gmail-out-1", "threadId": kwargs["thread_id"]},
    )

    with SessionLocal() as db:
        owner = _seed_user(db, 1, "owner@gmail.com")
        connector = Connector(
            owner_id=owner.id,
            type="email",
            provider="gmail",
            config={"refresh_token": "encrypted-refresh-token", "emailAddress": "owner@gmail.com"},
        )
        db.add(connector)
        db.commit()
        db.refresh(connector)

        ingest = EmailConversationIngestService(
            db,
            archive_store=conversation_archive.ConversationArchiveStore(str(archive_root / "conversations")),
        )
        conversation_id = ingest.ingest(
            EmailConversationIngest(
                owner_id=owner.id,
                connector_id=connector.id,
                provider="gmail",
                external_thread_id="thread-123",
                external_message_id="gmail-msg-1",
                subject="Dinner plans",
                body_text="Can you book dinner for 7?",
                from_email="friend@example.com",
                to_emails=("owner@gmail.com",),
                provider_metadata={
                    "gmail_message_id": "gmail-msg-1",
                    "thread_id": "thread-123",
                    "rfc_message_id": "<gmail-msg-1@example.com>",
                },
            )
        ).conversation_id

    from zerg.dependencies.auth import get_current_user
    from zerg.main import api_app

    def override_db():
        with SessionLocal() as db:
            yield db

    def override_user():
        return User(id=1, email="owner@gmail.com", role="USER")

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[get_current_user] = override_user

    client = TestClient(api_app)
    try:
        response = client.post(
            f"/conversations/{conversation_id}/reply",
            json={"body": "Booked for 7pm.", "reply_all": False},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["thread_id"] == "thread-123"
        assert payload["to_emails"] == ["friend@example.com"]
        assert payload["message"]["content"] == "Booked for 7pm."
        assert payload["message"]["external_message_id"] == "gmail-out-1"
    finally:
        api_app.dependency_overrides.clear()
