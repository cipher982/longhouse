from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite://")

from email.message import EmailMessage
from types import SimpleNamespace

import pytest

import zerg.database as database
from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.email.providers import GmailProvider
from zerg.models import Connector
from zerg.models import User
from zerg.models.enums import UserRole
from zerg.services import conversation_archive
from zerg.services import gmail_api
from zerg.services.conversation_service import ConversationService
from zerg.utils import crypto


def _make_db(tmp_path):
    db_path = tmp_path / "test_gmail_provider_conversations.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_user(db, email: str = "owner@gmail.com") -> User:
    user = User(email=email, role=UserRole.USER.value)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_connector(db, *, owner_id: int, history_id: int, email_address: str) -> Connector:
    connector = Connector(
        owner_id=owner_id,
        type="email",
        provider="gmail",
        config={
            "refresh_token": "encrypted-refresh-token",
            "history_id": history_id,
            "emailAddress": email_address,
        },
    )
    db.add(connector)
    db.commit()
    db.refresh(connector)
    return connector


def _build_email_bytes(
    *,
    subject: str,
    from_header: str,
    to_header: str,
    body_text: str,
    cc_header: str | None = None,
    reply_to_header: str | None = None,
    message_id: str = "<message@test.local>",
) -> bytes:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_header
    message["To"] = to_header
    message["Date"] = "Thu, 12 Mar 2026 18:30:00 +0000"
    message["Message-ID"] = message_id
    if cc_header:
        message["Cc"] = cc_header
    if reply_to_header:
        message["Reply-To"] = reply_to_header
    message.set_content(body_text)
    return message.as_bytes()


@pytest.mark.asyncio
async def test_process_connector_ingests_incoming_gmail_into_conversation(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    archive_root = tmp_path / "data"

    with SessionLocal() as db:
        user = _seed_user(db)
        connector = _seed_connector(
            db,
            owner_id=user.id,
            history_id=100,
            email_address="owner@gmail.com",
        )
        owner_id = user.id
        connector_id = connector.id

    monkeypatch.setattr(database, "default_session_factory", SessionLocal)
    monkeypatch.setattr(conversation_archive, "get_settings", lambda: SimpleNamespace(data_dir=archive_root))
    monkeypatch.setattr(crypto, "decrypt", lambda value: "refresh-token")

    async def fake_exchange_refresh_token(_refresh_token: str) -> str:
        return "access-token"

    async def fake_list_history(_access_token: str, start_history_id: int):
        assert start_history_id == 100
        return [
            {
                "id": "101",
                "messagesAdded": [
                    {
                        "message": {
                            "id": "gmail-msg-1",
                        }
                    }
                ],
            }
        ]

    async def fake_get_message_metadata(_access_token: str, msg_id: str):
        assert msg_id == "gmail-msg-1"
        return {
            "id": msg_id,
            "labelIds": ["INBOX"],
            "headers": {
                "From": "Friend <friend@example.com>",
                "Subject": "Dinner plans",
            },
        }

    async def fake_get_message_raw(_access_token: str, msg_id: str):
        assert msg_id == "gmail-msg-1"
        return {
            "id": msg_id,
            "threadId": "thread-123",
            "labelIds": ["INBOX"],
            "historyId": "101",
            "internalDate": "1773340200000",
            "snippet": "Can you book dinner for 7?",
            "raw_bytes": _build_email_bytes(
                subject="Dinner plans",
                from_header="Friend <friend@example.com>",
                to_header="owner@gmail.com",
                body_text="Can you book dinner for 7?",
                cc_header="team@example.com",
                reply_to_header="Assistant <assistant+reply@example.com>",
                message_id="<gmail-msg-1@example.com>",
            ),
        }

    monkeypatch.setattr(gmail_api, "async_exchange_refresh_token", fake_exchange_refresh_token)
    monkeypatch.setattr(gmail_api, "async_list_history", fake_list_history)
    monkeypatch.setattr(gmail_api, "async_get_message_metadata", fake_get_message_metadata)
    monkeypatch.setattr(gmail_api, "async_get_message_raw", fake_get_message_raw)

    await GmailProvider().process_connector(connector_id)

    with SessionLocal() as db:
        conversation = ConversationService.list_conversations(
            db,
            owner_id=owner_id,
            kind="email",
            limit=10,
        )[0]
        messages = ConversationService.list_messages(
            db,
            owner_id=owner_id,
            conversation_id=conversation.id,
        )
        refreshed_connector = db.get(Connector, connector_id)

        assert conversation.title == "Dinner plans"
        assert messages[0].direction == "incoming"
        assert messages[0].content == "Can you book dinner for 7?"
        assert (
            messages[0].message_metadata["email"]["provider_metadata"]["rfc_message_id"]
            == "<gmail-msg-1@example.com>"
        )
        assert messages[0].message_metadata["email"]["reply_to_emails"] == ["assistant+reply@example.com"]
        assert refreshed_connector is not None
        assert refreshed_connector.config["history_id"] == 101

        archive_path = archive_root / "conversations" / messages[0].archive_relpath
        assert archive_path.exists()
        assert archive_path.read_bytes()


@pytest.mark.asyncio
async def test_process_connector_dedupes_replayed_gmail_message_and_marks_outgoing(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    archive_root = tmp_path / "data"

    with SessionLocal() as db:
        user = _seed_user(db)
        connector = _seed_connector(
            db,
            owner_id=user.id,
            history_id=100,
            email_address="owner@gmail.com",
        )
        owner_id = user.id
        connector_id = connector.id

    history_calls: list[int] = []
    monkeypatch.setattr(database, "default_session_factory", SessionLocal)
    monkeypatch.setattr(conversation_archive, "get_settings", lambda: SimpleNamespace(data_dir=archive_root))
    monkeypatch.setattr(crypto, "decrypt", lambda value: "refresh-token")

    async def fake_exchange_refresh_token(_refresh_token: str) -> str:
        return "access-token"

    async def fake_list_history(_access_token: str, start_history_id: int):
        history_calls.append(start_history_id)
        return [
            {
                "id": "101",
                "messagesAdded": [
                    {
                        "message": {
                            "id": "gmail-msg-out",
                        }
                    }
                ],
            }
        ]

    async def fake_get_message_metadata(_access_token: str, msg_id: str):
        return {
            "id": msg_id,
            "labelIds": ["SENT"],
            "headers": {
                "From": "Owner <owner@gmail.com>",
                "Subject": "Re: Dinner plans",
            },
        }

    async def fake_get_message_raw(_access_token: str, msg_id: str):
        return {
            "id": msg_id,
            "threadId": "thread-123",
            "labelIds": ["SENT"],
            "historyId": "101",
            "internalDate": "1773340500000",
            "snippet": "Booked for 7pm.",
            "raw_bytes": _build_email_bytes(
                subject="Re: Dinner plans",
                from_header="Owner <owner@gmail.com>",
                to_header="friend@example.com",
                body_text="Booked for 7pm.",
                message_id="<gmail-msg-out@example.com>",
            ),
        }

    monkeypatch.setattr(gmail_api, "async_exchange_refresh_token", fake_exchange_refresh_token)
    monkeypatch.setattr(gmail_api, "async_list_history", fake_list_history)
    monkeypatch.setattr(gmail_api, "async_get_message_metadata", fake_get_message_metadata)
    monkeypatch.setattr(gmail_api, "async_get_message_raw", fake_get_message_raw)

    provider = GmailProvider()
    await provider.process_connector(connector_id)
    await provider.process_connector(connector_id)

    with SessionLocal() as db:
        conversation = ConversationService.list_conversations(
            db,
            owner_id=owner_id,
            kind="email",
            limit=10,
        )[0]
        messages = ConversationService.list_messages(
            db,
            owner_id=owner_id,
            conversation_id=conversation.id,
        )
        raw_dir = archive_root / "conversations" / str(owner_id) / str(conversation.id) / "raw"
        refreshed_connector = db.get(Connector, connector_id)

        assert history_calls == [100, 101]
        assert len(messages) == 1
        assert messages[0].direction == "outgoing"
        assert messages[0].content == "Booked for 7pm."
        assert len(list(raw_dir.glob("*.eml"))) == 1
        assert refreshed_connector is not None
        assert refreshed_connector.config["history_id"] == 101
