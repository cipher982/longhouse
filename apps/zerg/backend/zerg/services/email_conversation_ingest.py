"""Provider-neutral email conversation ingest helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from zerg.models.conversation import ConversationMessage
from zerg.services.conversation_archive import ConversationArchiveStore
from zerg.services.conversation_service import ConversationService


@dataclass(frozen=True)
class EmailConversationIngest:
    owner_id: int
    provider: str
    external_thread_id: str
    body_text: str
    connector_id: int | None = None
    external_message_id: str | None = None
    subject: str | None = None
    sent_at: datetime | None = None
    role: str = "user"
    direction: str = "incoming"
    sender_kind: str = "human"
    sender_display: str | None = None
    from_email: str | None = None
    to_emails: tuple[str, ...] = ()
    cc_emails: tuple[str, ...] = ()
    raw_bytes: bytes | None = None
    raw_extension: str = "eml"
    provider_metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class EmailConversationIngestResult:
    conversation_id: int
    message_id: int
    archive_relpath: str | None


class EmailConversationIngestService:
    """Map inbound or outbound email messages into conversations."""

    def __init__(self, db: Session, *, archive_store: ConversationArchiveStore | None = None):
        self.db = db
        self.archive_store = archive_store or ConversationArchiveStore()

    def ingest(self, message: EmailConversationIngest) -> EmailConversationIngestResult:
        if not message.external_thread_id.strip():
            raise ValueError("external_thread_id is required")

        conversation_metadata: dict[str, Any] = {
            "email": {
                "provider": message.provider,
            }
        }
        if message.connector_id is not None:
            conversation_metadata["email"]["connector_id"] = message.connector_id

        conversation = ConversationService.get_or_create_by_binding(
            self.db,
            owner_id=message.owner_id,
            kind="email",
            surface_id="email",
            provider=message.provider,
            binding_scope=f"connector:{message.connector_id}" if message.connector_id is not None else "default",
            external_conversation_id=message.external_thread_id,
            connector_id=message.connector_id,
            title=message.subject,
            binding_metadata={"thread_id": message.external_thread_id},
            conversation_metadata=conversation_metadata,
        )

        existing_message = None
        if message.external_message_id:
            existing_message = (
                self.db.query(ConversationMessage)
                .filter(
                    ConversationMessage.conversation_id == conversation.id,
                    ConversationMessage.external_message_id == message.external_message_id,
                )
                .first()
            )

        archive_relpath = None
        archive_metadata = {
            "provider": message.provider,
            "external_thread_id": message.external_thread_id,
            "external_message_id": message.external_message_id,
            "subject": message.subject,
            "from_email": message.from_email,
            "to_emails": list(message.to_emails),
            "cc_emails": list(message.cc_emails),
        }
        should_save_archive = message.raw_bytes is not None and (
            existing_message is None or (existing_message is not None and not existing_message.archive_relpath)
        )
        if should_save_archive:
            archive_relpath = self.archive_store.save_email_raw(
                owner_id=message.owner_id,
                conversation_id=conversation.id,
                external_message_id=message.external_message_id,
                raw_bytes=message.raw_bytes,
                extension=message.raw_extension,
                metadata=archive_metadata,
            )
        elif existing_message is not None:
            archive_relpath = existing_message.archive_relpath

        message_metadata: dict[str, Any] = {
            "email": {
                "provider": message.provider,
                "thread_id": message.external_thread_id,
                "subject": message.subject,
                "from_email": message.from_email,
                "to_emails": list(message.to_emails),
                "cc_emails": list(message.cc_emails),
            }
        }
        if message.connector_id is not None:
            message_metadata["email"]["connector_id"] = message.connector_id
        if message.provider_metadata:
            message_metadata["email"]["provider_metadata"] = message.provider_metadata

        row = ConversationService.append_message(
            self.db,
            owner_id=message.owner_id,
            conversation_id=conversation.id,
            role=message.role,
            content=message.body_text,
            direction=message.direction,
            sender_kind=message.sender_kind,
            sender_display=message.sender_display or message.from_email,
            external_message_id=message.external_message_id,
            archive_relpath=archive_relpath,
            message_metadata=message_metadata,
            sent_at=message.sent_at,
        )

        return EmailConversationIngestResult(
            conversation_id=conversation.id,
            message_id=row.id,
            archive_relpath=archive_relpath,
        )
