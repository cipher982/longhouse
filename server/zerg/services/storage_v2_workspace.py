"""Canonical workspace projection for storage-v2 sessions."""

from __future__ import annotations

import asyncio
import hashlib
import json
from uuid import UUID

from fastapi import HTTPException
from fastapi import status

from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.client import CatalogUnavailable
from zerg.config import get_settings
from zerg.routers.agents_storage_v2 import read_storage_v2_session_events_page
from zerg.services.catalog_read_gateway import CatalogReadError
from zerg.services.catalogd_supervisor import get_catalogd_client
from zerg.services.live_catalog_timeline import canonical_session_detail_enabled
from zerg.services.live_catalog_timeline import read_live_catalog_session


def _event_projection(
    event: dict[str, object],
    *,
    session_id: UUID,
    closed: bool,
    completed_tool_call_ids: set[str],
) -> dict[str, object]:
    event_id = str(event["event_id"])
    tool_call_id = str(event["tool_call_id"]) if event.get("tool_call_id") else None
    tool_call_state = None
    if event.get("tool_name"):
        tool_call_state = "completed" if tool_call_id in completed_tool_call_ids else ("dropped" if closed else "running")
    return {
        "kind": "event",
        "session_id": str(session_id),
        "timestamp": event["timestamp"],
        "event": {
            "id": event_id,
            "cursor": event["cursor"],
            "role": event["role"],
            "content_text": event.get("content_text"),
            "raw_content_text": None,
            "input_origin": None,
            "tool_name": event.get("tool_name"),
            "tool_input_json": event.get("tool_input_json"),
            "tool_output_text": event.get("tool_output_text"),
            "tool_output_truncated": False,
            "tool_output_original_chars": None,
            "tool_call_id": tool_call_id,
            "timestamp": event["timestamp"],
            "in_active_context": True,
            "branch_id": None,
            "is_head_branch": event.get("branch_kind") != "abandoned",
            "event_origin": "durable",
            "provisional_state": None,
            "provisional_cursor": None,
            "provisional_complete": False,
            "reconciled_event_id": None,
            "tool_call_state": tool_call_state,
            "media_refs": [],
        },
        "action": None,
        "continued_from_session_id": None,
        "continuation_kind": None,
        "origin_label": None,
        "parent_origin_label": None,
        "parent_continuation_kind": None,
        "branched_from_event_id": None,
    }


def _workspace_envelope(
    *,
    session_id: UUID,
    session,
    session_commit_seq: str,
    branch_mode: str,
    anchor: str,
    cursor: str | None,
    storage: dict[str, object] | None,
    page: dict[str, object] | None,
) -> dict[str, object]:
    """Build one workspace shape; archive readiness only controls its event page."""

    control_only = storage is None
    events = page.get("events") if page is not None else []
    if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="The render projection is invalid.")
    completed_tool_call_ids = {str(event["tool_call_id"]) for event in events if event.get("role") == "tool" and event.get("tool_call_id")}
    items = [
        _event_projection(
            event,
            session_id=session_id,
            closed=session.runtime_display.lifecycle == "closed",
            completed_tool_call_ids=completed_tool_call_ids,
        )
        for event in events
    ]
    total = int(page.get("total") or 0) if page is not None else 0
    latest_event_id = str(events[-1]["event_id"]) if events else None
    fingerprint_payload = {
        "session_commit_seq": session_commit_seq,
        "storage_commit_seq": storage.get("commit_seq") if storage is not None else None,
        "control_only": control_only,
        "generation_id": page.get("generation_id") if page is not None else None,
        "latest_event_id": latest_event_id,
        "next_cursor": page.get("next_cursor") if page is not None else None,
    }
    fingerprint = hashlib.sha256(json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    session_json = session.model_dump(mode="json")
    storage_session = storage.get("session") if storage is not None else None
    return {
        "session": session_json,
        "thread": {
            "root_session_id": str(session_id),
            "head_session_id": str(session_id),
            "sessions": [session_json],
        },
        "projection": {
            "root_session_id": str(session_id),
            "focus_session_id": str(session_id),
            "head_session_id": str(session_id),
            "path_session_ids": [str(session_id)],
            "items": items,
            "total": total,
            "page_offset": (max(0, total - len(items)) if anchor == "tail" and cursor is None else 0),
            "branch_mode": branch_mode,
            "abandoned_events": 0,
            "generation_id": page.get("generation_id") if page is not None else None,
            "next_cursor": page.get("next_cursor") if page is not None else None,
            "has_more": page.get("has_more") is True if page is not None else False,
        },
        "workspace_revision": {
            "latest_event_id": latest_event_id,
            "latest_session_updated_at": storage_session.get("updated_at") if isinstance(storage_session, dict) else None,
            "latest_runtime_signal_at": None,
            "runtime_version_sum": 0,
            "pause_request_count": 0,
            "pause_request_fingerprint": None,
            "managed_control_count": 1 if session.capabilities.live_control_available or session.capabilities.can_start_turn else 0,
            "managed_control_fingerprint": None,
            "live_preview_updated_at": None,
            "thread_session_count": 1,
            "fingerprint": fingerprint,
        },
        "control_only": control_only,
    }


async def build_storage_v2_workspace(
    *,
    session_id: UUID,
    owner_id: int,
    branch_mode: str,
    limit: int,
    cursor: str | None = None,
    anchor: str = "tail",
) -> dict[str, object] | None:
    """Return a storage-v2 workspace, including live control-only sessions.

    A managed control lease is useful only if the session remains openable.  A
    provider may not yet have a transcript source, however, so its first
    workspace is allowed to be an empty, explicitly control-only projection.
    """

    if branch_mode not in {"head", "all"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="branch_mode must be one of: head, all")
    if anchor not in {"start", "tail"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="anchor must be one of: start, tail")
    catalogd = get_catalogd_client()
    if catalogd is None:
        if get_settings().testing:
            return None
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="The session catalog is unavailable.")
    try:
        session, _provider_alias, session_commit_seq = await asyncio.to_thread(
            read_live_catalog_session,
            session_id,
            owner_id=owner_id,
            serve_mode="canonical" if canonical_session_detail_enabled() else "legacy",
        )
    except CatalogReadError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="The session catalog is unavailable.") from exc
    if session is None:
        return None
    try:
        storage_result = await catalogd.call("storage.session.read.v2", {"session_id": str(session_id)})
    except (CatalogRemoteError, CatalogUnavailable) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="The session catalog is unavailable.") from exc
    storage = storage_result if storage_result.get("found") is True else None
    if storage is None:
        if not (getattr(session, "origin_kind", None) == "console" or session.capabilities.live_control_available):
            return None
        page = None
    else:
        storage_session = storage.get("session")
        if not isinstance(storage_session, dict) or str(storage_session.get("owner_id")) != str(owner_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Session {session_id} not found")
        page = await read_storage_v2_session_events_page(
            session_id=session_id,
            owner_id=str(owner_id),
            cursor=cursor,
            anchor=anchor,
            limit=limit,
        )
    return _workspace_envelope(
        session_id=session_id,
        session=session,
        session_commit_seq=session_commit_seq,
        branch_mode=branch_mode,
        anchor=anchor,
        cursor=cursor,
        storage=storage,
        page=page,
    )


__all__ = ["build_storage_v2_workspace"]
