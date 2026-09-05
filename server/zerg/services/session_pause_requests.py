"""Durable structured-question pause requests for runtime sessions."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from typing import Any
from uuid import NAMESPACE_URL
from uuid import UUID
from uuid import uuid5

from sqlalchemy.orm import Session

from zerg.models.agents import AgentSession
from zerg.models.agents import SessionPauseRequest
from zerg.models.live_store import LiveInteractionRequest
from zerg.models.live_store import LiveRuntimeState
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.utils.time import normalize_utc

PAUSE_KIND_STRUCTURED_QUESTION = "structured_question"
PAUSE_KIND_PERMISSION_PROMPT = "permission_prompt"
PAUSE_KIND_PLAN_APPROVAL = "plan_approval"
PENDING_STATUS = "pending"
TERMINAL_STATUSES = {"resolved", "rejected", "failed", "expired"}
ACTIVE_STATUSES = {PENDING_STATUS}
QUESTION_PAYLOAD_KEYS = ("questions", "question", "prompt", "input", "schema", "plan", "steps", "proposal")

# How an answered pause request is delivered back to the provider. PULL = the
# provider polls Longhouse for the resolved row (Claude PreToolUse hook); PUSH =
# Longhouse pushes the decision to the running provider over managed control
# (Codex app-server, OpenCode bridge). Carried in provider_ref.reply_transport.
REPLY_TRANSPORT_CLAUDE_PULL = "claude_pretooluse_pull"
REPLY_TRANSPORT_CURSOR_POLL = "cursor_permission_poll"
REPLY_TRANSPORT_MANAGED_PUSH = "managed_push"
PULL_REPLY_TRANSPORTS = {REPLY_TRANSPORT_CLAUDE_PULL, REPLY_TRANSPORT_CURSOR_POLL}

# A stale lease is not proof of exit. Permission waits and managed-push provider
# questions belong to an execution; durable Longhouse questions do not.
EXECUTION_TERMINAL_STATES = {"session_ended", "process_gone", "run_completed", "run_failed", "run_cancelled", "user_closed"}


def clear_live_interaction_pointer(state: LiveRuntimeState, *, request_key: str, occurred_at: datetime) -> bool:
    """Clear only the pointer owned by this request, retaining the replay watermark."""
    if state.pending_interaction_id != request_key:
        return False
    state.pending_interaction_id = None
    state.pending_interaction_kind = None
    state.pending_interaction_opened_at = None
    state.pending_interaction_projection_json = None
    state.pending_interaction_can_respond = 0
    state.pending_interaction_updated_at = max(normalize_utc(state.pending_interaction_updated_at) or occurred_at, occurred_at)
    state.runtime_version = int(state.runtime_version or 0) + 1
    state.updated_at = max(normalize_utc(state.updated_at) or occurred_at, occurred_at)
    return True


def expire_live_interaction(db: Session, row: LiveInteractionRequest, *, occurred_at: datetime, reason: str) -> None:
    """Revoke a held request without discarding its history or a newer pointer."""
    row.status = "expired"
    row.can_respond = 0
    row.resolved_at = occurred_at
    row.last_seen_at = max(normalize_utc(row.last_seen_at) or occurred_at, occurred_at)
    row.updated_at = occurred_at
    row.response_payload_json = {"permissionDecision": "deny", "permissionDecisionReason": reason}
    row.response_text = reason
    db.add(row)
    state = db.get(LiveRuntimeState, row.runtime_key)
    if state is not None:
        clear_live_interaction_pointer(state, request_key=row.request_key, occurred_at=occurred_at)


def _execution_owned_interaction(row: LiveInteractionRequest) -> bool:
    return row.kind == PAUSE_KIND_PERMISSION_PROMPT or row.reply_transport == REPLY_TRANSPORT_MANAGED_PUSH


def live_interaction_terminal_reason(db: Session, row: LiveInteractionRequest) -> str | None:
    """Return positive owner-exit evidence, never a guess from stale activity."""
    if not _execution_owned_interaction(row):
        return None
    if row.run_id:
        run = db.get(LiveSessionRun, row.run_id)
        if run is not None and run.ended_at is not None:
            thread = db.get(LiveSessionThread, run.thread_id)
            if thread is not None and thread.session_id == row.session_id:
                return "Owning execution ended"
    state = db.get(LiveRuntimeState, row.runtime_key)
    if state is None or str(state.session_id) != row.session_id:
        return None
    last_seen_at = normalize_utc(row.last_seen_at)
    resumed_at = normalize_utc(state.execution_started_at)
    progress_at = normalize_utc(state.last_progress_at)
    if (
        row.reply_transport == REPLY_TRANSPORT_MANAGED_PUSH
        and state.phase == "idle"
        and resumed_at is not None
        and last_seen_at is not None
        and resumed_at > last_seen_at
        and progress_at is not None
        and progress_at >= resumed_at
        and (row.run_id is None or state.run_id is None or row.run_id == str(state.run_id))
    ):
        return "Provider continued after the interaction"
    if state.terminal_state not in EXECUTION_TERMINAL_STATES:
        return None
    if row.run_id and state.run_id is not None:
        return "Owning execution ended" if row.run_id == str(state.run_id) else None
    terminal_at = normalize_utc(state.terminal_at)
    if terminal_at is not None and last_seen_at is not None and last_seen_at <= terminal_at:
        return "Owning execution ended"
    return None


def materialize_live_interaction(db: Session, state: LiveRuntimeState, *, persist: bool = True) -> LiveInteractionRequest | None:
    """Preserve a pre-interaction-table runtime request before repairing it."""
    if not state.pending_interaction_id or state.session_id is None:
        return None
    row = db.query(LiveInteractionRequest).filter_by(request_key=state.pending_interaction_id).one_or_none()
    if row is not None:
        return row
    projection = state.pending_interaction_projection_json
    if not isinstance(projection, dict):
        return None
    occurred_at = normalize_utc(state.pending_interaction_opened_at)
    if occurred_at is None:
        return None
    provider = str(state.provider)
    kind = state.pending_interaction_kind or projection.get("kind") or PAUSE_KIND_STRUCTURED_QUESTION
    source, transport = (
        {
            "claude": ("claude_permission_gate", REPLY_TRANSPORT_CLAUDE_PULL),
            "cursor": ("cursor_permission_gate", REPLY_TRANSPORT_CURSOR_POLL),
            "opencode": ("opencode_bridge", REPLY_TRANSPORT_MANAGED_PUSH),
        }.get(provider, (None, None))
        if kind == PAUSE_KIND_PERMISSION_PROMPT
        else (None, None)
    )
    prefix = f"{provider}:{state.runtime_key}:"
    row = LiveInteractionRequest(
        id=str(projection.get("id") or uuid5(NAMESPACE_URL, f"longhouse-pause:{state.pending_interaction_id}")),
        session_id=str(state.session_id),
        runtime_key=state.runtime_key,
        run_id=projection.get("run_id"),
        provider=provider,
        request_key=state.pending_interaction_id,
        provider_request_id=state.pending_interaction_id.removeprefix(prefix) if state.pending_interaction_id.startswith(prefix) else None,
        source=source,
        reply_transport=transport,
        kind=kind,
        status="pending",
        can_respond=int(bool(state.pending_interaction_can_respond)),
        request_payload_json={},
        projection_json=projection,
        occurred_at=occurred_at,
        last_seen_at=normalize_utc(state.pending_interaction_updated_at) or occurred_at,
        created_at=occurred_at,
        updated_at=normalize_utc(state.pending_interaction_updated_at) or occurred_at,
        expires_at=_datetime_payload(projection.get("expires_at")),
    )
    if persist:
        db.add(row)
        db.flush()
    return row


def expire_live_interactions_for_terminal(db: Session, event: Any) -> int:
    """Apply each terminal's own scope, including delayed terminals of old runs."""
    if (event.payload or {}).get("terminal_state") not in EXECUTION_TERMINAL_STATES:
        return 0
    state = db.get(LiveRuntimeState, event.runtime_key)
    if state is not None:
        materialize_live_interaction(db, state)
    occurred_at = normalize_utc(event.occurred_at) or datetime.now(timezone.utc)
    rows = db.query(LiveInteractionRequest).filter_by(runtime_key=event.runtime_key, status="pending").all()
    expired = 0
    for row in rows:
        if not _execution_owned_interaction(row):
            continue
        if event.session_id is not None and str(event.session_id) != row.session_id:
            continue
        if row.run_id and event.run_id is not None:
            matches = row.run_id == str(event.run_id)
        else:
            # Legacy requests without run identity need both temporal ownership
            # and no evidence that the terminal belongs to a different run.
            matches = (
                (state is None or event.run_id is None or state.run_id is None or event.run_id == state.run_id)
                and (row.run_id is None or state is None or state.run_id is None or row.run_id == str(state.run_id))
                and normalize_utc(row.last_seen_at) <= occurred_at
            )
        if matches:
            expire_live_interaction(db, row, occurred_at=occurred_at, reason="Owning execution ended")
            expired += 1
    return expired


def apply_live_interaction_event(db: Session, event: Any, state: LiveRuntimeState) -> bool:
    """Reduce request history and its runtime pointer together, in event order."""
    payload = event.payload if isinstance(event.payload, dict) else {}
    request_key = pause_runtime_request_key(event)
    occurred_at = normalize_utc(event.occurred_at) or datetime.now(timezone.utc)
    row = db.query(LiveInteractionRequest).filter_by(request_key=request_key).one_or_none()
    if event.kind == "pause_resolution":
        if row is None:
            row = materialize_live_interaction(db, state)
            if row is not None and row.request_key != request_key:
                row = None
        if row is None or row.status != "pending" or normalize_utc(row.last_seen_at) > occurred_at:
            return False
        row.status = _terminal_status(str(payload.get("status") or "resolved"))
        row.can_respond = 0
        row.response_payload_json = _json_obj(payload.get("response_payload") or payload.get("response_payload_json"))
        row.response_text = _clean_str(payload.get("response_text") or payload.get("message"))
        row.resolved_at = occurred_at
        row.last_seen_at = occurred_at
        row.updated_at = occurred_at
        clear_live_interaction_pointer(state, request_key=request_key, occurred_at=occurred_at)
        return True
    if event.session_id is None:
        return False
    # A request identity denotes one provider wait, never a reusable slot.
    if row is not None and (row.status != "pending" or normalize_utc(row.last_seen_at) >= occurred_at):
        return False
    projection = build_pause_runtime_projection(event)
    provider_ref = _mapping(payload.get("provider_ref") or payload.get("provider_ref_json"))
    if row is None:
        row = LiveInteractionRequest(id=projection["id"], request_key=request_key, created_at=occurred_at)
    row.session_id = str(event.session_id)
    row.runtime_key = event.runtime_key
    run_id = event.run_id
    if run_id is None and state.run_id is not None:
        run = db.get(LiveSessionRun, str(state.run_id))
        # Do not attach a delayed request to a newer execution, or a new
        # unbound request to an already-ended execution.
        terminal_at = normalize_utc(state.terminal_at)
        if (run is None or normalize_utc(run.started_at) <= occurred_at) and (terminal_at is None or occurred_at <= terminal_at):
            run_id = state.run_id
    row.run_id = str(run_id) if run_id is not None else None
    row.provider = event.provider
    row.provider_request_id = _clean_str(payload.get("provider_request_id") or payload.get("request_id"))
    row.source = _clean_str(provider_ref.get("source")) or event.source
    row.reply_transport = _clean_str(provider_ref.get("reply_transport"))
    row.kind = str(payload.get("kind") or PAUSE_KIND_STRUCTURED_QUESTION)
    row.status = "pending"
    row.can_respond = int(bool(payload.get("can_respond")))
    row.request_payload_json = _request_payload(payload)
    row.projection_json = {**projection, "run_id": row.run_id}
    row.occurred_at = occurred_at
    row.last_seen_at = occurred_at
    row.updated_at = occurred_at
    row.expires_at = _datetime_payload(projection.get("expires_at"))
    db.add(row)
    reason = live_interaction_terminal_reason(db, row)
    watermark = normalize_utc(state.pending_interaction_updated_at)
    if reason is None and watermark is not None and occurred_at <= watermark and _execution_owned_interaction(row):
        reason = "Superseded by a newer provider interaction"
    if reason is not None:
        expire_live_interaction(db, row, occurred_at=occurred_at, reason=reason)
        return True
    if bool(payload.get("single_active", True)):
        for previous in (
            db.query(LiveInteractionRequest)
            .filter(
                LiveInteractionRequest.runtime_key == event.runtime_key,
                LiveInteractionRequest.request_key != request_key,
                LiveInteractionRequest.status == "pending",
                LiveInteractionRequest.last_seen_at <= occurred_at,
            )
            .all()
        ):
            # A permission cannot silently consume a durable user question.
            if previous.kind == row.kind and _execution_owned_interaction(previous) == _execution_owned_interaction(row):
                expire_live_interaction(db, previous, occurred_at=occurred_at, reason="Superseded by a newer provider interaction")
    if watermark is not None and occurred_at <= watermark:
        return True
    state.pending_interaction_id = request_key
    state.pending_interaction_kind = row.kind
    state.pending_interaction_opened_at = occurred_at
    state.pending_interaction_updated_at = occurred_at
    state.pending_interaction_can_respond = row.can_respond
    state.pending_interaction_projection_json = row.projection_json
    state.runtime_version = int(state.runtime_version or 0) + 1
    state.updated_at = max(normalize_utc(state.updated_at) or occurred_at, occurred_at)
    return True


def pending_interaction_from_live_runtime(runtime: LiveRuntimeState | None) -> dict[str, Any] | None:
    """Project the answerable interaction carried by the hot runtime row."""
    if runtime is None or not runtime.pending_interaction_id:
        return None
    projection = runtime.pending_interaction_projection_json
    if isinstance(projection, dict):
        result = {
            **projection,
            "can_respond": bool(runtime.pending_interaction_can_respond),
        }
        for key in ("occurred_at", "created_at", "expires_at"):
            value = result.get(key)
            if isinstance(value, str):
                try:
                    result[key] = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    result[key] = None
        return result
    return {
        "id": runtime.pending_interaction_id,
        "status": PENDING_STATUS,
        "kind": runtime.pending_interaction_kind or PAUSE_KIND_STRUCTURED_QUESTION,
        "occurred_at": runtime.pending_interaction_opened_at,
        "can_respond": bool(runtime.pending_interaction_can_respond),
    }


def load_hot_session_projection_map(session_ids: list[UUID]) -> dict[UUID, tuple[dict[str, Any] | None, str]]:
    """Load hot interaction and transcript convergence truth for detail reads.

    The archive still owns transcript bodies, but the bounded catalog owns the
    current interaction and convergence facts.  A present hot runtime row with
    no interaction intentionally clears a stale archive pause projection.
    """
    if not session_ids:
        return {}
    from zerg.services.catalog_facts import session_facts_map
    from zerg.services.catalog_read_gateway import CatalogReadError

    try:
        facts_by_session = session_facts_map([str(value) for value in session_ids])
    except CatalogReadError:
        return {}
    result: dict[UUID, tuple[dict[str, Any] | None, str]] = {}
    for facts in facts_by_session.values():
        if not isinstance(facts, dict):
            continue
        catalog = facts.get("catalog")
        if not isinstance(catalog, dict) or not catalog.get("session_id"):
            continue
        session_id = UUID(str(catalog["session_id"]))
        runtime = facts.get("runtime")
        projection = runtime.get("pending_interaction_projection_json") if isinstance(runtime, dict) else None
        if isinstance(projection, dict):
            projection = {**projection, "can_respond": bool(runtime.get("pending_interaction_can_respond"))}
            for key in ("occurred_at", "created_at", "expires_at"):
                if isinstance(projection.get(key), str):
                    try:
                        projection[key] = datetime.fromisoformat(projection[key].replace("Z", "+00:00"))
                    except ValueError:
                        projection[key] = None
        else:
            projection = None
        card = facts.get("card")
        archive_state = str(card.get("archive_state") or "pending") if isinstance(card, dict) else "pending"
        result[session_id] = (projection, archive_state)
    return result


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
    resolved_at = normalize_utc(occurred_at) or datetime.now(timezone.utc)
    if normalize_utc(row.expires_at) is not None and normalize_utc(row.expires_at) <= resolved_at and status != "expired":
        row.status = "expired"
        row.resolved_at = resolved_at
        row.response_payload_json = {
            "permissionDecision": "deny",
            "permissionDecisionReason": "Approval deadline expired",
        }
        row.response_text = "Approval deadline expired"
        db.add(row)
        db.flush()
        return row
    row.status = _terminal_status(status)
    row.resolved_at = resolved_at
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
        expires_at = normalize_utc(row.expires_at)
        if expires_at is not None and expires_at <= datetime.now(timezone.utc):
            continue
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
    now = datetime.now(timezone.utc)
    return [
        row
        for row in rows
        if is_user_facing_pause_request(row)
        and not (row.status == PENDING_STATUS and normalize_utc(row.expires_at) is not None and normalize_utc(row.expires_at) <= now)
    ]


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


def pause_runtime_request_key(event: Any) -> str:
    payload = event.payload if isinstance(event.payload, dict) else {}
    provider = _clean_str(event.provider) or "unknown"
    runtime_key = _clean_str(event.runtime_key) or ""
    provider_request_id = _clean_str(payload.get("provider_request_id") or payload.get("request_id"))
    return _clean_str(payload.get("request_key")) or make_pause_request_key(
        provider=provider,
        runtime_key=runtime_key,
        provider_request_id=provider_request_id,
        fallback=_clean_str(getattr(event, "dedupe_key", None)),
    )


def build_pause_runtime_projection(event: Any) -> dict[str, Any]:
    """Build the complete client projection for the bounded live lane."""
    payload = event.payload if isinstance(event.payload, dict) else {}
    occurred_at = normalize_utc(event.occurred_at) or datetime.now(timezone.utc)
    request_payload = _request_payload(payload)
    request_key = pause_runtime_request_key(event)
    return {
        "id": str(uuid5(NAMESPACE_URL, f"longhouse-pause:{request_key}")),
        "request_key": request_key,
        "session_id": str(event.session_id),
        "runtime_key": _clean_str(event.runtime_key) or "",
        "run_id": str(event.run_id) if getattr(event, "run_id", None) is not None else None,
        "kind": _clean_str(payload.get("kind")) or PAUSE_KIND_STRUCTURED_QUESTION,
        "status": PENDING_STATUS,
        "provider": _clean_str(event.provider) or "unknown",
        "can_respond": _bool(payload.get("can_respond")),
        "title": _clean_str(payload.get("title")),
        "summary": _clean_str(payload.get("summary")),
        "tool_name": _clean_str(payload.get("tool_name") or event.tool_name),
        "questions": _normalize_questions(request_payload),
        "occurred_at": occurred_at.isoformat(),
        "last_seen_at": occurred_at.isoformat(),
        "resolved_at": None,
        "expires_at": (expires_at.isoformat() if (expires_at := _datetime_payload(payload.get("expires_at"))) is not None else None),
    }


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
    if _clean_str(payload.get("kind")) == PAUSE_KIND_PLAN_APPROVAL:
        plan = payload.get("plan") or payload.get("proposal") or payload.get("summary") or payload.get("message")
        question = _clean_str(payload.get("question") or payload.get("prompt") or plan) or "Approve this plan?"
        return {
            "questions": [
                {
                    "id": "approval",
                    "question": question,
                    "options": [
                        {"label": "Approve", "value": "approve"},
                        {"label": "Reject", "value": "reject"},
                    ],
                }
            ],
            "plan": plan,
            "steps": payload.get("steps"),
        }
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
    if source in {"claude_permission_gate", "cursor_permission_gate"} and bool(row.can_respond):
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
