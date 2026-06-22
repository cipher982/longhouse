"""Durable structured-question pause requests for runtime sessions."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.models.agents import SessionPauseRequest
from zerg.utils.time import normalize_utc

PAUSE_KIND_STRUCTURED_QUESTION = "structured_question"
PAUSE_KIND_PERMISSION_PROMPT = "permission_prompt"
PENDING_STATUS = "pending"
TERMINAL_STATUSES = {"resolved", "rejected", "failed", "expired"}
ACTIVE_STATUSES = {PENDING_STATUS}
QUESTION_PAYLOAD_KEYS = ("questions", "question", "prompt", "input", "schema")

# How an answered pause request is delivered back to the provider. PULL = the
# provider polls Longhouse for the resolved row (Claude PreToolUse hook); PUSH =
# Longhouse pushes the decision to the running provider over managed control
# (Codex app-server, OpenCode bridge). Carried in provider_ref.reply_transport.
REPLY_TRANSPORT_CLAUDE_PULL = "claude_pretooluse_pull"
REPLY_TRANSPORT_MANAGED_PUSH = "managed_push"
PULL_REPLY_TRANSPORTS = {REPLY_TRANSPORT_CLAUDE_PULL}


def reply_transport_for_row(row: "SessionPauseRequest") -> str | None:
    """The provider_ref.reply_transport for a pause request, if set."""
    ref = row.provider_ref_json if isinstance(row.provider_ref_json, dict) else {}
    value = _clean_str(ref.get("reply_transport"))
    return value


def _provider_ref_source(row: "SessionPauseRequest") -> str | None:
    ref = row.provider_ref_json if isinstance(row.provider_ref_json, dict) else {}
    return _clean_str(ref.get("source"))


def is_pull_reply_transport(row: "SessionPauseRequest") -> bool:
    """True when the answer is delivered by the provider polling (resolve in place).

    Explicit pull transport always wins. For backward compatibility, a
    Claude permission-gate row (kind=permission_prompt, source=claude_permission_gate)
    written before reply_transport existed defaults to PULL — it must never be
    pushed over managed control (there is no live process to push to). Other rows
    (e.g. structured_question) keep their historical PUSH default.
    """
    transport = reply_transport_for_row(row)
    if transport in PULL_REPLY_TRANSPORTS:
        return True
    if transport is None and _clean_str(getattr(row, "kind", None)) == PAUSE_KIND_PERMISSION_PROMPT:
        return _provider_ref_source(row) == "claude_permission_gate"
    return False


def make_pause_request_key(
    *,
    provider: str,
    runtime_key: str,
    provider_request_id: str | None = None,
    fallback: str | None = None,
) -> str:
    provider_key = _clean_str(provider) or "unknown"
    runtime = _clean_str(runtime_key) or "unknown"
    suffix = _clean_str(provider_request_id) or _clean_str(fallback) or "unknown"
    return f"{provider_key}:{runtime}:{suffix}"


def upsert_pause_request(
    db: Session,
    *,
    session_id: UUID,
    runtime_key: str,
    provider: str,
    request_key: str,
    occurred_at: datetime,
    provider_request_id: str | None = None,
    provider_ref: Mapping[str, Any] | None = None,
    kind: str = PAUSE_KIND_STRUCTURED_QUESTION,
    tool_name: str | None = None,
    title: str | None = None,
    summary: str | None = None,
    request_payload: Mapping[str, Any] | None = None,
    can_respond: bool = False,
    expires_at: datetime | None = None,
) -> tuple[SessionPauseRequest, bool]:
    """Create or refresh a pending structured-question pause request."""

    observed_at = normalize_utc(occurred_at) or datetime.now(timezone.utc)
    resolved_expires_at = normalize_utc(expires_at)
    row = db.query(SessionPauseRequest).filter(SessionPauseRequest.request_key == request_key).first()
    changed = False
    if row is None:
        row = SessionPauseRequest(
            session_id=session_id,
            runtime_key=runtime_key,
            provider=_clean_str(provider) or "unknown",
            request_key=request_key,
            provider_request_id=_clean_str(provider_request_id),
            provider_ref_json=_json_obj(provider_ref),
            kind=_clean_str(kind) or PAUSE_KIND_STRUCTURED_QUESTION,
            status=PENDING_STATUS,
            tool_name=_clean_str(tool_name),
            title=_clean_str(title),
            summary=_clean_str(summary),
            request_payload_json=_json_obj(request_payload),
            can_respond=bool(can_respond),
            occurred_at=observed_at,
            last_seen_at=observed_at,
            expires_at=resolved_expires_at,
        )
        db.add(row)
        db.flush()
        return row, True

    updates: dict[str, Any] = {
        "session_id": session_id,
        "runtime_key": runtime_key,
        "provider": _clean_str(provider) or "unknown",
        "provider_request_id": _clean_str(provider_request_id),
        "provider_ref_json": _json_obj(provider_ref),
        "kind": _clean_str(kind) or PAUSE_KIND_STRUCTURED_QUESTION,
        "status": PENDING_STATUS,
        "tool_name": _clean_str(tool_name),
        "title": _clean_str(title),
        "summary": _clean_str(summary),
        "request_payload_json": _json_obj(request_payload),
        "can_respond": bool(can_respond),
        "last_seen_at": observed_at,
        "resolved_at": None,
        "expires_at": resolved_expires_at,
    }
    if normalize_utc(row.occurred_at) is None or observed_at < normalize_utc(row.occurred_at):
        updates["occurred_at"] = observed_at
    for field, value in updates.items():
        if getattr(row, field) != value:
            setattr(row, field, value)
            changed = True
    if changed:
        db.add(row)
        db.flush()
    return row, changed


def resolve_pause_request(
    db: Session,
    *,
    request_key: str | None = None,
    pause_request_id: UUID | None = None,
    runtime_key: str | None = None,
    provider_request_id: str | None = None,
    status: str = "resolved",
    occurred_at: datetime | None = None,
    response_payload: Mapping[str, Any] | None = None,
    response_text: str | None = None,
) -> SessionPauseRequest | None:
    """Resolve one pending pause request by its strongest available key."""

    query = db.query(SessionPauseRequest)
    if pause_request_id is not None:
        query = query.filter(SessionPauseRequest.id == pause_request_id)
    elif request_key:
        query = query.filter(SessionPauseRequest.request_key == request_key)
    elif runtime_key and provider_request_id:
        query = query.filter(
            SessionPauseRequest.runtime_key == runtime_key,
            SessionPauseRequest.provider_request_id == provider_request_id,
        )
    elif runtime_key:
        query = query.filter(
            SessionPauseRequest.runtime_key == runtime_key,
            SessionPauseRequest.status == PENDING_STATUS,
        ).order_by(SessionPauseRequest.occurred_at.desc(), SessionPauseRequest.created_at.desc())
    else:
        return None

    row = query.first()
    if row is None or row.status != PENDING_STATUS:
        return row
    row.status = _terminal_status(status)
    row.resolved_at = normalize_utc(occurred_at) or datetime.now(timezone.utc)
    row.response_payload_json = _json_obj(response_payload)
    row.response_text = _clean_str(response_text)
    db.add(row)
    db.flush()
    return row


def resolve_pending_pause_requests_for_runtime(
    db: Session,
    *,
    runtime_key: str,
    status: str = "resolved",
    occurred_at: datetime | None = None,
    response_text: str | None = None,
) -> int:
    return _finish_pending(
        db,
        filters=[SessionPauseRequest.runtime_key == runtime_key],
        status=status,
        occurred_at=occurred_at,
        response_text=response_text,
    )


def expire_pending_pause_requests_for_session(
    db: Session,
    *,
    session_id: UUID,
    occurred_at: datetime | None = None,
    response_text: str | None = None,
) -> int:
    return _finish_pending(
        db,
        filters=[SessionPauseRequest.session_id == session_id],
        status="expired",
        occurred_at=occurred_at,
        response_text=response_text,
    )


def expire_pending_pause_requests_for_runtime(
    db: Session,
    *,
    runtime_key: str,
    occurred_at: datetime | None = None,
    response_text: str | None = None,
) -> int:
    return _finish_pending(
        db,
        filters=[SessionPauseRequest.runtime_key == runtime_key],
        status="expired",
        occurred_at=occurred_at,
        response_text=response_text,
    )


def load_active_pause_request_map(db: Session, session_ids: list[UUID]) -> dict[UUID, SessionPauseRequest]:
    if not session_ids:
        return {}
    rows = (
        db.query(SessionPauseRequest)
        .filter(SessionPauseRequest.session_id.in_(session_ids))
        .filter(SessionPauseRequest.status == PENDING_STATUS)
        .order_by(
            SessionPauseRequest.session_id.asc(),
            SessionPauseRequest.last_seen_at.desc(),
            SessionPauseRequest.occurred_at.desc(),
            SessionPauseRequest.created_at.desc(),
        )
        .all()
    )
    by_session: dict[UUID, SessionPauseRequest] = {}
    for row in rows:
        if not is_user_facing_pause_request(row):
            continue
        by_session.setdefault(row.session_id, row)
    return by_session


def load_active_pause_request_for_session(db: Session, session_id: UUID) -> SessionPauseRequest | None:
    return load_active_pause_request_map(db, [session_id]).get(session_id)


def list_pause_requests_for_session(
    db: Session,
    session_id: UUID,
    *,
    status: str | None = PENDING_STATUS,
) -> list[SessionPauseRequest]:
    query = db.query(SessionPauseRequest).filter(SessionPauseRequest.session_id == session_id)
    cleaned_status = _clean_str(status)
    if cleaned_status:
        query = query.filter(SessionPauseRequest.status == cleaned_status)
    rows = query.order_by(
        SessionPauseRequest.status.asc(),
        SessionPauseRequest.last_seen_at.desc(),
        SessionPauseRequest.occurred_at.desc(),
        SessionPauseRequest.created_at.desc(),
    ).all()
    return [row for row in rows if is_user_facing_pause_request(row)]


def get_pause_request_for_session(
    db: Session,
    *,
    session_id: UUID,
    pause_request_id: UUID,
) -> SessionPauseRequest | None:
    return (
        db.query(SessionPauseRequest)
        .filter(
            SessionPauseRequest.session_id == session_id,
            SessionPauseRequest.id == pause_request_id,
        )
        .first()
    )


def apply_pause_runtime_event(db: Session, event: Any) -> bool:
    """Apply a pause_request/pause_resolution runtime event without writing phase."""

    payload = event.payload if isinstance(event.payload, dict) else {}
    occurred_at = normalize_utc(event.occurred_at) or datetime.now(timezone.utc)
    runtime_key = _clean_str(event.runtime_key) or ""
    provider = _clean_str(event.provider) or "unknown"
    provider_request_id = _clean_str(payload.get("provider_request_id") or payload.get("request_id"))
    request_key = _clean_str(payload.get("request_key")) or make_pause_request_key(
        provider=provider,
        runtime_key=runtime_key,
        provider_request_id=provider_request_id,
        fallback=_clean_str(getattr(event, "dedupe_key", None)),
    )

    if event.kind == "pause_request":
        if event.session_id is None:
            return False
        if db.query(AgentSession.id).filter(AgentSession.id == event.session_id).first() is None:
            return False
        if _bool(payload.get("single_active", True)):
            _supersede_runtime_requests_if_needed(db, runtime_key=runtime_key, request_key=request_key, occurred_at=occurred_at)
        _row, changed = upsert_pause_request(
            db,
            session_id=event.session_id,
            runtime_key=runtime_key,
            provider=provider,
            request_key=request_key,
            provider_request_id=provider_request_id,
            provider_ref=_mapping(payload.get("provider_ref") or payload.get("provider_ref_json")),
            kind=_clean_str(payload.get("kind")) or PAUSE_KIND_STRUCTURED_QUESTION,
            tool_name=_clean_str(payload.get("tool_name") or event.tool_name),
            title=_clean_str(payload.get("title")),
            summary=_clean_str(payload.get("summary")),
            request_payload=_request_payload(payload),
            can_respond=_bool(payload.get("can_respond")),
            occurred_at=occurred_at,
            expires_at=_datetime_payload(payload.get("expires_at")),
        )
        return changed

    if event.kind == "pause_resolution":
        row = resolve_pause_request(
            db,
            request_key=request_key,
            runtime_key=runtime_key,
            provider_request_id=provider_request_id,
            status=_clean_str(payload.get("status")) or "resolved",
            occurred_at=occurred_at,
            response_payload=_mapping(payload.get("response_payload") or payload.get("response_payload_json")),
            response_text=_clean_str(payload.get("response_text") or payload.get("message")),
        )
        return bool(row is not None and row.status != PENDING_STATUS)

    return False


def serialize_pause_request_projection(
    row: SessionPauseRequest | None,
    *,
    can_respond: bool | None = None,
) -> dict[str, Any] | None:
    if row is None:
        return None
    request_payload = row.request_payload_json if isinstance(row.request_payload_json, dict) else {}
    effective_can_respond = bool(row.can_respond if can_respond is None else can_respond)
    return {
        "id": str(row.id),
        "session_id": str(row.session_id),
        "runtime_key": row.runtime_key,
        "kind": row.kind,
        "status": row.status,
        "provider": row.provider,
        "can_respond": effective_can_respond,
        "title": row.title,
        "summary": row.summary,
        "tool_name": row.tool_name,
        "questions": _normalize_questions(request_payload),
        "occurred_at": normalize_utc(row.occurred_at),
        "last_seen_at": normalize_utc(row.last_seen_at),
        "resolved_at": normalize_utc(row.resolved_at),
        "expires_at": normalize_utc(row.expires_at),
    }


def _finish_pending(
    db: Session,
    *,
    filters: list[Any],
    status: str,
    occurred_at: datetime | None,
    response_text: str | None = None,
) -> int:
    resolved_at = normalize_utc(occurred_at) or datetime.now(timezone.utc)
    rows = db.query(SessionPauseRequest).filter(SessionPauseRequest.status == PENDING_STATUS, *filters).all()
    changed = 0
    for row in rows:
        row.status = _terminal_status(status)
        row.resolved_at = resolved_at
        if response_text:
            row.response_text = response_text
        db.add(row)
        changed += 1
    if changed:
        db.flush()
    return changed


def _supersede_runtime_requests_if_needed(db: Session, *, runtime_key: str, request_key: str, occurred_at: datetime) -> None:
    rows = (
        db.query(SessionPauseRequest)
        .filter(SessionPauseRequest.runtime_key == runtime_key)
        .filter(SessionPauseRequest.status == PENDING_STATUS)
        .filter(SessionPauseRequest.request_key != request_key)
        .all()
    )
    for row in rows:
        row.status = "resolved"
        row.resolved_at = occurred_at
        row.response_text = "Superseded by a newer provider question."
        db.add(row)
    if rows:
        db.flush()


def _request_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    explicit = _mapping(payload.get("request_payload") or payload.get("request_payload_json"))
    if explicit:
        return explicit
    out: dict[str, Any] = {}
    for key in QUESTION_PAYLOAD_KEYS:
        if key in payload:
            out[key] = payload[key]
    return out


def is_user_facing_pause_request(row: SessionPauseRequest) -> bool:
    """Hide legacy hook-only placeholders from user-facing question surfaces."""

    ref = _mapping(row.provider_ref_json)
    source = _clean_str(ref.get("source"))
    # Answerable Claude permission prompts (PreToolUse gate) are real, user-facing
    # decisions even though they originate from a hook. Always surface them.
    if source == "claude_permission_gate" and bool(row.can_respond):
        return True
    if source == "claude_hook":
        return False
    request_key = _clean_str(row.request_key) or ""
    provider_request_id = _clean_str(row.provider_request_id) or ""
    return not (request_key.startswith("claude-hook:") or provider_request_id == "claude-hook-ask-user-question")


def _normalize_questions(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_questions = payload.get("questions")
    if raw_questions is None and any(key in payload for key in ("question", "prompt", "options")):
        raw_questions = [payload]
    if not isinstance(raw_questions, list):
        return []

    questions: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_questions):
        if not isinstance(item, Mapping):
            continue
        question_id = _clean_str(item.get("id") or item.get("name") or item.get("key")) or f"question_{idx + 1}"
        options = []
        raw_options = item.get("options")
        if not isinstance(raw_options, list):
            raw_options = item.get("choices")
        if isinstance(raw_options, list):
            for option in raw_options:
                if isinstance(option, Mapping):
                    label = _clean_str(option.get("label") or option.get("value") or option.get("text"))
                    description = _clean_str(option.get("description") or option.get("detail"))
                    value = _clean_str(option.get("value") or label)
                else:
                    label = _clean_str(option)
                    description = None
                    value = label
                if label:
                    options.append({"label": label, "description": description, "value": value})
        questions.append(
            {
                "id": question_id,
                "header": _clean_str(item.get("header") or item.get("title")),
                "question": _clean_str(item.get("question") or item.get("prompt") or item.get("label")) or "Answer required",
                "multi_select": _bool(item.get("multi_select") if "multi_select" in item else item.get("multiSelect")),
                "options": options,
            }
        )
    return questions


def _terminal_status(value: str) -> str:
    cleaned = _clean_str(value) or "resolved"
    if cleaned == PENDING_STATUS:
        return "resolved"
    return cleaned if cleaned in TERMINAL_STATUSES else "resolved"


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _json_obj(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not value:
        return None
    return dict(value)


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _datetime_payload(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return normalize_utc(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return normalize_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None
