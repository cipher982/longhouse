from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from zerg.models.agents import SessionLivePreview
from zerg.models.live_store import LiveSessionLivePreview
from zerg.services.provisional_events import EVENT_ORIGIN_LIVE_PROVISIONAL
from zerg.services.provisional_events import TranscriptPreview
from zerg.services.provisional_events import build_provisional_cursor
from zerg.services.provisional_events import build_provisional_key
from zerg.utils.time import normalize_utc

LIVE_PREVIEW_SOURCES = {"codex_bridge_live", "codex_console_live", "cursor_print", "opencode_run"}


@dataclass(frozen=True)
class LivePreviewCandidate:
    session_id: UUID
    thread_id: str | None
    turn_key: str
    seq: int | None
    preview_text: str
    provisional_cursor: str
    provisional_complete: bool
    preview_observed_at: datetime
    source: str
    last_observation_id: str
    preview_role: str = "assistant"
    tool_name: str | None = None
    tool_input_json: dict | None = None
    tool_output_text: str | None = None
    tool_call_id: str | None = None
    tool_call_state: str | None = None


def live_preview_candidate_from_runtime_event(
    event: Any,
    *,
    observation_id: str,
) -> LivePreviewCandidate | None:
    payload = event.payload or {}
    if event.session_id is None:
        return None
    provider = (event.provider or "").strip().lower()
    if provider not in {"codex", "cursor", "opencode"}:
        return None
    source = (event.source or "").strip()
    if source.lower() not in LIVE_PREVIEW_SOURCES:
        return None
    if event.kind != "progress_signal":
        return None
    progress_kind = payload.get("progress_kind")
    if provider == "cursor" and source.lower() == "cursor_print" and progress_kind == "cursor_print_stream":
        return _cursor_print_preview_candidate(event, payload, observation_id=observation_id)
    if provider == "opencode" and source.lower() == "opencode_run" and progress_kind == "opencode_run_stream":
        return _opencode_run_preview_candidate(event, payload, observation_id=observation_id)
    if progress_kind not in {"bridge_live_transcript_delta", "console_live_tool_item"}:
        return None

    is_tool = progress_kind == "console_live_tool_item"
    command = str(payload.get("command") or "").strip()
    output = str(payload.get("output") or "")
    preview_text = (output.strip() or command) if is_tool else str(payload.get("live_text") or "").strip()
    if not preview_text:
        return None

    thread_id = _optional_str(payload.get("thread_id") or event.thread_id)
    turn_id = _optional_str(payload.get("turn_id"))
    item_id = _optional_str(payload.get("item_id"))
    turn_key = build_provisional_key(
        source=source,
        session_id=event.session_id,
        thread_id=thread_id,
        turn_id=_item_scoped_turn_id(turn_id, item_id),
    )
    seq = _coerce_seq(payload.get("seq"))
    observed_at = normalize_utc(event.occurred_at) or datetime.now(timezone.utc)
    return LivePreviewCandidate(
        session_id=event.session_id,
        thread_id=thread_id,
        turn_key=turn_key,
        seq=seq,
        preview_text=preview_text,
        provisional_cursor=build_provisional_cursor(key=turn_key, seq=seq),
        provisional_complete=bool(payload.get("turn_completed") or payload.get("completed")),
        preview_observed_at=observed_at,
        source=source,
        last_observation_id=observation_id,
        preview_role="assistant",
        tool_name="exec" if is_tool else None,
        tool_input_json={"command": command} if is_tool else None,
        tool_output_text=output if is_tool and output else None,
        tool_call_id=item_id if is_tool else None,
        tool_call_state=(_tool_call_state(payload.get("status"), completed=bool(payload.get("completed"))) if is_tool else None),
    )


def _cursor_print_preview_candidate(event: Any, payload: dict[str, Any], *, observation_id: str) -> LivePreviewCandidate | None:
    raw = payload.get("event")
    if not isinstance(raw, dict) or event.session_id is None:
        return None
    raw_type = str(raw.get("type") or "").strip()
    seq = _coerce_seq(payload.get("seq"))
    thread_id = _optional_str(payload.get("thread_id") or event.thread_id)
    turn_id = _optional_str(payload.get("turn_id"))
    observed_at = normalize_utc(event.occurred_at) or datetime.now(timezone.utc)
    if raw_type == "result" and raw.get("subtype") == "success":
        text = str(raw.get("result") or "").strip()
        if not text:
            return None
        turn_key = build_provisional_key(
            source="cursor_print",
            session_id=event.session_id,
            thread_id=thread_id,
            turn_id=turn_id,
        )
        return LivePreviewCandidate(
            session_id=event.session_id,
            thread_id=thread_id,
            turn_key=turn_key,
            seq=seq,
            preview_text=text,
            provisional_cursor=build_provisional_cursor(key=turn_key, seq=seq),
            provisional_complete=True,
            preview_observed_at=observed_at,
            source="cursor_print",
            last_observation_id=observation_id,
        )
    if raw_type == "assistant":
        message = raw.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        blocks = content if isinstance(content, list) else []
        text = "".join(str(block.get("text") or "") for block in blocks if isinstance(block, dict) and block.get("type") == "text")
        if not text:
            return None
        turn_key = build_provisional_key(
            source="cursor_print",
            session_id=event.session_id,
            thread_id=thread_id,
            turn_id=turn_id,
        )
        return LivePreviewCandidate(
            session_id=event.session_id,
            thread_id=thread_id,
            turn_key=turn_key,
            seq=seq,
            preview_text=text,
            provisional_cursor=build_provisional_cursor(key=turn_key, seq=seq),
            provisional_complete=False,
            preview_observed_at=observed_at,
            source="cursor_print",
            last_observation_id=observation_id,
        )
    if raw_type != "tool_call":
        return None
    call_id = _optional_str(raw.get("call_id"))
    call = raw.get("tool_call")
    if not isinstance(call, dict):
        return None
    tool_name, detail = _cursor_tool_detail(call)
    if detail is None:
        return None
    args = detail.get("args") if isinstance(detail.get("args"), dict) else {}
    result = detail.get("result") if isinstance(detail.get("result"), dict) else {}
    success = result.get("success") if isinstance(result.get("success"), dict) else {}
    rejected = result.get("rejected") if isinstance(result.get("rejected"), dict) else {}
    command = str(args.get("command") or "").strip()
    output = _cursor_tool_output(success, rejected)
    subtype = str(raw.get("subtype") or "").strip()
    turn_key = build_provisional_key(
        source="cursor_print",
        session_id=event.session_id,
        thread_id=thread_id,
        turn_id=_item_scoped_turn_id(turn_id, call_id),
    )
    return LivePreviewCandidate(
        session_id=event.session_id,
        thread_id=thread_id,
        turn_key=turn_key,
        seq=seq,
        preview_text=output.strip() or command or str(detail.get("description") or tool_name),
        provisional_cursor=build_provisional_cursor(key=turn_key, seq=seq),
        provisional_complete=subtype == "completed",
        preview_observed_at=observed_at,
        source="cursor_print",
        last_observation_id=observation_id,
        preview_role="assistant",
        tool_name=tool_name,
        tool_input_json=dict(args) if args else None,
        tool_output_text=output or None,
        tool_call_id=call_id,
        tool_call_state="completed" if subtype == "completed" else "running",
    )


def _opencode_run_preview_candidate(event: Any, payload: dict[str, Any], *, observation_id: str) -> LivePreviewCandidate | None:
    raw = payload.get("event")
    if not isinstance(raw, dict) or event.session_id is None:
        return None
    part = raw.get("part") if isinstance(raw.get("part"), dict) else {}
    raw_type = str(raw.get("type") or "").strip()
    seq = _coerce_seq(payload.get("seq"))
    thread_id = _optional_str(payload.get("thread_id") or event.thread_id)
    turn_id = _optional_str(payload.get("turn_id"))
    observed_at = normalize_utc(event.occurred_at) or datetime.now(timezone.utc)
    if raw_type == "text" and str(part.get("type") or "") == "text":
        text = str(part.get("text") or "").strip()
        if not text:
            return None
        turn_key = build_provisional_key(
            source="opencode_run",
            session_id=event.session_id,
            thread_id=thread_id,
            turn_id=turn_id,
        )
        return LivePreviewCandidate(
            session_id=event.session_id,
            thread_id=thread_id,
            turn_key=turn_key,
            seq=seq,
            preview_text=text,
            provisional_cursor=build_provisional_cursor(key=turn_key, seq=seq),
            provisional_complete=False,
            preview_observed_at=observed_at,
            source="opencode_run",
            last_observation_id=observation_id,
        )
    if raw_type != "tool_use" or str(part.get("type") or "") != "tool":
        return None
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    tool_name = str(part.get("tool") or "Tool")
    call_id = _optional_str(part.get("callID"))
    status = str(state.get("status") or "pending")
    tool_input = state.get("input") if isinstance(state.get("input"), dict) else None
    output = str(state.get("output") or "")
    preview_text = output.strip() or json.dumps(tool_input or {}, ensure_ascii=False, sort_keys=True) or tool_name
    turn_key = build_provisional_key(
        source="opencode_run",
        session_id=event.session_id,
        thread_id=thread_id,
        turn_id=_item_scoped_turn_id(turn_id, call_id),
    )
    return LivePreviewCandidate(
        session_id=event.session_id,
        thread_id=thread_id,
        turn_key=turn_key,
        seq=seq,
        preview_text=preview_text,
        provisional_cursor=build_provisional_cursor(key=turn_key, seq=seq),
        provisional_complete=status in {"completed", "error"},
        preview_observed_at=observed_at,
        source="opencode_run",
        last_observation_id=observation_id,
        preview_role="assistant",
        tool_name=tool_name,
        tool_input_json=dict(tool_input) if tool_input else None,
        tool_output_text=output or None,
        tool_call_id=call_id,
        tool_call_state=("failed" if status == "error" else "completed" if status == "completed" else "running"),
    )


def _cursor_tool_detail(call: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    for key, label in (
        ("shellToolCall", "Shell"),
        ("mcpToolCall", "MCP"),
        ("readToolCall", "Read"),
        ("writeToolCall", "Write"),
    ):
        value = call.get(key)
        if isinstance(value, dict):
            return label, value
    return "Tool", None


def _cursor_tool_output(success: dict[str, Any], rejected: dict[str, Any]) -> str:
    for key in ("stdout", "interleavedOutput", "content", "output", "text"):
        value = success.get(key)
        if isinstance(value, str) and value:
            return value
    reason = rejected.get("reason")
    if isinstance(reason, str) and reason:
        return reason
    return json.dumps(success, ensure_ascii=False, sort_keys=True) if success else ""


def upsert_session_live_preview(db: Session, candidate: LivePreviewCandidate) -> bool:
    existing = db.get(SessionLivePreview, candidate.session_id)
    return _upsert_live_preview_row(db, candidate, existing, SessionLivePreview, session_id=candidate.session_id)


def upsert_live_session_live_preview(db: Session, candidate: LivePreviewCandidate) -> bool:
    existing = db.get(LiveSessionLivePreview, str(candidate.session_id))
    return _upsert_live_preview_row(db, candidate, existing, LiveSessionLivePreview, session_id=str(candidate.session_id))


def _upsert_live_preview_row(
    db: Session,
    candidate: LivePreviewCandidate,
    existing: Any,
    model,
    *,
    session_id: UUID | str,
) -> bool:
    now = datetime.now(timezone.utc)
    if existing is not None and existing.last_observation_id == candidate.last_observation_id:
        return False
    if existing is not None and existing.superseded_at is not None:
        superseded_at = normalize_utc(existing.superseded_at)
        candidate_at = normalize_utc(candidate.preview_observed_at)
        if superseded_at is not None and candidate_at is not None and candidate_at <= superseded_at:
            return False
    if existing is not None and not _candidate_should_replace(candidate, existing):
        return False

    if existing is None:
        db.add(
            model(
                session_id=session_id,
                thread_id=candidate.thread_id,
                turn_key=candidate.turn_key,
                seq=candidate.seq,
                preview_text=candidate.preview_text,
                preview_role=candidate.preview_role,
                tool_name=candidate.tool_name,
                tool_input_json=(json.dumps(candidate.tool_input_json) if candidate.tool_input_json is not None else None),
                tool_output_text=candidate.tool_output_text,
                tool_call_id=candidate.tool_call_id,
                tool_call_state=candidate.tool_call_state,
                provisional_cursor=candidate.provisional_cursor,
                provisional_complete=1 if candidate.provisional_complete else 0,
                event_origin=EVENT_ORIGIN_LIVE_PROVISIONAL,
                preview_observed_at=candidate.preview_observed_at,
                preview_updated_at=now,
                source=candidate.source,
                last_observation_id=candidate.last_observation_id,
            )
        )
        return True

    existing.thread_id = candidate.thread_id
    existing.turn_key = candidate.turn_key
    existing.seq = candidate.seq
    existing.preview_text = candidate.preview_text
    existing.preview_role = candidate.preview_role
    existing.tool_name = candidate.tool_name
    existing.tool_input_json = json.dumps(candidate.tool_input_json) if candidate.tool_input_json is not None else None
    existing.tool_output_text = candidate.tool_output_text
    existing.tool_call_id = candidate.tool_call_id
    existing.tool_call_state = candidate.tool_call_state
    existing.provisional_cursor = candidate.provisional_cursor
    existing.provisional_complete = 1 if candidate.provisional_complete else 0
    existing.event_origin = EVENT_ORIGIN_LIVE_PROVISIONAL
    existing.preview_observed_at = candidate.preview_observed_at
    existing.preview_updated_at = now
    existing.source = candidate.source
    existing.last_observation_id = candidate.last_observation_id
    existing.superseded_at = None
    existing.superseded_by_event_id = None
    existing.superseded_reason = None
    return True


def supersede_session_live_preview(
    db: Session,
    *,
    session_id: UUID,
    durable_at: datetime | None,
    durable_event_id: int | None = None,
    reason: str = "superseded_by_durable",
) -> bool:
    row = db.get(SessionLivePreview, session_id)
    if row is None or row.superseded_at is not None:
        return False
    normalized_durable_at = normalize_utc(durable_at)
    preview_at = normalize_utc(row.preview_observed_at)
    if normalized_durable_at is None or preview_at is None or normalized_durable_at < preview_at:
        return False

    now = datetime.now(timezone.utc)
    row.superseded_at = normalized_durable_at
    row.preview_updated_at = now
    row.superseded_by_event_id = durable_event_id
    row.superseded_reason = reason
    return True


def load_session_live_preview_map(db: Session, session_ids: list[UUID]) -> dict[str, TranscriptPreview]:
    if not session_ids:
        return {}
    rows = (
        db.query(SessionLivePreview)
        .filter(SessionLivePreview.session_id.in_(session_ids))
        .filter(SessionLivePreview.superseded_at.is_(None))
        .all()
    )
    return preview_map_from_rows(rows)


def load_live_session_live_preview_map(db: Session, session_ids: list[UUID]) -> dict[str, TranscriptPreview]:
    if not session_ids:
        return {}
    session_id_strings = [str(session_id) for session_id in session_ids]
    rows = (
        db.query(LiveSessionLivePreview)
        .filter(LiveSessionLivePreview.session_id.in_(session_id_strings))
        .filter(LiveSessionLivePreview.superseded_at.is_(None))
        .all()
    )
    return preview_map_from_rows(rows)


def preview_map_from_rows(rows) -> dict[str, TranscriptPreview]:
    previews: dict[str, TranscriptPreview] = {}
    for row in rows:
        text = str(row.preview_text or "").strip()
        if not text:
            continue
        timestamp = normalize_utc(row.preview_observed_at)
        if timestamp is None:
            continue
        event_id = int(row.seq) if row.seq is not None else int(timestamp.timestamp() * 1000)
        previews[str(row.session_id)] = TranscriptPreview(
            event_id=event_id,
            text=text,
            event_origin=row.event_origin or EVENT_ORIGIN_LIVE_PROVISIONAL,
            timestamp=timestamp,
            provisional_cursor=row.provisional_cursor,
            provisional_complete=bool(row.provisional_complete),
            role=row.preview_role or "assistant",
            tool_name=row.tool_name,
            tool_input_json=json.loads(row.tool_input_json) if row.tool_input_json else None,
            tool_output_text=row.tool_output_text,
            tool_call_id=row.tool_call_id,
            tool_call_state=row.tool_call_state,
        )
    return previews


def _candidate_should_replace(candidate: LivePreviewCandidate, existing: SessionLivePreview) -> bool:
    candidate_at = normalize_utc(candidate.preview_observed_at) or datetime.min.replace(tzinfo=timezone.utc)
    existing_at = normalize_utc(existing.preview_observed_at) or datetime.min.replace(tzinfo=timezone.utc)
    if candidate.turn_key != existing.turn_key:
        return candidate_at >= existing_at
    if candidate.seq is not None and existing.seq is not None:
        if candidate.seq != existing.seq:
            return candidate.seq > existing.seq
        return candidate_at >= existing_at
    if candidate.seq is not None and existing.seq is None:
        return True
    if candidate.seq is None and existing.seq is not None:
        return False
    return candidate_at >= existing_at


def _optional_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _item_scoped_turn_id(turn_id: str | None, item_id: str | None) -> str | None:
    if not item_id:
        return turn_id
    return f"{turn_id or 'unknown-turn'}#{item_id}"


def _coerce_seq(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _tool_call_state(value: Any, *, completed: bool) -> str:
    status = str(value or "").strip().lower()
    if completed or status in {"completed", "failed", "cancelled"}:
        return "completed"
    return "running"
