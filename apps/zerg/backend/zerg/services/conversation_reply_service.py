"""Reply helpers for canonical email conversations."""

from __future__ import annotations

from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Any

from sqlalchemy.orm import Session

from zerg.models.connector import Connector
from zerg.models.conversation import ConversationBinding
from zerg.models.conversation import ConversationMessage
from zerg.models.user import User
from zerg.services import gmail_api
from zerg.services.conversation_service import ConversationService
from zerg.services.email_conversation_ingest import EmailConversationIngest
from zerg.services.email_conversation_ingest import EmailConversationIngestService
from zerg.utils import crypto


def _normalize_email(value: str | None) -> str:
    return (value or "").strip().lower()


def _dedupe_emails(values: list[str], *, exclude: set[str] | None = None) -> tuple[str, ...]:
    exclude = exclude or set()
    seen: set[str] = set()
    deduped: list[str] = []
    for raw in values:
        candidate = (raw or "").strip()
        normalized = _normalize_email(candidate)
        if not normalized or normalized in exclude or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(candidate)
    return tuple(deduped)


def _reply_subject(subject: str | None) -> str:
    cleaned = (subject or "").strip()
    if not cleaned:
        return "Re:"
    if cleaned.lower().startswith("re:"):
        return cleaned
    return f"Re: {cleaned}"


def _merge_references(references: str | None, in_reply_to: str | None) -> str | None:
    parts = [part for part in (references or "").split() if part]
    if in_reply_to and in_reply_to not in parts:
        parts.append(in_reply_to)
    return " ".join(parts) if parts else None


def _send_result_value(result: Any, key: str, default: Any = None) -> Any:
    if result is None:
        return default
    if isinstance(result, dict):
        return result.get(key, default)
    return getattr(result, key, default)


class ConversationReplyError(ValueError):
    """Structured error for canonical conversation replies."""

    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class ConversationReplyRequest:
    owner_id: int
    conversation_id: int
    body_text: str
    reply_all: bool = False
    role: str = "user"
    sender_kind: str = "human"
    sender_display: str | None = None


@dataclass(frozen=True)
class ConversationReplyResult:
    conversation_id: int
    message_id: int
    external_message_id: str | None
    provider: str
    thread_id: str
    subject: str
    to_emails: tuple[str, ...]
    cc_emails: tuple[str, ...]


class ConversationReplyService:
    """Reply to an existing email conversation using the bound provider."""

    def __init__(self, db: Session):
        self.db = db

    def reply(self, request: ConversationReplyRequest) -> ConversationReplyResult:
        body_text = (request.body_text or "").strip()
        if not body_text:
            raise ConversationReplyError("body_text is required")

        conversation = ConversationService.get_conversation(
            self.db,
            owner_id=request.owner_id,
            conversation_id=request.conversation_id,
        )
        if conversation is None:
            raise ConversationReplyError("Conversation not found", status_code=404)
        if conversation.kind != "email":
            raise ConversationReplyError("Only email conversations support replies right now")

        binding = self._pick_email_binding(request.owner_id, conversation.id)
        if binding.provider != "gmail":
            raise ConversationReplyError(f"Reply provider not supported yet: {binding.provider}")

        connector = self._get_connector(request.owner_id, binding)
        mailbox_email = self._resolve_mailbox_email(request.owner_id, connector)
        anchor = self._pick_anchor_message(request.owner_id, conversation.id, mailbox_email)
        thread_id = (binding.external_conversation_id or "").strip()
        if not thread_id:
            raise ConversationReplyError("Email conversation is missing a thread binding")

        email_meta = (anchor.message_metadata or {}).get("email") or {}
        provider_meta = email_meta.get("provider_metadata") or {}
        subject = _reply_subject(email_meta.get("subject") or conversation.title)
        in_reply_to = provider_meta.get("rfc_message_id")
        if not in_reply_to:
            raise ConversationReplyError("Email conversation is missing Message-ID metadata for the reply anchor")
        references = _merge_references(provider_meta.get("references"), in_reply_to)

        to_emails, cc_emails = self._resolve_recipients(
            email_meta=email_meta,
            mailbox_email=mailbox_email,
            reply_all=request.reply_all,
        )
        raw_bytes, message_id_header = self._build_raw_reply(
            subject=subject,
            body_text=body_text,
            from_email=mailbox_email,
            to_emails=to_emails,
            cc_emails=cc_emails,
            in_reply_to=in_reply_to,
            references=references,
        )

        encrypted_refresh_token = (connector.config or {}).get("refresh_token")
        if not encrypted_refresh_token:
            raise ConversationReplyError("Email connector is missing a refresh token")
        refresh_token = crypto.decrypt(encrypted_refresh_token)
        access_token = gmail_api.exchange_refresh_token(refresh_token)
        send_result = gmail_api.send_thread_reply(
            access_token,
            raw_bytes=raw_bytes,
            thread_id=thread_id,
            to_emails=to_emails,
            cc_emails=cc_emails,
        )
        if send_result is None:
            raise ConversationReplyError("Failed to send Gmail reply")

        external_message_id = _send_result_value(send_result, "id") or _send_result_value(
            send_result,
            "message_id",
        )
        resolved_thread_id = str(_send_result_value(send_result, "threadId") or _send_result_value(send_result, "thread_id") or thread_id)

        ingest = EmailConversationIngestService(self.db)
        stored = ingest.ingest(
            EmailConversationIngest(
                owner_id=request.owner_id,
                connector_id=connector.id,
                provider=binding.provider,
                external_thread_id=resolved_thread_id,
                external_message_id=str(external_message_id) if external_message_id else None,
                subject=subject,
                body_text=body_text,
                role=request.role,
                direction="outgoing",
                sender_kind=request.sender_kind,
                sender_display=request.sender_display or mailbox_email,
                from_email=mailbox_email,
                to_emails=to_emails,
                cc_emails=cc_emails,
                raw_bytes=raw_bytes,
                raw_extension="eml",
                provider_metadata={
                    "gmail_message_id": external_message_id,
                    "thread_id": resolved_thread_id,
                    "rfc_message_id": message_id_header,
                    "references": references,
                    "in_reply_to": in_reply_to,
                },
            )
        )
        return ConversationReplyResult(
            conversation_id=conversation.id,
            message_id=stored.message_id,
            external_message_id=str(external_message_id) if external_message_id else None,
            provider=binding.provider,
            thread_id=resolved_thread_id,
            subject=subject,
            to_emails=to_emails,
            cc_emails=cc_emails,
        )

    def _pick_email_binding(self, owner_id: int, conversation_id: int) -> ConversationBinding:
        bindings = ConversationService.list_bindings(
            self.db,
            owner_id=owner_id,
            conversation_id=conversation_id,
        )
        for binding in bindings:
            if binding.surface_id == "email":
                return binding
        raise ConversationReplyError("Conversation is missing an email binding")

    def _get_connector(self, owner_id: int, binding: ConversationBinding) -> Connector:
        if binding.connector_id is None:
            raise ConversationReplyError("Email conversation is missing a connector binding")
        connector = self.db.get(Connector, binding.connector_id)
        if connector is None:
            raise ConversationReplyError("Email connector not found", status_code=404)
        if connector.owner_id != owner_id or connector.provider != "gmail" or connector.type != "email":
            raise ConversationReplyError("Email connector is not valid for this conversation")
        return connector

    def _resolve_mailbox_email(self, owner_id: int, connector: Connector) -> str:
        mailbox_email = _normalize_email((connector.config or {}).get("emailAddress"))
        if mailbox_email:
            return mailbox_email
        owner = self.db.get(User, owner_id)
        if owner and owner.email:
            return owner.email.strip()
        raise ConversationReplyError("Could not resolve mailbox email for this conversation")

    def _pick_anchor_message(
        self,
        owner_id: int,
        conversation_id: int,
        mailbox_email: str,
    ) -> ConversationMessage:
        messages = ConversationService.list_messages(
            self.db,
            owner_id=owner_id,
            conversation_id=conversation_id,
            limit=500,
        )
        mailbox_normalized = _normalize_email(mailbox_email)
        for message in reversed(messages):
            email_meta = (message.message_metadata or {}).get("email") or {}
            from_email = _normalize_email(email_meta.get("from_email"))
            if not email_meta:
                continue
            if message.direction != "incoming":
                continue
            if not from_email or from_email == mailbox_normalized:
                continue
            return message
        raise ConversationReplyError("Conversation has no inbound email message to reply to yet")

    def _resolve_recipients(
        self,
        *,
        email_meta: dict[str, Any],
        mailbox_email: str,
        reply_all: bool,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        mailbox_normalized = _normalize_email(mailbox_email)
        primary = list(email_meta.get("reply_to_emails") or [])
        if not primary:
            primary = [email_meta.get("from_email") or ""]
        to_emails = _dedupe_emails(primary, exclude={mailbox_normalized})
        if not to_emails:
            raise ConversationReplyError("Conversation is missing a reply target")

        cc_emails: tuple[str, ...] = ()
        if reply_all:
            cc_candidates = list(email_meta.get("to_emails") or []) + list(email_meta.get("cc_emails") or [])
            exclude = {mailbox_normalized, *(_normalize_email(email) for email in to_emails)}
            cc_emails = _dedupe_emails(cc_candidates, exclude=exclude)
        return to_emails, cc_emails

    def _build_raw_reply(
        self,
        *,
        subject: str,
        body_text: str,
        from_email: str,
        to_emails: tuple[str, ...],
        cc_emails: tuple[str, ...],
        in_reply_to: str,
        references: str | None,
    ) -> tuple[bytes, str]:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = from_email
        msg["To"] = ", ".join(to_emails)
        if cc_emails:
            msg["Cc"] = ", ".join(cc_emails)
        msg["In-Reply-To"] = in_reply_to
        if references:
            msg["References"] = references

        domain = _normalize_email(from_email).split("@", 1)[1] if "@" in from_email else "localhost"
        message_id = make_msgid(domain=domain)
        msg["Message-ID"] = message_id
        msg.set_content(body_text)
        return msg.as_bytes(), message_id
