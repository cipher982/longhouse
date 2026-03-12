"""Persistence helpers for human-visible conversations."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from zerg.models.conversation import Conversation
from zerg.models.conversation import ConversationBinding
from zerg.models.conversation import ConversationMessage


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_scope(value: str | None) -> str:
    return (value or "").strip()


def _derive_title_from_content(content: str, max_chars: int = 80) -> str | None:
    text = " ".join((content or "").split())
    if not text:
        return None
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


class ConversationService:
    """High-level conversation API for surface-backed user threads."""

    @staticmethod
    def get_conversation(db: Session, *, owner_id: int, conversation_id: int) -> Conversation | None:
        return (
            db.query(Conversation)
            .filter(
                Conversation.id == conversation_id,
                Conversation.owner_id == owner_id,
            )
            .first()
        )

    @staticmethod
    def get_conversation_by_binding(
        db: Session,
        *,
        owner_id: int,
        surface_id: str,
        external_conversation_id: str,
        provider: str = "default",
        binding_scope: str | None = None,
    ) -> Conversation | None:
        binding = (
            db.query(ConversationBinding)
            .filter(
                ConversationBinding.owner_id == owner_id,
                ConversationBinding.surface_id == surface_id,
                ConversationBinding.provider == provider,
                ConversationBinding.binding_scope == _normalize_scope(binding_scope),
                ConversationBinding.external_conversation_id == external_conversation_id,
            )
            .first()
        )
        return binding.conversation if binding is not None else None

    @staticmethod
    def get_or_create_by_binding(
        db: Session,
        *,
        owner_id: int,
        kind: str,
        surface_id: str,
        provider: str = "default",
        external_conversation_id: str,
        binding_scope: str | None = None,
        connector_id: int | None = None,
        title: str | None = None,
        status: str = "active",
        binding_metadata: dict | None = None,
        conversation_metadata: dict | None = None,
    ) -> Conversation:
        binding = (
            db.query(ConversationBinding)
            .filter(
                ConversationBinding.owner_id == owner_id,
                ConversationBinding.surface_id == surface_id,
                ConversationBinding.provider == provider,
                ConversationBinding.binding_scope == _normalize_scope(binding_scope),
                ConversationBinding.external_conversation_id == external_conversation_id,
            )
            .first()
        )
        if binding is not None:
            if connector_id is not None and binding.connector_id != connector_id:
                binding.connector_id = connector_id
            if binding_metadata:
                merged_binding = dict(binding.binding_metadata or {})
                merged_binding.update(binding_metadata)
                binding.binding_metadata = merged_binding

            conversation = binding.conversation
            if title and not conversation.title:
                conversation.title = title
            if conversation_metadata:
                merged_conversation = dict(conversation.conversation_metadata or {})
                merged_conversation.update(conversation_metadata)
                conversation.conversation_metadata = merged_conversation
            db.commit()
            db.refresh(conversation)
            return conversation

        conversation = Conversation(
            owner_id=owner_id,
            kind=kind,
            title=title,
            status=status,
            conversation_metadata=conversation_metadata or None,
        )
        db.add(conversation)
        db.flush()

        binding = ConversationBinding(
            conversation_id=conversation.id,
            owner_id=owner_id,
            surface_id=surface_id,
            provider=provider,
            binding_scope=_normalize_scope(binding_scope),
            connector_id=connector_id,
            external_conversation_id=external_conversation_id,
            binding_metadata=binding_metadata or None,
        )
        db.add(binding)
        db.commit()
        db.refresh(conversation)
        return conversation

    @staticmethod
    def append_message(
        db: Session,
        *,
        owner_id: int,
        conversation_id: int,
        role: str,
        content: str,
        direction: str = "incoming",
        sender_kind: str = "human",
        sender_display: str | None = None,
        content_blocks: list | None = None,
        external_message_id: str | None = None,
        parent_message_id: int | None = None,
        archive_relpath: str | None = None,
        message_metadata: dict | None = None,
        internal: bool = False,
        sent_at: datetime | None = None,
    ) -> ConversationMessage:
        conversation = ConversationService.get_conversation(db, owner_id=owner_id, conversation_id=conversation_id)
        if conversation is None:
            raise ValueError("Conversation not found")

        if external_message_id:
            existing = (
                db.query(ConversationMessage)
                .filter(
                    ConversationMessage.conversation_id == conversation_id,
                    ConversationMessage.external_message_id == external_message_id,
                )
                .first()
            )
            if existing is not None:
                updated = False
                if archive_relpath and not existing.archive_relpath:
                    existing.archive_relpath = archive_relpath
                    updated = True
                if message_metadata:
                    merged_metadata = dict(existing.message_metadata or {})
                    merged_metadata.update(message_metadata)
                    existing.message_metadata = merged_metadata
                    updated = True
                if updated:
                    db.commit()
                    db.refresh(existing)
                return existing

        effective_sent_at = sent_at or _utc_now()
        row = ConversationMessage(
            conversation_id=conversation_id,
            role=role,
            direction=direction,
            sender_kind=sender_kind,
            sender_display=sender_display,
            content=content,
            content_blocks=content_blocks or None,
            external_message_id=external_message_id,
            parent_message_id=parent_message_id,
            archive_relpath=archive_relpath,
            message_metadata=message_metadata or None,
            internal=internal,
            sent_at=effective_sent_at,
        )
        db.add(row)

        conversation.last_message_at = effective_sent_at
        if not conversation.title:
            derived_title = _derive_title_from_content(content)
            if derived_title:
                conversation.title = derived_title

        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def list_conversations(
        db: Session,
        *,
        owner_id: int,
        kind: str | None = None,
        status: str | None = "active",
        limit: int = 50,
    ) -> list[Conversation]:
        query = db.query(Conversation).filter(Conversation.owner_id == owner_id)
        if kind:
            query = query.filter(Conversation.kind == kind)
        if status:
            query = query.filter(Conversation.status == status)
        return (
            query.order_by(
                Conversation.last_message_at.desc(),
                Conversation.updated_at.desc(),
                Conversation.id.desc(),
            )
            .limit(limit)
            .all()
        )

    @staticmethod
    def list_messages(
        db: Session,
        *,
        owner_id: int,
        conversation_id: int,
        include_internal: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ConversationMessage]:
        conversation = ConversationService.get_conversation(db, owner_id=owner_id, conversation_id=conversation_id)
        if conversation is None:
            raise ValueError("Conversation not found")

        query = db.query(ConversationMessage).filter(ConversationMessage.conversation_id == conversation_id)
        if not include_internal:
            query = query.filter(ConversationMessage.internal.is_(False))
        return (
            query.order_by(
                ConversationMessage.sent_at.asc(),
                ConversationMessage.id.asc(),
            )
            .offset(offset)
            .limit(limit)
            .all()
        )

    @staticmethod
    def list_bindings(
        db: Session,
        *,
        owner_id: int,
        conversation_id: int,
    ) -> list[ConversationBinding]:
        conversation = ConversationService.get_conversation(db, owner_id=owner_id, conversation_id=conversation_id)
        if conversation is None:
            raise ValueError("Conversation not found")
        return (
            db.query(ConversationBinding)
            .filter(
                ConversationBinding.owner_id == owner_id,
                ConversationBinding.conversation_id == conversation_id,
            )
            .order_by(
                ConversationBinding.surface_id.asc(),
                ConversationBinding.provider.asc(),
                ConversationBinding.binding_scope.asc(),
                ConversationBinding.external_conversation_id.asc(),
            )
            .all()
        )

    @staticmethod
    def search_conversations(
        db: Session,
        *,
        owner_id: int,
        query: str,
        kind: str | None = None,
        limit: int = 20,
    ) -> list[Conversation]:
        needle = (query or "").strip().lower()
        if not needle:
            return []

        search_expr = f"%{needle}%"
        rows = (
            db.query(Conversation)
            .join(ConversationMessage, ConversationMessage.conversation_id == Conversation.id)
            .filter(Conversation.owner_id == owner_id)
            .filter(ConversationMessage.internal.is_(False))
            .filter(
                func.lower(ConversationMessage.content).like(search_expr)
                | func.lower(func.coalesce(Conversation.title, "")).like(search_expr)
            )
        )
        if kind:
            rows = rows.filter(Conversation.kind == kind)

        return (
            rows.distinct()
            .order_by(
                Conversation.last_message_at.desc(),
                Conversation.updated_at.desc(),
            )
            .limit(limit)
            .all()
        )

    @staticmethod
    def count_messages(
        db: Session,
        *,
        owner_id: int,
        conversation_id: int,
        include_internal: bool = False,
    ) -> int:
        conversation = ConversationService.get_conversation(db, owner_id=owner_id, conversation_id=conversation_id)
        if conversation is None:
            raise ValueError("Conversation not found")

        query = db.query(func.count(ConversationMessage.id)).filter(ConversationMessage.conversation_id == conversation_id)
        if not include_internal:
            query = query.filter(ConversationMessage.internal.is_(False))
        return int(query.scalar() or 0)

    @staticmethod
    def count_bindings(
        db: Session,
        *,
        owner_id: int,
        conversation_id: int,
    ) -> int:
        conversation = ConversationService.get_conversation(db, owner_id=owner_id, conversation_id=conversation_id)
        if conversation is None:
            raise ValueError("Conversation not found")
        query = db.query(func.count(ConversationBinding.id)).filter(
            ConversationBinding.owner_id == owner_id,
            ConversationBinding.conversation_id == conversation_id,
        )
        return int(query.scalar() or 0)
