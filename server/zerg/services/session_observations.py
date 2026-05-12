"""Append-only session observation writes.

This is the write-side bus for raw facts that later reducers materialize into
transcript, archive, runtime, and timeline read models.
"""

from __future__ import annotations

import hashlib
import json
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

OBS_KIND_PROVIDER_SOURCE_LINE = "provider_source_line"
OBS_KIND_RUNTIME_SIGNAL = "runtime_signal"
OBS_KIND_BRIDGE_TRANSCRIPT_DELTA = "bridge_transcript_delta"


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
) -> bool:
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    stmt = (
        sqlite_insert(SessionObservation)
        .values(
            observation_id=observation_id,
            session_id=session_id,
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
    return bool(result.rowcount)


def record_runtime_observation(db: Session, event: Any, *, received_at: datetime | None = None) -> bool:
    payload = event.payload or {}
    kind = OBS_KIND_BRIDGE_TRANSCRIPT_DELTA if _is_bridge_transcript_delta(event, payload) else OBS_KIND_RUNTIME_SIGNAL
    return record_session_observation(
        db,
        observation_id=f"runtime:{event.source}:{event.dedupe_key}",
        session_id=event.session_id,
        runtime_key=event.runtime_key,
        provider=event.provider,
        device_id=event.device_id,
        source_domain=SOURCE_DOMAIN_RUNTIME,
        source=event.source,
        kind=kind,
        source_cursor=f"{event.kind}:{event.dedupe_key}",
        observed_at=event.occurred_at,
        received_at=received_at,
        payload={
            "kind": event.kind,
            "phase": event.phase,
            "tool_name": event.tool_name,
            "freshness_ms": event.freshness_ms,
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
) -> bool:
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
        payload={
            "branch_id": branch_id,
            "revision": revision,
            "line_hash": line_hash,
            "raw_json": raw_json,
        },
    )


def _is_bridge_transcript_delta(event: Any, payload: dict[str, Any]) -> bool:
    return (
        (event.provider or "").strip().lower() == "codex"
        and (event.source or "").strip().lower() == "codex_bridge_live"
        and event.kind == "progress_signal"
        and payload.get("progress_kind") == "bridge_live_transcript_delta"
    )


def _hash_parts(*parts: str) -> str:
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(part.encode("utf-8"))
        hasher.update(b"\0")
    return hasher.hexdigest()
