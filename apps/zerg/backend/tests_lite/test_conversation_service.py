"""Tests for the conversation service foundation."""

import os

os.environ.setdefault("DATABASE_URL", "sqlite://")

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.user import User
from zerg.services.conversation_service import ConversationService


def _make_db(tmp_path):
    db_path = tmp_path / "test_conversations_service.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_user(db, user_id: int, email: str) -> User:
    user = User(id=user_id, email=email, role="USER")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_get_or_create_by_binding_reuses_existing_conversation(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        owner = _seed_user(db, 1, "owner@test.local")

        first = ConversationService.get_or_create_by_binding(
            db,
            owner_id=owner.id,
            kind="email",
            surface_id="email",
            provider="gmail",
            binding_scope="owner@gmail.com",
            external_conversation_id="thread-123",
            title="Inbox thread",
            conversation_metadata={"source": "gmail"},
            binding_metadata={"thread_id": "thread-123"},
        )
        second = ConversationService.get_or_create_by_binding(
            db,
            owner_id=owner.id,
            kind="email",
            surface_id="email",
            provider="gmail",
            binding_scope="owner@gmail.com",
            external_conversation_id="thread-123",
            title="Updated title ignored once set",
            conversation_metadata={"folder": "INBOX"},
            binding_metadata={"label": "important"},
        )

        assert second.id == first.id
        assert second.title == "Inbox thread"

        bindings = ConversationService.list_bindings(
            db,
            owner_id=owner.id,
            conversation_id=first.id,
        )
        assert len(bindings) == 1
        assert bindings[0].provider == "gmail"
        assert bindings[0].binding_scope == "owner@gmail.com"
        assert bindings[0].binding_metadata["thread_id"] == "thread-123"
        assert bindings[0].binding_metadata["label"] == "important"


def test_append_message_dedupes_external_message_ids_and_updates_last_message(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        owner = _seed_user(db, 1, "owner@test.local")
        conversation = ConversationService.get_or_create_by_binding(
            db,
            owner_id=owner.id,
            kind="email",
            surface_id="email",
            provider="gmail",
            binding_scope="owner@gmail.com",
            external_conversation_id="thread-456",
        )

        first = ConversationService.append_message(
            db,
            owner_id=owner.id,
            conversation_id=conversation.id,
            role="external",
            direction="incoming",
            sender_kind="external_contact",
            sender_display="Alice <alice@example.com>",
            content="Need help with the rollout",
            external_message_id="msg-1",
        )
        duplicate = ConversationService.append_message(
            db,
            owner_id=owner.id,
            conversation_id=conversation.id,
            role="external",
            direction="incoming",
            sender_kind="external_contact",
            sender_display="Alice <alice@example.com>",
            content="Need help with the rollout",
            external_message_id="msg-1",
            message_metadata={"deduped": True},
        )

        assert duplicate.id == first.id
        assert ConversationService.count_messages(
            db,
            owner_id=owner.id,
            conversation_id=conversation.id,
        ) == 1

        refreshed = ConversationService.get_conversation(
            db,
            owner_id=owner.id,
            conversation_id=conversation.id,
        )
        assert refreshed is not None
        assert refreshed.last_message_at is not None


def test_search_conversations_scopes_to_owner_and_matches_content(tmp_path):
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
            conversation_id=owner_conversation.id,
            role="external",
            direction="incoming",
            sender_kind="external_contact",
            content="Status on the email rollout design?",
        )
        ConversationService.append_message(
            db,
            owner_id=other.id,
            conversation_id=other_conversation.id,
            role="external",
            direction="incoming",
            sender_kind="external_contact",
            content="Private conversation for other user",
        )

        matches = ConversationService.search_conversations(
            db,
            owner_id=owner.id,
            query="rollout",
        )
        assert [conversation.id for conversation in matches] == [owner_conversation.id]
