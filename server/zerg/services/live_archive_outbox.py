"""Live Store outbox bridge into the archive SQLite lane."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from zerg.models.agents import AgentHeartbeat
from zerg.models.live_store import LiveArchiveOutbox
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import ingest_runtime_events
from zerg.utils.time import normalize_utc

HEARTBEAT_STAMP_KIND = "heartbeat_stamp.v1"
RUNTIME_EVENT_KIND = "runtime_event.v1"


@dataclass(frozen=True)
class LiveArchiveDrainResult:
    processed: int = 0
    drained: int = 0
    failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "processed": self.processed,
            "drained": self.drained,
            "failed": self.failed,
        }


def heartbeat_stamp_idempotency_key(heartbeat: dict[str, Any]) -> str:
    device_id = str(heartbeat.get("device_id") or "").strip()
    received_at = heartbeat.get("received_at")
    if isinstance(received_at, datetime):
        received_key = received_at.isoformat()
    else:
        received_key = str(received_at or "").strip()
    return f"{HEARTBEAT_STAMP_KIND}:{device_id}:{received_key}"


def enqueue_heartbeat_stamp_outbox(
    db: Session,
    heartbeat: dict[str, Any],
    *,
    idempotency_key: str | None = None,
) -> bool:
    """Queue a live heartbeat stamp for async archive durability.

    This is intentionally called inside the same Live Store transaction as the
    hot heartbeat stamp write. If enqueue fails, the hot write fails too; the
    live lane must not claim a fact that cannot be durably replayed.
    """

    key = idempotency_key or heartbeat_stamp_idempotency_key(heartbeat)
    existing = db.query(LiveArchiveOutbox.id).filter(LiveArchiveOutbox.idempotency_key == key).first()
    if existing is not None:
        return False
    db.add(
        LiveArchiveOutbox(
            idempotency_key=key,
            kind=HEARTBEAT_STAMP_KIND,
            payload_json=json.dumps({"heartbeat": _jsonable(heartbeat)}, sort_keys=True),
        )
    )
    return True


def runtime_event_idempotency_key(event: RuntimeEventIngest) -> str:
    occurred_at = normalize_utc(event.occurred_at) or event.occurred_at
    raw_key = f"{RUNTIME_EVENT_KIND}:{event.source}:{event.dedupe_key}:" f"{event.runtime_key}:{event.kind}:{occurred_at.isoformat()}"
    if len(raw_key) <= 512:
        return raw_key
    digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    return f"{RUNTIME_EVENT_KIND}:sha256:{digest}"


def enqueue_runtime_event_outbox(
    db: Session,
    event: RuntimeEventIngest,
    *,
    idempotency_key: str | None = None,
) -> bool:
    """Queue a runtime event for archive durability in the live transaction."""

    key = idempotency_key or runtime_event_idempotency_key(event)
    existing = db.query(LiveArchiveOutbox.id).filter(LiveArchiveOutbox.idempotency_key == key).first()
    if existing is not None:
        return False
    db.add(
        LiveArchiveOutbox(
            idempotency_key=key,
            kind=RUNTIME_EVENT_KIND,
            payload_json=json.dumps(
                {"event": _jsonable(event.model_dump())},
                sort_keys=True,
            ),
        )
    )
    return True


def enqueue_runtime_events_outbox(db: Session, events: list[RuntimeEventIngest]) -> int:
    queued = 0
    for event in events:
        if enqueue_runtime_event_outbox(db, event):
            queued += 1
    return queued


def cleanup_drained_live_archive_outbox(
    db: Session,
    *,
    older_than: datetime,
    limit: int = 1000,
) -> int:
    """Delete drained outbox rows older than the retention cutoff."""

    if limit <= 0:
        return 0
    cutoff = normalize_utc(older_than) or older_than
    ids = [
        row.id
        for row in (
            db.query(LiveArchiveOutbox.id)
            .filter(LiveArchiveOutbox.drained_at.isnot(None))
            .filter(LiveArchiveOutbox.drained_at < cutoff)
            .order_by(LiveArchiveOutbox.drained_at.asc(), LiveArchiveOutbox.id.asc())
            .limit(limit)
            .all()
        )
    ]
    if not ids:
        return 0
    return db.query(LiveArchiveOutbox).filter(LiveArchiveOutbox.id.in_(ids)).delete(synchronize_session=False)


def drain_live_archive_outbox(
    live_db: Session,
    archive_db: Session,
    *,
    limit: int = 100,
    now: datetime | None = None,
) -> LiveArchiveDrainResult:
    """Drain a bounded Live Store outbox batch into archive SQLite.

    Archive commit happens before the outbox row is marked drained. If the live
    commit fails after the archive commit, a future drain retries the row and
    idempotency prevents duplicate archive heartbeat rows.
    """

    if limit <= 0:
        return LiveArchiveDrainResult()

    drained_at = now or datetime.now(timezone.utc)
    rows = (
        live_db.query(LiveArchiveOutbox)
        .filter(LiveArchiveOutbox.drained_at.is_(None))
        .order_by(LiveArchiveOutbox.created_at.asc(), LiveArchiveOutbox.id.asc())
        .limit(limit)
        .all()
    )
    if not rows:
        return LiveArchiveDrainResult()

    processed = 0
    drained = 0
    failed = 0
    for row in rows:
        processed += 1
        row.attempts = int(row.attempts or 0) + 1
        try:
            _drain_row(row, archive_db)
            archive_db.commit()
        except Exception as exc:
            archive_db.rollback()
            row.last_error = f"{type(exc).__name__}: {exc}"
            failed += 1
            continue

        row.drained_at = drained_at
        row.last_error = None
        drained += 1

    try:
        live_db.commit()
    except Exception:
        live_db.rollback()
        return LiveArchiveDrainResult(processed=processed, drained=0, failed=processed)

    return LiveArchiveDrainResult(processed=processed, drained=drained, failed=failed)


def _drain_row(row: LiveArchiveOutbox, archive_db: Session) -> None:
    if row.kind == HEARTBEAT_STAMP_KIND:
        _drain_heartbeat_stamp(row, archive_db)
        return
    if row.kind == RUNTIME_EVENT_KIND:
        _drain_runtime_event(row, archive_db)
        return
    raise ValueError(f"Unsupported live archive outbox kind: {row.kind}")


def _drain_heartbeat_stamp(row: LiveArchiveOutbox, archive_db: Session) -> None:
    payload = json.loads(row.payload_json or "{}")
    heartbeat = _restore_jsonable(payload.get("heartbeat") or {})
    device_id = str(heartbeat.get("device_id") or "").strip()
    received_at = normalize_utc(heartbeat.get("received_at"))
    if not device_id or received_at is None:
        raise ValueError("heartbeat outbox payload is missing device_id or received_at")

    exists = (
        archive_db.query(AgentHeartbeat.id)
        .filter(
            AgentHeartbeat.device_id == device_id,
            AgentHeartbeat.received_at == received_at,
        )
        .first()
    )
    if exists is not None:
        return
    archive_payload = {**heartbeat, "received_at": received_at}
    archive_db.add(AgentHeartbeat(**archive_payload))


def _drain_runtime_event(row: LiveArchiveOutbox, archive_db: Session) -> None:
    payload = json.loads(row.payload_json or "{}")
    event_payload = _restore_jsonable(payload.get("event") or {})
    event = RuntimeEventIngest.model_validate(event_payload)
    ingest_runtime_events(archive_db, [event])


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        normalized = normalize_utc(value) or value
        return {"__longhouse_datetime__": normalized.isoformat()}
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _restore_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        marker = value.get("__longhouse_datetime__")
        if marker is not None:
            return datetime.fromisoformat(str(marker))
        return {str(key): _restore_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_restore_jsonable(item) for item in value]
    return value
