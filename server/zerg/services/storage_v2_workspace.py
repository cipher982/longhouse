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
from zerg.services.catalogd_supervisor import get_catalogd_client
from zerg.services.live_catalog_timeline import read_live_catalog_session


def _event_projection(event: dict[str, object], *, session_id: UUID, closed: bool) -> dict[str, object]:
    event_id = str(event["event_id"])
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
            "tool_call_id": event.get("tool_call_id"),
            "timestamp": event["timestamp"],
            "in_active_context": True,
            "branch_id": None,
            "is_head_branch": event.get("branch_kind") != "abandoned",
            "event_origin": "durable",
            "provisional_state": None,
            "provisional_cursor": None,
            "provisional_complete": False,
            "reconciled_event_id": None,
            "tool_call_state": "dropped" if closed and event.get("tool_name") else None,
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


async def build_storage_v2_workspace(
    *,
    session_id: UUID,
    owner_id: int,
    branch_mode: str,
    limit: int,
    cursor: str | None = None,
) -> dict[str, object] | None:
    """Return a storage-v2 workspace, or ``None`` for a legacy-only session."""

    if branch_mode not in {"head", "all"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="branch_mode must be one of: head, all")
    catalogd = get_catalogd_client()
    if catalogd is None:
        if get_settings().testing:
            return None
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="The session catalog is unavailable.")
    try:
        storage = await catalogd.call("storage.session.read.v2", {"session_id": str(session_id)})
    except (CatalogRemoteError, CatalogUnavailable) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="The session catalog is unavailable.") from exc
    if storage.get("found") is not True:
        return None
    storage_session = storage.get("session")
    if not isinstance(storage_session, dict) or str(storage_session.get("owner_id")) != str(owner_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Session {session_id} not found")

    session, _provider_alias, session_commit_seq = await asyncio.to_thread(read_live_catalog_session, session_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="The session projection is unavailable.")
    page = await read_storage_v2_session_events_page(
        session_id=session_id,
        owner_id=str(owner_id),
        cursor=cursor,
        anchor="tail",
        limit=limit,
    )
    events = page.get("events")
    if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="The render projection is invalid.")
    closed = session.lifecycle == "closed"
    items = [_event_projection(event, session_id=session_id, closed=closed) for event in events]
    session_json = session.model_dump(mode="json")
    latest_event_id = str(events[-1]["event_id"]) if events else None
    fingerprint_payload = {
        "session_commit_seq": session_commit_seq,
        "storage_commit_seq": storage.get("commit_seq"),
        "generation_id": page.get("generation_id"),
        "latest_event_id": latest_event_id,
        "next_cursor": page.get("next_cursor"),
    }
    fingerprint = hashlib.sha256(json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    projection = {
        "root_session_id": str(session_id),
        "focus_session_id": str(session_id),
        "head_session_id": str(session_id),
        "path_session_ids": [str(session_id)],
        "items": items,
        "total": int(page.get("total") or 0),
        "page_offset": max(0, int(page.get("total") or 0) - len(items)) if cursor is None else 0,
        "branch_mode": branch_mode,
        "abandoned_events": 0,
        "generation_id": page.get("generation_id"),
        "next_cursor": page.get("next_cursor"),
        "has_more": page.get("has_more") is True,
    }
    return {
        "session": session_json,
        "thread": {
            "root_session_id": str(session_id),
            "head_session_id": str(session_id),
            "sessions": [session_json],
        },
        "projection": projection,
        "workspace_revision": {
            "latest_event_id": latest_event_id,
            "latest_session_updated_at": storage_session.get("updated_at"),
            "latest_runtime_signal_at": None,
            "runtime_version_sum": 0,
            "pause_request_count": 0,
            "pause_request_fingerprint": None,
            "managed_control_count": 1 if session.capabilities.live_control_available else 0,
            "managed_control_fingerprint": None,
            "live_preview_updated_at": None,
            "thread_session_count": 1,
            "fingerprint": fingerprint,
        },
    }


__all__ = ["build_storage_v2_workspace"]
