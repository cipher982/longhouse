"""Append-only session observation writes.

This is the write-side bus for raw facts that later reducers materialize into
transcript, archive, runtime, and timeline read models.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Any
from uuid import UUID

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from zerg.models.agents import SessionObservation
from zerg.services.raw_json_compression import CODEC_PLAIN
from zerg.utils.time import normalize_utc

SOURCE_DOMAIN_TRANSCRIPT = "transcript"
SOURCE_DOMAIN_RUNTIME = "runtime"
SOURCE_DOMAIN_CLIENT = "client"
SOURCE_DOMAIN_SERVER = "server"

OBS_KIND_PROVIDER_SOURCE_LINE = "provider_source_line"
OBS_KIND_PROVIDER_EVENT = "provider_event"
OBS_KIND_RUNTIME_SIGNAL = "runtime_signal"
OBS_KIND_BRIDGE_TRANSCRIPT_DELTA = "bridge_transcript_delta"
OBS_KIND_CLIENT_RENDER = "client_render"
OBS_KIND_SERVER_FANOUT = "server_fanout"


@dataclass(frozen=True)
class ObservationWriteResult:
    observation: SessionObservation | None
    inserted: bool


def record_session_observation(
    db: Session,
    *,
    observation_id: str,
    session_id: UUID | None,
    runtime_key: str | None,
    provider: str,
    device_id: str | None,
    source_domain: str,
    source: str,
    kind: str,
    observed_at: datetime,
    payload: dict[str, Any],
    received_at: datetime | None = None,
    source_path: str | None = None,
    source_offset: int | None = None,
    source_cursor: str | None = None,
    thread_id: UUID | None = None,
    load_observation: bool = True,
) -> ObservationWriteResult:
    # Phase 2 dual-write: ensure thread_id stamping never silently drops to NULL.
    # Callers may pass an explicit thread_id; when absent and a session_id is
    # available, materialize the primary thread on the fly so observations
    # always carry the kernel pointer.
    if thread_id is None and session_id is not None:
        from zerg.services.agents.kernel_writes import ensure_thread_id_for_session

        thread_id = ensure_thread_id_for_session(db, session_id)

    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    stmt = (
        sqlite_insert(SessionObservation)
        .values(
            observation_id=observation_id,
            session_id=session_id,
            thread_id=thread_id,
            runtime_key=runtime_key,
            provider=(provider or "unknown").strip() or "unknown",
            device_id=device_id,
            source_domain=source_domain,
            source=source,
            kind=kind,
            source_path=source_path,
            source_offset=source_offset,
            source_cursor=source_cursor,
            observed_at=normalize_utc(observed_at) or datetime.now(timezone.utc),
            received_at=normalize_utc(received_at) or datetime.now(timezone.utc),
            payload_json=payload_json,
            payload_json_z=None,
            payload_json_codec=CODEC_PLAIN,
        )
        .on_conflict_do_nothing(index_elements=["observation_id"])
    )
    result = db.execute(stmt)
    if result.rowcount:
        if not load_observation:
            return ObservationWriteResult(observation=None, inserted=True)
        db.flush()
        observation = db.query(SessionObservation).filter(SessionObservation.observation_id == observation_id).first()
        return ObservationWriteResult(observation=observation, inserted=True)
    return ObservationWriteResult(observation=None, inserted=False)


def record_runtime_observation(
    db: Session,
    event: Any,
    *,
    received_at: datetime | None = None,
    thread_id: UUID | None = None,
    load_observation: bool = True,
) -> ObservationWriteResult:
    payload = event.payload or {}
    dedupe_key = _runtime_dedupe_key(event)
    kind = OBS_KIND_BRIDGE_TRANSCRIPT_DELTA if _is_bridge_transcript_delta(event, payload) else OBS_KIND_RUNTIME_SIGNAL
    return record_session_observation(
        db,
        observation_id=f"runtime:{event.source}:{dedupe_key}",
        session_id=event.session_id,
        thread_id=thread_id,
        runtime_key=event.runtime_key,
        provider=event.provider,
        device_id=event.device_id,
        source_domain=SOURCE_DOMAIN_RUNTIME,
        source=event.source,
        kind=kind,
        source_cursor=f"{event.kind}:{dedupe_key}",
        observed_at=event.occurred_at,
        received_at=received_at,
        load_observation=load_observation,
        payload={
            "kind": event.kind,
            "phase": event.phase,
            "tool_name": event.tool_name,
            "freshness_ms": event.freshness_ms,
            "dedupe_key": dedupe_key,
            "payload": payload,
        },
    )


def record_source_line_observation(
    db: Session,
    *,
    session_id: UUID,
    provider: str,
    device_id: str | None,
    source: str,
    source_path: str,
    source_offset: int,
    branch_id: int,
    revision: int,
    line_hash: str,
    raw_json: str,
    observed_at: datetime,
    received_at: datetime | None = None,
    thread_id: UUID | None = None,
    load_observation: bool = True,
) -> ObservationWriteResult:
    observation_id = "source_line:" + _hash_parts(
        str(session_id),
        str(branch_id),
        source_path,
        str(source_offset),
        line_hash,
    )
    return record_session_observation(
        db,
        observation_id=observation_id,
        session_id=session_id,
        thread_id=thread_id,
        runtime_key=None,
        provider=provider,
        device_id=device_id,
        source_domain=SOURCE_DOMAIN_TRANSCRIPT,
        source=source,
        kind=OBS_KIND_PROVIDER_SOURCE_LINE,
        source_path=source_path,
        source_offset=source_offset,
        source_cursor=f"{source_path}:{source_offset}:{revision}",
        observed_at=observed_at,
        received_at=received_at,
        load_observation=load_observation,
        payload={
            "branch_id": branch_id,
            "revision": revision,
            "line_hash": line_hash,
            "raw_json": raw_json,
        },
    )


def record_provider_event_observation(
    db: Session,
    *,
    session_id: UUID,
    provider: str,
    device_id: str | None,
    source: str,
    branch_id: int,
    role: str,
    timestamp: datetime,
    event_hash: str,
    content_text: str | None = None,
    tool_name: str | None = None,
    tool_input_json: Any | None = None,
    tool_output_text: str | None = None,
    tool_call_id: str | None = None,
    source_path: str | None = None,
    source_offset: int | None = None,
    raw_json: str | None = None,
    event_uuid: str | None = None,
    parent_event_uuid: str | None = None,
    received_at: datetime | None = None,
    thread_id: UUID | None = None,
    load_observation: bool = True,
) -> ObservationWriteResult:
    identity = event_uuid or _hash_parts(
        str(session_id),
        str(branch_id),
        source_path or "",
        str(source_offset) if source_offset is not None else "",
        event_hash,
        role,
        timestamp.isoformat(),
    )
    observation_id = "provider_event:" + _hash_parts(str(session_id), str(branch_id), identity)
    return record_session_observation(
        db,
        observation_id=observation_id,
        session_id=session_id,
        thread_id=thread_id,
        runtime_key=None,
        provider=provider,
        device_id=device_id,
        source_domain=SOURCE_DOMAIN_TRANSCRIPT,
        source=source,
        kind=OBS_KIND_PROVIDER_EVENT,
        source_path=source_path,
        source_offset=source_offset,
        source_cursor=event_uuid
        or (
            f"{source_path}:{source_offset}:{event_hash}"
            if source_path is not None and source_offset is not None
            else identity
        ),
        observed_at=timestamp,
        received_at=received_at,
        load_observation=load_observation,
        payload={
            "branch_id": branch_id,
            "role": role,
            "content_text": content_text,
            "tool_name": tool_name,
            "tool_input_json": tool_input_json,
            "tool_output_text": tool_output_text,
            "tool_call_id": tool_call_id,
            "timestamp": timestamp.isoformat(),
            "event_hash": event_hash,
            "raw_json": raw_json,
            "event_uuid": event_uuid,
            "parent_event_uuid": parent_event_uuid,
        },
    )


def _is_bridge_transcript_delta(event: Any, payload: dict[str, Any]) -> bool:
    return (
        (event.provider or "").strip().lower() == "codex"
        and (event.source or "").strip().lower() == "codex_bridge_live"
        and event.kind == "progress_signal"
        and payload.get("progress_kind") == "bridge_live_transcript_delta"
    )


def _runtime_dedupe_key(event: Any) -> str:
    raw = str(getattr(event, "dedupe_key", "") or "").strip()
    if raw:
        return raw
    raise ValueError("runtime observations require a non-empty dedupe_key")


def _hash_parts(*parts: str) -> str:
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(part.encode("utf-8"))
        hasher.update(b"\0")
    return hasher.hexdigest()
