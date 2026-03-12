from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite://")

from datetime import datetime
from datetime import timezone

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models import Connector
from zerg.models import User
from zerg.models.enums import UserRole
from zerg.services.conversation_archive import ConversationArchiveStore
from zerg.services.conversation_service import ConversationService
from zerg.services.email_conversation_ingest import EmailConversationIngest
from zerg.services.email_conversation_ingest import EmailConversationIngestService


def _make_db(tmp_path):
    db_path = tmp_path / "test_email_conversation_ingest.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_user(db, email: str = "owner@test.local") -> User:
    user = User(email=email, role=UserRole.USER.value)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_connector(db, *, owner_id: int, provider: str = "gmail") -> Connector:
    connector = Connector(owner_id=owner_id, type="email", provider=provider, config={})
    db.add(connector)
    db.commit()
    db.refresh(connector)
    return connector


def test_email_conversation_ingest_creates_conversation_and_archive(tmp_path):
    SessionLocal = _make_db(tmp_path)
    archive_root = tmp_path / "conversation-archive"

    with SessionLocal() as db:
        user = _seed_user(db)
        connector = _seed_connector(db, owner_id=user.id)
        service = EmailConversationIngestService(
            db,
            archive_store=ConversationArchiveStore(str(archive_root)),
        )

        result = service.ingest(
            EmailConversationIngest(
                owner_id=user.id,
                connector_id=connector.id,
                provider="gmail",
                external_thread_id="thread-123",
                external_message_id="msg-123",
                subject="Dinner plans",
                body_text="Can you book dinner for 7?",
                from_email="david@drose.io",
                to_emails=("oikos@agents.drose.io",),
                raw_bytes=b"raw-email-payload",
                sent_at=datetime(2026, 3, 12, 18, 30, tzinfo=timezone.utc),
            )
        )

        conversation = ConversationService.get_conversation(
            db,
            owner_id=user.id,
            conversation_id=result.conversation_id,
        )
        messages = ConversationService.list_messages(
            db,
            owner_id=user.id,
            conversation_id=result.conversation_id,
        )

        assert conversation is not None
        assert conversation.kind == "email"
        assert conversation.title == "Dinner plans"
        assert len(messages) == 1
        assert messages[0].archive_relpath == result.archive_relpath
        assert messages[0].message_metadata["email"]["thread_id"] == "thread-123"

        archive_path = archive_root / result.archive_relpath
        assert archive_path.exists()
        assert archive_path.read_bytes() == b"raw-email-payload"


def test_email_conversation_ingest_reuses_thread_binding(tmp_path):
    SessionLocal = _make_db(tmp_path)
    archive_root = tmp_path / "conversation-archive"

    with SessionLocal() as db:
        user = _seed_user(db)
        connector = _seed_connector(db, owner_id=user.id)
        service = EmailConversationIngestService(
            db,
            archive_store=ConversationArchiveStore(str(archive_root)),
        )

        first = service.ingest(
            EmailConversationIngest(
                owner_id=user.id,
                connector_id=connector.id,
                provider="gmail",
                external_thread_id="thread-abc",
                external_message_id="msg-1",
                subject="Trip ideas",
                body_text="Let's go to Portugal",
            )
        )
        second = service.ingest(
            EmailConversationIngest(
                owner_id=user.id,
                connector_id=connector.id,
                provider="gmail",
                external_thread_id="thread-abc",
                external_message_id="msg-2",
                subject="Re: Trip ideas",
                body_text="Also check flights from NYC",
                from_email="friend@example.com",
            )
        )

        messages = ConversationService.list_messages(
            db,
            owner_id=user.id,
            conversation_id=first.conversation_id,
        )

        assert first.conversation_id == second.conversation_id
        assert len(messages) == 2
        assert messages[1].content == "Also check flights from NYC"
