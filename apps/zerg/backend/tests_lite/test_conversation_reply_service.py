from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models import Connector
from zerg.models import User
from zerg.models.enums import UserRole
from zerg.services import conversation_archive
from zerg.services import gmail_api
from zerg.services.conversation_reply_service import ConversationReplyError
from zerg.services.conversation_reply_service import ConversationReplyRequest
from zerg.services.conversation_reply_service import ConversationReplyService
from zerg.services.conversation_service import ConversationService
from zerg.services.email_conversation_ingest import EmailConversationIngest
from zerg.services.email_conversation_ingest import EmailConversationIngestService
from zerg.utils import crypto


def _make_db(tmp_path):
    db_path = tmp_path / "test_conversation_reply_service.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_user(db, email: str = "owner@gmail.com") -> User:
    user = User(email=email, role=UserRole.USER.value)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_connector(db, *, owner_id: int, email_address: str) -> Connector:
    connector = Connector(
        owner_id=owner_id,
        type="email",
        provider="gmail",
        config={
            "refresh_token": "encrypted-refresh-token",
            "emailAddress": email_address,
        },
    )
    db.add(connector)
    db.commit()
    db.refresh(connector)
    return connector


def _ingest_inbound_message(
    db,
    *,
    owner_id: int,
    connector_id: int,
    archive_root,
    from_email: str = "friend@example.com",
    reply_to_emails: tuple[str, ...] = (),
    to_emails: tuple[str, ...] = ("owner@gmail.com",),
    cc_emails: tuple[str, ...] = (),
) -> int:
    service = EmailConversationIngestService(
        db,
        archive_store=conversation_archive.ConversationArchiveStore(str(archive_root / "conversations")),
    )
    result = service.ingest(
        EmailConversationIngest(
            owner_id=owner_id,
            connector_id=connector_id,
            provider="gmail",
            external_thread_id="thread-123",
            external_message_id="gmail-msg-1",
            subject="Dinner plans",
            body_text="Can you book dinner for 7?",
            from_email=from_email,
            reply_to_emails=reply_to_emails,
            to_emails=to_emails,
            cc_emails=cc_emails,
            raw_bytes=b"inbound-raw-email",
            provider_metadata={
                "gmail_message_id": "gmail-msg-1",
                "thread_id": "thread-123",
                "rfc_message_id": "<gmail-msg-1@example.com>",
                "references": "<older-message@example.com>",
            },
        )
    )
    return result.conversation_id


def test_conversation_reply_service_replies_to_sender_and_appends_message(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    archive_root = tmp_path / "data"

    monkeypatch.setattr(conversation_archive, "get_settings", lambda: SimpleNamespace(data_dir=archive_root))
    monkeypatch.setattr(crypto, "decrypt", lambda value: "refresh-token")
    monkeypatch.setattr(gmail_api, "exchange_refresh_token", lambda refresh_token: "access-token")

    sent = {}

    def fake_send_thread_reply(access_token, *, raw_bytes, thread_id, to_emails, cc_emails=None):
        sent["access_token"] = access_token
        sent["raw_bytes"] = raw_bytes
        sent["thread_id"] = thread_id
        sent["to_emails"] = tuple(to_emails)
        sent["cc_emails"] = tuple(cc_emails or ())
        return {"id": "gmail-out-1", "threadId": thread_id}

    monkeypatch.setattr(gmail_api, "send_thread_reply", fake_send_thread_reply)

    with SessionLocal() as db:
        user = _seed_user(db)
        connector = _seed_connector(db, owner_id=user.id, email_address="owner@gmail.com")
        conversation_id = _ingest_inbound_message(
            db,
            owner_id=user.id,
            connector_id=connector.id,
            archive_root=archive_root,
            reply_to_emails=("assistant+reply@example.com",),
            cc_emails=("team@example.com",),
        )

        service = ConversationReplyService(db)
        result = service.reply(
            ConversationReplyRequest(
                owner_id=user.id,
                conversation_id=conversation_id,
                body_text="Booked for 7pm.",
            )
        )

        messages = ConversationService.list_messages(
            db,
            owner_id=user.id,
            conversation_id=conversation_id,
        )

        assert result.external_message_id == "gmail-out-1"
        assert result.to_emails == ("assistant+reply@example.com",)
        assert result.cc_emails == ()
        assert sent["access_token"] == "access-token"
        assert sent["thread_id"] == "thread-123"
        assert sent["to_emails"] == ("assistant+reply@example.com",)
        assert len(messages) == 2
        assert messages[-1].direction == "outgoing"
        assert messages[-1].content == "Booked for 7pm."
        assert messages[-1].external_message_id == "gmail-out-1"
        assert messages[-1].message_metadata["email"]["provider_metadata"]["in_reply_to"] == "<gmail-msg-1@example.com>"
        assert messages[-1].archive_relpath is not None
        assert (archive_root / "conversations" / messages[-1].archive_relpath).exists()


def test_conversation_reply_service_reply_all_keeps_existing_thread_participants(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    archive_root = tmp_path / "data"

    monkeypatch.setattr(conversation_archive, "get_settings", lambda: SimpleNamespace(data_dir=archive_root))
    monkeypatch.setattr(crypto, "decrypt", lambda value: "refresh-token")
    monkeypatch.setattr(gmail_api, "exchange_refresh_token", lambda refresh_token: "access-token")

    sent = {}

    def fake_send_thread_reply(access_token, *, raw_bytes, thread_id, to_emails, cc_emails=None):
        sent["to_emails"] = tuple(to_emails)
        sent["cc_emails"] = tuple(cc_emails or ())
        return {"id": "gmail-out-2", "threadId": thread_id}

    monkeypatch.setattr(gmail_api, "send_thread_reply", fake_send_thread_reply)

    with SessionLocal() as db:
        user = _seed_user(db)
        connector = _seed_connector(db, owner_id=user.id, email_address="owner@gmail.com")
        conversation_id = _ingest_inbound_message(
            db,
            owner_id=user.id,
            connector_id=connector.id,
            archive_root=archive_root,
            from_email="friend@example.com",
            to_emails=("owner@gmail.com", "teammate@example.com"),
            cc_emails=("manager@example.com", "owner@gmail.com"),
        )

        result = ConversationReplyService(db).reply(
            ConversationReplyRequest(
                owner_id=user.id,
                conversation_id=conversation_id,
                body_text="I handled it.",
                reply_all=True,
            )
        )

        assert result.to_emails == ("friend@example.com",)
        assert result.cc_emails == ("teammate@example.com", "manager@example.com")
        assert sent["to_emails"] == ("friend@example.com",)
        assert sent["cc_emails"] == ("teammate@example.com", "manager@example.com")


def test_conversation_reply_service_rejects_non_email_conversations(tmp_path):
    SessionLocal = _make_db(tmp_path)

    with SessionLocal() as db:
        user = _seed_user(db)
        conversation = ConversationService.get_or_create_by_binding(
            db,
            owner_id=user.id,
            kind="operator",
            surface_id="operator",
            external_conversation_id="operator:main",
        )

        with pytest.raises(ConversationReplyError, match="Only email conversations support replies right now"):
            ConversationReplyService(db).reply(
                ConversationReplyRequest(
                    owner_id=user.id,
                    conversation_id=conversation.id,
                    body_text="No-op",
                )
            )
