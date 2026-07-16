"""Synchronous catalog operations executed on catalogd's dedicated DB thread."""

from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL
from uuid import UUID
from uuid import uuid4
from uuid import uuid5

from sqlalchemy import Engine
from sqlalchemy import and_
from sqlalchemy import case
from sqlalchemy import delete
from sqlalchemy import func
from sqlalchemy import insert
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import tuple_
from sqlalchemy import union_all
from sqlalchemy import update
from sqlalchemy.orm import Session

from zerg.catalogd.models import LegacyMigrationRun
from zerg.catalogd.models import LegacyMigrationSession
from zerg.catalogd.models import MediaObject
from zerg.catalogd.models import ProjectorState
from zerg.catalogd.models import ProjectorStoreBinding
from zerg.catalogd.models import RawObject as LiveRawObject
from zerg.catalogd.models import RenderGeneration
from zerg.catalogd.models import RenderObject
from zerg.catalogd.models import SessionMediaRef
from zerg.catalogd.models import SessionTombstone as LiveSessionTombstone
from zerg.catalogd.models import SourceEpoch as LiveSourceEpoch
from zerg.catalogd.models import StorageSession
from zerg.catalogd.schema import catalog_meta
from zerg.models.live_store import LiveAPNSDeviceRegistration
from zerg.models.live_store import LiveAPNSLiveActivityRegistration
from zerg.models.live_store import LiveArchiveOutbox
from zerg.models.live_store import LiveConsoleTurn
from zerg.models.live_store import LiveControlLease
from zerg.models.live_store import LiveDeviceToken
from zerg.models.live_store import LiveHeartbeatStamp
from zerg.models.live_store import LiveInteractionRequest
from zerg.models.live_store import LiveLaunchReadiness
from zerg.models.live_store import LiveMachineControlOperation
from zerg.models.live_store import LiveMachinePresence
from zerg.models.live_store import LiveNotificationClientPresence
from zerg.models.live_store import LiveRefreshSession
from zerg.models.live_store import LiveRuntimeState
from zerg.models.live_store import LiveSession
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionInputAttachment
from zerg.models.live_store import LiveSessionInputReceipt
from zerg.models.live_store import LiveSessionLaunchAttempt
from zerg.models.live_store import LiveSessionLivePreview
from zerg.models.live_store import LiveSessionMessage
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.models.live_store import LiveSessionThreadAlias
from zerg.models.live_store import LiveTimelineCard
from zerg.models.live_store import LiveUser
from zerg.services.session_title import sanitize_title
from zerg.storage_v2.contracts import DurableReceipt
from zerg.storage_v2.contracts import EnvelopeIdentity
from zerg.storage_v2.contracts import envelope_id as compute_envelope_id

DEVICE_TOKEN_LIMIT_PER_OWNER = 1_000
SESSION_READ_LIMIT = 100
MACHINE_ENROLLMENT_LIMIT = 1_000
WORKSPACE_CANDIDATE_LIMIT = 5_000
# The capability projector consumes only its highest-ranked connection.  The
# ordering below is deliberately identical to that projector, so returning the
# winner preserves semantics while keeping a 100-row page bounded.
SESSION_CONNECTION_LIMIT = 1
_CONTROL_LEASE_TTL = timedelta(minutes=15)
_EXCLUDED_WORKSPACE_ENVIRONMENTS = ("test", "e2e")
_RECENCY_BUCKETS: tuple[tuple[float, int], ...] = (
    (1.0, 100),
    (4.0, 70),
    (14.0, 50),
    (31.0, 30),
)


def _json_launch_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        **result,
        "session_id": str(result["session_id"]),
    }


def _live_console_turn_dto(
    turn: LiveConsoleTurn,
    *,
    message: str | None = None,
    client_request_id: str | None = None,
    provider_config: str | None = None,
) -> dict[str, Any]:
    return {
        "turn_id": turn.id,
        "session_id": turn.session_id,
        "thread_id": turn.thread_id,
        "run_id": turn.run_id,
        "state": turn.state,
        "provider": turn.provider,
        "device_id": turn.device_id,
        "cwd": turn.cwd,
        "message": message,
        "client_request_id": client_request_id,
        "provider_config": json.loads(provider_config or "{}"),
        "resume_provider_thread_id": turn.resume_provider_thread_id,
        "error": turn.error,
    }


def _canonical_outbox_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return {"__longhouse_datetime__": (_as_aware_utc(value) or value).isoformat()}
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _canonical_outbox_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonical_outbox_value(item) for item in value]
    return value


def _launch_view_dto(view: Any) -> dict[str, Any]:
    return {
        "session_id": str(view.session_id),
        "launch_state": view.launch_state,
        "execution_lifetime": view.execution_lifetime,
        "launch_error_code": view.launch_error_code,
        "launch_error_message": view.launch_error_message,
        "owner_id": view.owner_id,
        "provider": view.provider,
        "device_id": view.device_id,
        "machine_id": view.machine_id,
        "project": view.project,
        "created_at": _encode_datetime(view.created_at),
        "updated_at": _encode_datetime(view.updated_at),
    }


def _interaction_id(request_key: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"longhouse-pause:{request_key}"))


def _interaction_dto(row: LiveInteractionRequest) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "session_id": str(row.session_id),
        "runtime_key": str(row.runtime_key),
        "provider": str(row.provider),
        "request_key": str(row.request_key),
        "provider_request_id": row.provider_request_id,
        "source": row.source,
        "reply_transport": row.reply_transport,
        "kind": str(row.kind),
        "status": str(row.status),
        "can_respond": bool(row.can_respond),
        "projection": _bounded_pause_projection(row.projection_json, compact=False),
        "response_payload": row.response_payload_json if isinstance(row.response_payload_json, dict) else None,
        "response_text": row.response_text,
        "occurred_at": _encode_datetime(row.occurred_at),
        "last_seen_at": _encode_datetime(row.last_seen_at),
        "resolved_at": _encode_datetime(row.resolved_at),
        "expires_at": _encode_datetime(row.expires_at),
    }


def _runtime_interaction_dto(runtime: LiveRuntimeState) -> dict[str, Any] | None:
    projection = _bounded_pause_projection(runtime.pending_interaction_projection_json, compact=False)
    request_key = str(runtime.pending_interaction_id or "").strip()
    if projection is None or not request_key:
        return None
    interaction_id = str(projection.get("id") or _interaction_id(request_key))
    provider = str(runtime.provider)
    kind = str(runtime.pending_interaction_kind or projection.get("kind") or "structured_question")
    request_prefix = f"{provider}:{runtime.runtime_key}:"
    provider_request_id = request_key[len(request_prefix) :] if request_key.startswith(request_prefix) else None
    source = None
    reply_transport = None
    if kind == "permission_prompt" and provider == "claude":
        source = "claude_permission_gate"
        reply_transport = "claude_pretooluse_pull"
    elif kind == "permission_prompt" and provider == "opencode":
        source = "opencode_bridge"
        reply_transport = "managed_push"
    return {
        "id": interaction_id,
        "session_id": str(runtime.session_id),
        "runtime_key": str(runtime.runtime_key),
        "provider": provider,
        "request_key": request_key,
        "provider_request_id": provider_request_id,
        "source": source,
        "reply_transport": reply_transport,
        "kind": kind,
        "status": "pending",
        "can_respond": bool(runtime.pending_interaction_can_respond),
        "projection": projection,
        "response_payload": None,
        "response_text": None,
        "occurred_at": _encode_datetime(runtime.pending_interaction_opened_at),
        "last_seen_at": _encode_datetime(runtime.pending_interaction_updated_at or runtime.updated_at),
        "resolved_at": None,
        "expires_at": projection.get("expires_at"),
    }


def _apply_live_interaction_event(db: Session, event: Any) -> LiveInteractionRequest | None:
    """Mirror one accepted runtime interaction event into bounded control facts."""

    from zerg.services.session_pause_requests import build_pause_runtime_projection
    from zerg.services.session_pause_requests import pause_runtime_request_key

    payload = event.payload if isinstance(event.payload, dict) else {}
    request_key = pause_runtime_request_key(event)
    observed_at = _as_aware_utc(event.occurred_at) or datetime.now(UTC)
    if event.kind == "pause_resolution":
        row = db.query(LiveInteractionRequest).filter(LiveInteractionRequest.request_key == request_key).one_or_none()
        if row is None or row.status != "pending":
            return row
        if (_as_aware_utc(row.last_seen_at) or observed_at) > observed_at:
            return row
        terminal_status = str(payload.get("status") or "resolved")
        row.status = terminal_status if terminal_status in {"resolved", "rejected", "failed", "expired"} else "resolved"
        row.can_respond = 0
        row.response_payload_json = dict(payload.get("response_payload") or payload.get("response_payload_json") or {}) or None
        row.response_text = str(payload.get("response_text") or payload.get("message") or "").strip() or None
        row.resolved_at = observed_at
        row.last_seen_at = observed_at
        row.updated_at = observed_at
        db.add(row)
        return row
    if event.kind != "pause_request" or event.session_id is None:
        return None
    provider_ref = payload.get("provider_ref") or payload.get("provider_ref_json") or {}
    provider_ref = provider_ref if isinstance(provider_ref, dict) else {}
    row = db.query(LiveInteractionRequest).filter(LiveInteractionRequest.request_key == request_key).one_or_none()
    if row is not None:
        last_seen_at = _as_aware_utc(row.last_seen_at)
        resolved_at = _as_aware_utc(row.resolved_at)
        if (last_seen_at is not None and observed_at < last_seen_at) or (row.status == "pending" and last_seen_at == observed_at):
            return row
        if row.status != "pending" and resolved_at is not None and observed_at <= resolved_at:
            return row
    if bool(payload.get("single_active", True)):
        db.query(LiveInteractionRequest).filter(
            LiveInteractionRequest.runtime_key == str(event.runtime_key),
            LiveInteractionRequest.request_key != request_key,
            LiveInteractionRequest.status == "pending",
        ).update(
            {
                "status": "expired",
                "can_respond": 0,
                "last_seen_at": observed_at,
                "resolved_at": observed_at,
                "updated_at": observed_at,
            },
            synchronize_session=False,
        )
    projection = build_pause_runtime_projection(event)
    if row is None:
        row = LiveInteractionRequest(
            id=_interaction_id(request_key),
            request_key=request_key,
            created_at=observed_at,
        )
    row.session_id = str(event.session_id)
    row.runtime_key = str(event.runtime_key)
    row.provider = str(event.provider or "unknown")
    row.provider_request_id = str(payload.get("provider_request_id") or payload.get("request_id") or "").strip() or None
    row.source = str(provider_ref.get("source") or "").strip() or None
    row.reply_transport = str(provider_ref.get("reply_transport") or "").strip() or None
    row.kind = str(payload.get("kind") or "structured_question")
    row.status = "pending"
    row.can_respond = int(bool(payload.get("can_respond")))
    request_payload = payload.get("request_payload") or payload.get("request_payload_json") or payload.get("payload") or {}
    row.request_payload_json = dict(request_payload) if isinstance(request_payload, dict) else {}
    row.projection_json = projection
    row.response_payload_json = None
    row.response_text = None
    row.occurred_at = observed_at
    row.last_seen_at = observed_at
    row.resolved_at = None
    expires_at = projection.get("expires_at")
    row.expires_at = _as_aware_utc(datetime.fromisoformat(expires_at)) if isinstance(expires_at, str) else None
    row.updated_at = observed_at
    db.add(row)
    return row


def _live_control_session_dto(session: Any) -> dict[str, Any]:
    return {
        "id": str(session.id),
        "provider": session.provider,
        "device_id": session.device_id,
        "device_name": session.device_name,
        "cwd": session.cwd,
        "project": session.project,
        "git_repo": session.git_repo,
        "git_branch": session.git_branch,
        "ended_at": _encode_datetime(session.ended_at),
        "closed_at": _encode_datetime(session.closed_at),
        "close_reason": session.close_reason,
        "loop_mode": session.loop_mode,
        "permission_mode": session.permission_mode,
        "primary_thread_id": str(session.primary_thread_id) if session.primary_thread_id else None,
    }


def _input_receipt_dto(receipt: Any) -> dict[str, Any]:
    return {
        "id": receipt.id,
        "owner_id": receipt.owner_id,
        "session_id": receipt.session_id,
        "provider": receipt.provider,
        "text": receipt.text,
        "intent": receipt.intent,
        "status": receipt.status,
        "client_request_id": receipt.client_request_id,
        "archive_session_input_id": receipt.archive_session_input_id,
        "delivery_request_id": receipt.delivery_request_id,
        "error_json": receipt.error_json,
        "created_at": _encode_datetime(receipt.created_at),
        "updated_at": _encode_datetime(receipt.updated_at),
    }


def _input_attachment_dto(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "input_receipt_id": str(row.input_receipt_id),
        "owner_id": int(row.owner_id),
        "session_id": str(row.session_id),
        "mime_type": str(row.mime_type),
        "byte_size": int(row.byte_size),
        "sha256": str(row.sha256),
        "blob_path": str(row.blob_path),
        "original_filename": row.original_filename,
        "original_byte_size": int(row.original_byte_size) if row.original_byte_size is not None else None,
        "created_at": _encode_datetime(row.created_at),
        "expires_at": _encode_datetime(row.expires_at),
    }


def _session_message_dto(row: Any) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "from_session_id": str(row.from_session_id),
        "to_session_id": str(row.to_session_id),
        "text": str(getattr(row, "body", row.text)),
        "source_event_id": row.source_event_id,
        "delivery_status": str(row.delivery_status),
        "delivery_attempts": int(row.delivery_attempts or 0),
        "last_error": row.last_error,
        "delivered_via": row.delivered_via,
        "created_at": _encode_datetime(row.created_at),
        "delivered_at": _encode_datetime(row.delivered_at),
        "acknowledged_at": _encode_datetime(row.acknowledged_at),
    }


def _machine_operation_dto(operation: LiveMachineControlOperation) -> dict[str, Any]:
    from zerg.services.machine_control_operations import machine_control_operation_to_response

    payload = machine_control_operation_to_response(operation)
    for field in ("created_at", "started_at", "finished_at"):
        payload[field] = _encode_datetime(payload[field])
    return payload


class CatalogStore:
    def retire_archive_outbox(self) -> dict[str, int | str]:
        """Remove dead monolith projections and retain launch rows only as completed receipts."""

        table = LiveArchiveOutbox.__table__
        observed_at = datetime.now(UTC)
        obsolete_kinds = ("heartbeat_stamp.v1", "runtime_event.v1", "session_input_receipt.v1")
        launch_kinds = ("managed_local_launch.v1", "remote_launch.v1", "remote_launch_outcome.v1")
        with _write_transaction(self.engine) as connection:
            deleted = connection.execute(delete(table).where(table.c.kind.in_(obsolete_kinds))).rowcount or 0
            completed = (
                connection.execute(
                    update(table)
                    .where(table.c.kind.in_(launch_kinds), table.c.drained_at.is_(None))
                    .values(drained_at=table.c.created_at, last_error=None)
                ).rowcount
                or 0
            )
            pruned = (
                connection.execute(
                    delete(table).where(
                        table.c.kind.in_(launch_kinds),
                        table.c.drained_at.isnot(None),
                        table.c.drained_at < observed_at - timedelta(days=30),
                    )
                ).rowcount
                or 0
            )
            commit_seq = _advance_commit_seq(connection, observed_at) if deleted or completed or pruned else _current_commit_seq(connection)
            return {
                "deleted": int(deleted),
                "completed": int(completed),
                "pruned": int(pruned),
                "commit_seq": str(commit_seq),
            }

    """Small product operations over the bounded catalog.

    Methods are deliberately synchronous: the daemon invokes mutations on one
    executor and explicitly read-only operations on a separate bounded pool,
    keeping SQLite work off the asyncio socket loop while WAL readers remain
    available during background writes.
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def authenticate_device(self, *, token_hash: str) -> dict[str, Any]:
        """Validate one machine credential without turning auth into a write."""

        token_table = LiveDeviceToken.__table__
        with _read_snapshot(self.engine) as connection:
            row = connection.execute(select(token_table).where(token_table.c.token_hash == token_hash)).mappings().first()
            if row is None:
                hmac.compare_digest(token_hash, "0" * 64)
                return {"valid": False, "commit_seq": str(_current_commit_seq(connection))}
            if not hmac.compare_digest(token_hash, str(row["token_hash"])) or row["revoked_at"] is not None:
                return {"valid": False, "commit_seq": str(_current_commit_seq(connection))}

            commit_seq = _current_commit_seq(connection)
            return {
                "valid": True,
                "commit_seq": str(commit_seq),
                "token": {
                    "id": str(row["id"]),
                    "owner_id": row["owner_id"],
                    "device_id": row["device_id"],
                    "created_at": _encode_datetime(row["created_at"]),
                    "last_used_at": _encode_datetime(row["last_used_at"]),
                    "revoked_at": None,
                },
            }

    def get_user(self, *, user_id: int, touch_last_login: bool) -> dict[str, Any]:
        """Resolve one user, optionally recording their first successful login."""

        user_table = LiveUser.__table__
        now = datetime.now(UTC)
        with _write_transaction(self.engine) if touch_last_login else _read_snapshot(self.engine) as connection:
            row = connection.execute(select(user_table).where(user_table.c.id == user_id)).mappings().first()
            if row is None:
                return {"found": False, "changed": False, "commit_seq": str(_current_commit_seq(connection))}
            changed = touch_last_login and row["last_login"] is None
            if changed:
                connection.execute(update(user_table).where(user_table.c.id == user_id).values(last_login=now, updated_at=now))
                commit_seq = _advance_commit_seq(connection, now)
                row = connection.execute(select(user_table).where(user_table.c.id == user_id)).mappings().one()
            else:
                commit_seq = _current_commit_seq(connection)
            return {
                "found": True,
                "changed": changed,
                "user": _user_dto(row),
                "commit_seq": str(commit_seq),
            }

    def get_active_owner(self) -> dict[str, Any]:
        """Resolve the single-tenant owner without exposing a SQLite reader."""

        user_table = LiveUser.__table__
        with _read_snapshot(self.engine) as connection:
            owner_id = connection.execute(
                select(user_table.c.id).where(user_table.c.is_active.is_(True)).order_by(user_table.c.id.asc()).limit(1)
            ).scalar_one_or_none()
            return {
                "found": owner_id is not None,
                "owner_id": int(owner_id) if owner_id is not None else None,
                "commit_seq": str(_current_commit_seq(connection)),
            }

    def ensure_single_tenant_owner(
        self,
        *,
        email: str,
        provider: str,
        provider_user_id: str | None,
    ) -> dict[str, Any]:
        user_table = LiveUser.__table__
        now = datetime.now(UTC)
        with _write_transaction(self.engine) as connection:
            owners = (
                connection.execute(
                    select(user_table)
                    .where(or_(user_table.c.provider != "service", user_table.c.provider.is_(None)))
                    .order_by(user_table.c.id.asc())
                    .limit(2)
                )
                .mappings()
                .all()
            )
            if len(owners) > 1:
                return {"conflict": "multiple_owners", "commit_seq": str(_current_commit_seq(connection))}
            if owners:
                owner = owners[0]
                if str(owner["email"]).casefold() != email.casefold():
                    return {"conflict": "owner_email_mismatch", "commit_seq": str(_current_commit_seq(connection))}
                if owner["role"] != "ADMIN":
                    connection.execute(update(user_table).where(user_table.c.id == owner["id"]).values(role="ADMIN", updated_at=now))
                    commit_seq = _advance_commit_seq(connection, now)
                    owner = connection.execute(select(user_table).where(user_table.c.id == owner["id"])).mappings().one()
                else:
                    commit_seq = _current_commit_seq(connection)
                return {
                    "created": False,
                    "user": _user_dto(owner),
                    "commit_seq": str(commit_seq),
                }
            user_id = connection.execute(
                insert(user_table)
                .values(
                    provider=provider,
                    provider_user_id=provider_user_id,
                    email=email,
                    email_verified=True,
                    is_active=True,
                    role="ADMIN",
                    prefs={},
                    context={},
                    created_at=now,
                    updated_at=now,
                )
                .returning(user_table.c.id)
            ).scalar_one()
            commit_seq = _advance_commit_seq(connection, now)
            owner = connection.execute(select(user_table).where(user_table.c.id == user_id)).mappings().one()
            return {
                "created": True,
                "user": _user_dto(owner),
                "commit_seq": str(commit_seq),
            }

    def upsert_notification_presence(
        self,
        *,
        owner_id: int,
        client_id: str,
        client_type: str,
        visible: bool,
        route: str | None,
        session_id: str | None,
        observed_at: datetime,
    ) -> dict[str, Any]:
        table = LiveNotificationClientPresence.__table__
        with _write_transaction(self.engine) as connection:
            row = connection.execute(select(table).where(table.c.owner_id == owner_id, table.c.client_id == client_id)).mappings().first()
            if row is not None:
                durable_observed_at = _as_aware_utc(row["last_seen_at"]) or observed_at
                same_payload = (
                    str(row["client_type"]) == client_type
                    and bool(row["visible"]) == visible
                    and row["route"] == route
                    and row["session_id"] == session_id
                )
                if observed_at == durable_observed_at and not same_payload:
                    return {
                        "idempotency_conflict": True,
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                if observed_at <= durable_observed_at:
                    return {
                        "idempotency_conflict": False,
                        "stale": observed_at < durable_observed_at,
                        "presence": {
                            "client_id": str(row["client_id"]),
                            "client_type": str(row["client_type"]),
                            "visible": bool(row["visible"]),
                            "route": row["route"],
                            "session_id": row["session_id"],
                            "last_seen_at": durable_observed_at.isoformat(),
                        },
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
            values = {
                "client_type": client_type,
                "visible": visible,
                "route": route,
                "session_id": session_id,
                "last_seen_at": observed_at,
                "updated_at": observed_at,
            }
            if row is None:
                connection.execute(
                    insert(table).values(
                        owner_id=owner_id,
                        client_id=client_id,
                        created_at=observed_at,
                        **values,
                    )
                )
            else:
                connection.execute(update(table).where(table.c.owner_id == owner_id, table.c.client_id == client_id).values(**values))
            commit_seq = _advance_commit_seq(connection, observed_at)
            return {
                "idempotency_conflict": False,
                "stale": False,
                "presence": {
                    "client_id": client_id,
                    "client_type": client_type,
                    "visible": visible,
                    "route": route,
                    "session_id": session_id,
                    "last_seen_at": observed_at.isoformat(),
                },
                "commit_seq": str(commit_seq),
            }

    def recent_visible_web_presence(self, *, owner_id: int, threshold: datetime) -> dict[str, Any]:
        table = LiveNotificationClientPresence.__table__
        with _read_snapshot(self.engine) as connection:
            found = connection.execute(
                select(table.c.id)
                .where(
                    table.c.owner_id == owner_id,
                    table.c.client_type == "web",
                    table.c.visible.is_(True),
                    table.c.last_seen_at >= threshold,
                )
                .limit(1)
            ).first()
            return {
                "visible": found is not None,
                "commit_seq": str(_current_commit_seq(connection)),
            }

    def read_machine_presence_policy(self, *, owner_id: int) -> dict[str, Any]:
        user_table = LiveUser.__table__
        with _read_snapshot(self.engine) as connection:
            prefs = connection.execute(select(user_table.c.prefs).where(user_table.c.id == owner_id)).scalar_one_or_none()
            decoded = _decode_json_object(prefs) if prefs is not None else {}
            enabled = decoded.get("machine_presence_enabled")
            return {
                "found": prefs is not None,
                "enabled": enabled if isinstance(enabled, bool) else True,
                "commit_seq": str(_current_commit_seq(connection)),
            }

    def upsert_machine_presence(
        self,
        *,
        owner_id: int,
        device_id: str,
        state: str,
        source: str,
        idle_seconds: int | None,
        measured_at: datetime,
        received_at: datetime,
    ) -> dict[str, Any]:
        table = LiveMachinePresence.__table__
        values = {
            "state": state,
            "source": source,
            "idle_seconds": idle_seconds,
            "measured_at": measured_at,
            "received_at": received_at,
            "updated_at": received_at,
        }
        with _write_transaction(self.engine) as connection:
            row = connection.execute(select(table.c.id).where(table.c.owner_id == owner_id, table.c.device_id == device_id)).first()
            if row is None:
                connection.execute(
                    insert(table).values(
                        owner_id=owner_id,
                        device_id=device_id,
                        created_at=received_at,
                        **values,
                    )
                )
            else:
                connection.execute(update(table).where(table.c.owner_id == owner_id, table.c.device_id == device_id).values(**values))
            commit_seq = _advance_commit_seq(connection, received_at)
            return {
                "presence": {
                    "owner_id": owner_id,
                    "device_id": device_id,
                    "state": state,
                    "source": source,
                    "idle_seconds": idle_seconds,
                    "measured_at": measured_at.isoformat(),
                    "received_at": received_at.isoformat(),
                },
                "commit_seq": str(commit_seq),
            }

    def upsert_apns_device(
        self,
        *,
        registration_id: str,
        owner_id: int,
        platform: str,
        device_token: str,
        push_environment: str,
        app_build_id: str | None,
        observed_at: datetime,
    ) -> dict[str, Any]:
        table = LiveAPNSDeviceRegistration.__table__
        with _write_transaction(self.engine) as connection:
            row = (
                connection.execute(select(table).where(table.c.owner_id == owner_id, table.c.device_token == device_token))
                .mappings()
                .first()
            )
            stored_id = str(row["id"]) if row is not None else registration_id
            values = {
                "platform": platform,
                "push_environment": push_environment,
                "app_build_id": app_build_id,
                "last_seen_at": observed_at,
                "updated_at": observed_at,
                "revoked_at": None,
            }
            if row is None:
                connection.execute(
                    insert(table).values(
                        id=stored_id,
                        owner_id=owner_id,
                        device_token=device_token,
                        created_at=observed_at,
                        **values,
                    )
                )
            else:
                connection.execute(update(table).where(table.c.id == stored_id).values(**values))
            commit_seq = _advance_commit_seq(connection, observed_at)
            return {
                "registration": {
                    "id": stored_id,
                    "platform": platform,
                    "push_environment": push_environment,
                    "app_build_id": app_build_id,
                    "last_seen_at": observed_at.isoformat(),
                },
                "commit_seq": str(commit_seq),
            }

    def upsert_apns_live_activity(
        self,
        *,
        registration_id: str,
        owner_id: int,
        session_id: str,
        activity_id: str,
        push_token: str,
        push_environment: str,
        app_build_id: str | None,
        observed_at: datetime,
    ) -> dict[str, Any]:
        table = LiveAPNSLiveActivityRegistration.__table__
        with _write_transaction(self.engine) as connection:
            row = (
                connection.execute(select(table).where(table.c.owner_id == owner_id, table.c.activity_id == activity_id)).mappings().first()
            )
            if row is None:
                row = (
                    connection.execute(select(table).where(table.c.owner_id == owner_id, table.c.push_token == push_token))
                    .mappings()
                    .first()
                )
            stored_id = str(row["id"]) if row is not None else registration_id
            values = {
                "session_id": session_id,
                "activity_id": activity_id,
                "push_token": push_token,
                "push_environment": push_environment,
                "app_build_id": app_build_id,
                "last_seen_at": observed_at,
                "updated_at": observed_at,
                "ended_at": None,
            }
            if row is None:
                connection.execute(
                    insert(table).values(
                        id=stored_id,
                        owner_id=owner_id,
                        created_at=observed_at,
                        **values,
                    )
                )
            else:
                connection.execute(update(table).where(table.c.id == stored_id).values(**values))
            commit_seq = _advance_commit_seq(connection, observed_at)
            return {
                "registration": {
                    "id": stored_id,
                    "session_id": session_id,
                    "activity_id": activity_id,
                    "push_environment": push_environment,
                    "app_build_id": app_build_id,
                    "last_seen_at": observed_at.isoformat(),
                },
                "commit_seq": str(commit_seq),
            }

    def end_apns_live_activity(self, *, owner_id: int, activity_id: str, ended_at: datetime) -> dict[str, Any]:
        table = LiveAPNSLiveActivityRegistration.__table__
        with _write_transaction(self.engine) as connection:
            count = connection.execute(
                update(table)
                .where(table.c.owner_id == owner_id, table.c.activity_id == activity_id, table.c.ended_at.is_(None))
                .values(ended_at=ended_at, updated_at=ended_at)
            ).rowcount
            commit_seq = _advance_commit_seq(connection, ended_at) if count else _current_commit_seq(connection)
            return {"found": bool(count), "commit_seq": str(commit_seq)}

    def resolve_device(
        self,
        *,
        token_hash: str,
        touch_last_used: bool,
        touch_interval_seconds: int,
    ) -> dict[str, Any]:
        """Resolve an active machine credential and its active owner atomically."""

        token_table = LiveDeviceToken.__table__
        user_table = LiveUser.__table__
        now = datetime.now(UTC)
        context = _write_transaction(self.engine) if touch_last_used else _read_snapshot(self.engine)
        with context as connection:
            row = (
                connection.execute(
                    select(
                        user_table,
                        token_table.c.id.label("device_token_id"),
                        token_table.c.owner_id.label("device_owner_id"),
                        token_table.c.device_id.label("device_id"),
                        token_table.c.token_hash.label("device_token_hash"),
                        token_table.c.created_at.label("device_created_at"),
                        token_table.c.last_used_at.label("device_last_used_at"),
                    )
                    .select_from(token_table.join(user_table, token_table.c.owner_id == user_table.c.id))
                    .where(
                        token_table.c.token_hash == token_hash,
                        token_table.c.revoked_at.is_(None),
                        user_table.c.is_active.is_(True),
                    )
                )
                .mappings()
                .first()
            )
            if row is None or not hmac.compare_digest(token_hash, str(row["device_token_hash"])):
                hmac.compare_digest(token_hash, "0" * 64)
                return {"valid": False, "changed": False, "commit_seq": str(_current_commit_seq(connection))}

            last_used_at = _as_aware_utc(row["device_last_used_at"])
            changed = bool(touch_last_used and (last_used_at is None or (now - last_used_at).total_seconds() >= touch_interval_seconds))
            if changed:
                connection.execute(update(token_table).where(token_table.c.id == row["device_token_id"]).values(last_used_at=now))
                commit_seq = _advance_commit_seq(connection, now)
                last_used_at = now
            else:
                commit_seq = _current_commit_seq(connection)
            return {
                "valid": True,
                "changed": changed,
                "token": {
                    "id": str(row["device_token_id"]),
                    "owner_id": row["device_owner_id"],
                    "device_id": row["device_id"],
                    "created_at": _encode_datetime(row["device_created_at"]),
                    "last_used_at": _encode_datetime(last_used_at),
                    "revoked_at": None,
                },
                "user": _user_dto(row),
                "commit_seq": str(commit_seq),
            }

    def resolve_cp_user(
        self,
        *,
        cp_user_id: int,
        email: str,
        email_verified: bool,
        display_name: str | None,
        avatar_url: str | None,
    ) -> dict[str, Any]:
        """Resolve/link one control-plane identity using the established conflict rules."""

        user_table = LiveUser.__table__
        now = datetime.now(UTC)
        with _write_transaction(self.engine) as connection:
            user = connection.execute(select(user_table).where(user_table.c.cp_user_id == cp_user_id)).mappings().first()
            changed = False
            if user is None:
                existing = connection.execute(select(user_table).where(user_table.c.email == email)).mappings().first()
                if existing is not None:
                    if not email_verified:
                        return {
                            "conflict": "email_unverified_link",
                            "commit_seq": str(_current_commit_seq(connection)),
                        }
                    if existing["cp_user_id"] not in (None, cp_user_id):
                        return {
                            "conflict": "account_link_conflict",
                            "commit_seq": str(_current_commit_seq(connection)),
                        }
                    user = existing
                else:
                    user_id = connection.execute(
                        insert(user_table)
                        .values(
                            provider="control-plane",
                            provider_user_id=f"cp:{cp_user_id}",
                            email=email,
                            cp_user_id=cp_user_id,
                            email_verified=email_verified,
                            is_active=True,
                            role="USER",
                            display_name=display_name,
                            avatar_url=avatar_url,
                            prefs={},
                            context={},
                            last_login=now,
                            created_at=now,
                            updated_at=now,
                        )
                        .returning(user_table.c.id)
                    ).scalar_one()
                    user = connection.execute(select(user_table).where(user_table.c.id == user_id)).mappings().one()
                    changed = True

            values: dict[str, Any] = {}
            if user["cp_user_id"] != cp_user_id:
                values["cp_user_id"] = cp_user_id
            if user["provider"] != "control-plane":
                values["provider"] = "control-plane"
            provider_user_id = f"cp:{cp_user_id}"
            if user["provider_user_id"] != provider_user_id:
                values["provider_user_id"] = provider_user_id
            if user["email"] != email:
                collision = connection.execute(
                    select(user_table.c.id).where(user_table.c.email == email, user_table.c.id != user["id"])
                ).first()
                if collision is None:
                    values["email"] = email
            desired_display_name = display_name or user["display_name"]
            desired_avatar_url = avatar_url or user["avatar_url"]
            if user["display_name"] != desired_display_name:
                values["display_name"] = desired_display_name
            if user["avatar_url"] != desired_avatar_url:
                values["avatar_url"] = desired_avatar_url
            if user["email_verified"] != email_verified:
                values["email_verified"] = email_verified
            if user["is_active"] is not True:
                values["is_active"] = True
            if user["last_login"] is None:
                values["last_login"] = now
            if values:
                values["updated_at"] = now
                connection.execute(update(user_table).where(user_table.c.id == user["id"]).values(**values))
                changed = True
            if changed:
                commit_seq = _advance_commit_seq(connection, now)
                user = connection.execute(select(user_table).where(user_table.c.id == user["id"])).mappings().one()
            else:
                commit_seq = _current_commit_seq(connection)
            return {"changed": changed, "user": _user_dto(user), "commit_seq": str(commit_seq)}

    def resolve_local_user(
        self,
        *,
        email: str,
        provider: str,
        provider_user_id: str | None,
        role: str,
        adopt_existing: bool,
        require_email_match: bool,
        max_users: int | None,
        promote_role: bool,
    ) -> dict[str, Any]:
        """Resolve, create, or explicitly adopt a self-hosted local owner."""

        user_table = LiveUser.__table__
        now = datetime.now(UTC)
        with _write_transaction(self.engine) as connection:
            user = connection.execute(select(user_table).where(user_table.c.email == email)).mappings().first()
            adopted = False
            if user is None:
                existing = (
                    connection.execute(
                        select(user_table)
                        .where(or_(user_table.c.provider != "service", user_table.c.provider.is_(None)))
                        .order_by(user_table.c.id.asc())
                        .limit(1)
                    )
                    .mappings()
                    .first()
                )
                if existing is not None and require_email_match:
                    return {"conflict": "owner_email_mismatch", "commit_seq": str(_current_commit_seq(connection))}
                if existing is not None and adopt_existing:
                    user = existing
                    adopted = True
                else:
                    if max_users is not None:
                        count = connection.execute(select(func.count()).select_from(user_table)).scalar_one()
                        if count >= max_users:
                            return {
                                "conflict": "user_limit_reached",
                                "commit_seq": str(_current_commit_seq(connection)),
                            }
                    user_id = connection.execute(
                        insert(user_table)
                        .values(
                            provider=provider,
                            provider_user_id=provider_user_id,
                            email=email,
                            email_verified=True,
                            is_active=True,
                            role=role,
                            prefs={},
                            context={},
                            created_at=now,
                            updated_at=now,
                        )
                        .returning(user_table.c.id)
                    ).scalar_one()
                    user = connection.execute(select(user_table).where(user_table.c.id == user_id)).mappings().one()
                    commit_seq = _advance_commit_seq(connection, now)
                    return {
                        "created": True,
                        "adopted": False,
                        "changed": True,
                        "user": _user_dto(user),
                        "commit_seq": str(commit_seq),
                    }
            changed = False
            if promote_role and user["role"] != role:
                connection.execute(update(user_table).where(user_table.c.id == user["id"]).values(role=role, updated_at=now))
                changed = True
            commit_seq = _advance_commit_seq(connection, now) if changed else _current_commit_seq(connection)
            if changed:
                user = connection.execute(select(user_table).where(user_table.c.id == user["id"])).mappings().one()
            return {
                "created": False,
                "adopted": adopted,
                "changed": changed,
                "user": _user_dto(user),
                "commit_seq": str(commit_seq),
            }

    def create_refresh_session(
        self,
        *,
        user_id: int,
        token_hash: str,
        family_id: str,
        parent_id: int | None,
        created_at: datetime,
        absolute_expires_at: datetime,
        idle_expires_at: datetime,
    ) -> dict[str, Any]:
        """Create one refresh lineage row with exact replay by token hash."""

        table = LiveRefreshSession.__table__
        now = datetime.now(UTC)
        with _write_transaction(self.engine) as connection:
            existing = connection.execute(select(table).where(table.c.token_hash == token_hash)).mappings().first()
            if existing is not None:
                exact = (
                    existing["user_id"] == user_id
                    and existing["family_id"] == family_id
                    and existing["parent_id"] == parent_id
                    and _as_aware_utc(existing["created_at"]) == created_at
                    and _as_aware_utc(existing["absolute_expires_at"]) == absolute_expires_at
                    and _as_aware_utc(existing["idle_expires_at"]) == idle_expires_at
                )
                return {
                    "created": False,
                    "exact_replay": exact,
                    "session_id": existing["id"],
                    "family_id": existing["family_id"],
                    "commit_seq": str(_current_commit_seq(connection)),
                }
            user_exists = connection.execute(select(LiveUser.id).where(LiveUser.id == user_id)).first()
            if user_exists is None:
                return {"not_found": "user", "commit_seq": str(_current_commit_seq(connection))}
            session_id = connection.execute(
                insert(table)
                .values(
                    token_hash=token_hash,
                    user_id=user_id,
                    family_id=family_id,
                    parent_id=parent_id,
                    created_at=created_at,
                    absolute_expires_at=absolute_expires_at,
                    idle_expires_at=idle_expires_at,
                )
                .returning(table.c.id)
            ).scalar_one()
            commit_seq = _advance_commit_seq(connection, now)
            return {
                "created": True,
                "exact_replay": False,
                "session_id": session_id,
                "family_id": family_id,
                "commit_seq": str(commit_seq),
            }

    def rotate_refresh_session(
        self,
        *,
        token_hash: str,
        next_token_hash: str,
        now: datetime,
        idle_expires_at: datetime,
        reuse_grace_seconds: int,
    ) -> dict[str, Any]:
        """Rotate once; a caller can replay the same next hash after an unknown outcome."""

        table = LiveRefreshSession.__table__
        with _write_transaction(self.engine) as connection:
            parent = connection.execute(select(table).where(table.c.token_hash == token_hash)).mappings().first()
            if parent is None or parent["revoked_at"] is not None:
                return {"status": "invalid", "commit_seq": str(_current_commit_seq(connection))}
            if now > _as_aware_utc(parent["absolute_expires_at"]) or now > _as_aware_utc(parent["idle_expires_at"]):
                return {"status": "invalid", "commit_seq": str(_current_commit_seq(connection))}
            user = connection.execute(select(LiveUser.__table__).where(LiveUser.id == parent["user_id"])).mappings().first()
            if user is None or user["is_active"] is not True:
                count = connection.execute(
                    update(table).where(table.c.family_id == parent["family_id"], table.c.revoked_at.is_(None)).values(revoked_at=now)
                ).rowcount
                commit_seq = _advance_commit_seq(connection, now) if count else _current_commit_seq(connection)
                return {
                    "status": "family_revoked" if count else "invalid",
                    "revoked_count": count,
                    "commit_seq": str(commit_seq),
                }

            if parent["used_at"] is not None:
                child = (
                    connection.execute(select(table).where(table.c.parent_id == parent["id"], table.c.revoked_at.is_(None)))
                    .mappings()
                    .first()
                )
                if child is not None and hmac.compare_digest(str(child["token_hash"]), next_token_hash):
                    return {
                        "status": "exact_replay",
                        "session_id": child["id"],
                        "user_id": parent["user_id"],
                        "family_id": parent["family_id"],
                        "user": _user_dto(user),
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                elapsed = (now - _as_aware_utc(parent["used_at"])).total_seconds()
                if elapsed <= reuse_grace_seconds:
                    return {"status": "invalid", "commit_seq": str(_current_commit_seq(connection))}
                count = connection.execute(
                    update(table).where(table.c.family_id == parent["family_id"], table.c.revoked_at.is_(None)).values(revoked_at=now)
                ).rowcount
                commit_seq = _advance_commit_seq(connection, now) if count else _current_commit_seq(connection)
                return {
                    "status": "family_revoked",
                    "revoked_count": count,
                    "commit_seq": str(commit_seq),
                }

            collision = connection.execute(select(table).where(table.c.token_hash == next_token_hash)).first()
            if collision is not None:
                return {"conflict": "next_token_hash", "commit_seq": str(_current_commit_seq(connection))}
            connection.execute(update(table).where(table.c.id == parent["id"]).values(used_at=now))
            child_id = connection.execute(
                insert(table)
                .values(
                    token_hash=next_token_hash,
                    user_id=parent["user_id"],
                    family_id=parent["family_id"],
                    parent_id=parent["id"],
                    created_at=now,
                    absolute_expires_at=parent["absolute_expires_at"],
                    idle_expires_at=idle_expires_at,
                )
                .returning(table.c.id)
            ).scalar_one()
            commit_seq = _advance_commit_seq(connection, now)
            return {
                "status": "rotated",
                "session_id": child_id,
                "user_id": parent["user_id"],
                "family_id": parent["family_id"],
                "user": _user_dto(user),
                "commit_seq": str(commit_seq),
            }

    def revoke_refresh_family(self, *, token_hash: str, now: datetime) -> dict[str, Any]:
        """Find a cookie's family and revoke every still-active member."""

        table = LiveRefreshSession.__table__
        with _write_transaction(self.engine) as connection:
            row = connection.execute(select(table.c.family_id).where(table.c.token_hash == token_hash)).first()
            if row is None:
                return {
                    "found": False,
                    "changed": False,
                    "revoked_count": 0,
                    "commit_seq": str(_current_commit_seq(connection)),
                }
            count = connection.execute(
                update(table).where(table.c.family_id == row.family_id, table.c.revoked_at.is_(None)).values(revoked_at=now)
            ).rowcount
            commit_seq = _advance_commit_seq(connection, now) if count else _current_commit_seq(connection)
            return {
                "found": True,
                "changed": bool(count),
                "revoked_count": count,
                "commit_seq": str(commit_seq),
            }

    def update_user(
        self,
        *,
        user_id: int,
        display_name: str | None,
        avatar_url: str | None,
        prefs: dict[str, Any] | None,
        update_mask: list[str],
    ) -> dict[str, Any]:
        """Update the bounded user profile without conflating omitted and null."""

        table = LiveUser.__table__
        now = datetime.now(UTC)
        requested = {"display_name": display_name, "avatar_url": avatar_url, "prefs": prefs}
        with _write_transaction(self.engine) as connection:
            row = connection.execute(select(table).where(table.c.id == user_id)).mappings().first()
            if row is None:
                return {"found": False, "changed": False, "commit_seq": str(_current_commit_seq(connection))}
            values = {field: requested[field] for field in update_mask if row[field] != requested[field]}
            if values:
                values["updated_at"] = now
                connection.execute(update(table).where(table.c.id == user_id).values(**values))
                commit_seq = _advance_commit_seq(connection, now)
                row = connection.execute(select(table).where(table.c.id == user_id)).mappings().one()
            else:
                commit_seq = _current_commit_seq(connection)
            return {
                "found": True,
                "changed": bool(values),
                "user": _user_dto(row),
                "commit_seq": str(commit_seq),
            }

    def list_devices(self, *, owner_id: int, include_revoked: bool) -> dict[str, Any]:
        """Return one owner's machine credentials from a single snapshot."""

        token_table = LiveDeviceToken.__table__
        with _read_snapshot(self.engine) as connection:
            commit_seq = _current_commit_seq(connection)
            statement = select(token_table).where(token_table.c.owner_id == owner_id)
            if not include_revoked:
                statement = statement.where(token_table.c.revoked_at.is_(None))
            rows = (
                connection.execute(
                    statement.order_by(token_table.c.created_at.desc(), token_table.c.id).limit(DEVICE_TOKEN_LIMIT_PER_OWNER + 1)
                )
                .mappings()
                .all()
            )
            if len(rows) > DEVICE_TOKEN_LIMIT_PER_OWNER:
                return {
                    "commit_seq": str(commit_seq),
                    "tokens": [],
                    "total": 0,
                    "limit_exceeded": True,
                }
            return {
                "commit_seq": str(commit_seq),
                "tokens": [
                    {
                        "id": str(row["id"]),
                        "device_id": str(row["device_id"]),
                        "machine_name": row["machine_name"],
                        "created_at": _encode_datetime(row["created_at"]),
                        "last_used_at": _encode_datetime(row["last_used_at"]),
                        "revoked_at": _encode_datetime(row["revoked_at"]),
                        "is_valid": row["revoked_at"] is None,
                    }
                    for row in rows
                ],
                "total": len(rows),
                "limit_exceeded": False,
            }

    def create_device(
        self,
        *,
        owner_id: int,
        token_id: str,
        device_id: str,
        token_hash: str,
    ) -> dict[str, Any]:
        """Create one machine credential, idempotently keyed by token_id."""

        token_table = LiveDeviceToken.__table__
        now = datetime.now(UTC)
        with _write_transaction(self.engine) as connection:
            existing = connection.execute(select(token_table).where(token_table.c.id == token_id)).mappings().first()
            if existing is not None:
                exact_replay = (
                    existing["owner_id"] == owner_id
                    and existing["device_id"] == device_id
                    and hmac.compare_digest(str(existing["token_hash"]), token_hash)
                )
                return {
                    "created": False,
                    "exact_replay": exact_replay,
                    "limit_exceeded": False,
                    "token_id": str(existing["id"]),
                    "device_id": str(existing["device_id"]),
                    "created_at": _encode_datetime(existing["created_at"]),
                    "commit_seq": str(_current_commit_seq(connection)),
                }

            token_count = connection.execute(
                select(func.count()).select_from(token_table).where(token_table.c.owner_id == owner_id)
            ).scalar_one()
            if token_count >= DEVICE_TOKEN_LIMIT_PER_OWNER:
                return {
                    "created": False,
                    "exact_replay": False,
                    "limit_exceeded": True,
                    "commit_seq": str(_current_commit_seq(connection)),
                }

            connection.execute(
                token_table.insert().values(
                    id=token_id,
                    owner_id=owner_id,
                    device_id=device_id,
                    machine_name=None,
                    token_hash=token_hash,
                    created_at=now,
                )
            )
            commit_seq = connection.execute(
                update(catalog_meta)
                .where(catalog_meta.c.singleton == 1)
                .values(
                    commit_seq=catalog_meta.c.commit_seq + 1,
                    updated_at=now.isoformat(),
                )
                .returning(catalog_meta.c.commit_seq)
            ).scalar_one()
            return {
                "created": True,
                "exact_replay": False,
                "limit_exceeded": False,
                "token_id": token_id,
                "device_id": device_id,
                "created_at": now.isoformat(),
                "commit_seq": str(commit_seq),
            }

    def rename_machine(self, *, owner_id: int, device_id: str, machine_name: str) -> dict[str, Any]:
        """Set one durable display name across active credentials for a machine."""

        token_table = LiveDeviceToken.__table__
        now = datetime.now(UTC)
        with _write_transaction(self.engine) as connection:
            rows = connection.execute(
                select(token_table.c.id, token_table.c.machine_name).where(
                    token_table.c.owner_id == owner_id,
                    token_table.c.device_id == device_id,
                    token_table.c.revoked_at.is_(None),
                )
            ).all()
            if not rows:
                return {"found": False, "changed": False, "commit_seq": str(_current_commit_seq(connection))}
            changed = any(row.machine_name != machine_name for row in rows)
            if changed:
                connection.execute(
                    update(token_table)
                    .where(
                        token_table.c.owner_id == owner_id,
                        token_table.c.device_id == device_id,
                        token_table.c.revoked_at.is_(None),
                    )
                    .values(machine_name=machine_name)
                )
                commit_seq = _advance_commit_seq(connection, now)
            else:
                commit_seq = _current_commit_seq(connection)
            return {
                "found": True,
                "changed": changed,
                "device_id": device_id,
                "machine_name": machine_name,
                "commit_seq": str(commit_seq),
            }

    def revoke_device(self, *, owner_id: int, token_id: str) -> dict[str, Any]:
        """Idempotently revoke one machine credential in a single commit.

        A replay after a lost response returns the durable revocation without
        allocating another commit sequence number. Its ``commit_seq`` is the
        current catalog sequence, not necessarily the original revoke's seq.
        """

        token_table = LiveDeviceToken.__table__
        now = datetime.now(UTC)
        with _write_transaction(self.engine) as connection:
            row = (
                connection.execute(
                    select(token_table.c.id, token_table.c.revoked_at).where(
                        token_table.c.id == token_id,
                        token_table.c.owner_id == owner_id,
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return {
                    "found": False,
                    "changed": False,
                    "commit_seq": str(_current_commit_seq(connection)),
                }

            revoked_at = _as_aware_utc(row["revoked_at"])
            if revoked_at is not None:
                return {
                    "found": True,
                    "changed": False,
                    "token_id": str(row["id"]),
                    "revoked_at": _encode_datetime(revoked_at),
                    "commit_seq": str(_current_commit_seq(connection)),
                }

            connection.execute(
                update(token_table).where(token_table.c.id == token_id, token_table.c.owner_id == owner_id).values(revoked_at=now)
            )
            commit_seq = connection.execute(
                update(catalog_meta)
                .where(catalog_meta.c.singleton == 1)
                .values(
                    commit_seq=catalog_meta.c.commit_seq + 1,
                    updated_at=now.isoformat(),
                )
                .returning(catalog_meta.c.commit_seq)
            ).scalar_one()
            return {
                "found": True,
                "changed": True,
                "token_id": str(row["id"]),
                "revoked_at": now.isoformat(),
                "commit_seq": str(commit_seq),
            }

    def apply_machine_heartbeat(
        self,
        *,
        heartbeat: dict[str, Any],
        managed_leases: list[dict[str, Any]],
        managed_leases_present: bool,
        owner_id: int | None,
    ) -> dict[str, Any]:
        """Atomically persist and reconcile one hosted Machine Agent heartbeat."""

        from zerg.services.live_session_state import mark_missing_live_sessions
        from zerg.services.live_session_state import upsert_live_sessions_from_managed_leases
        from zerg.services.managed_control_state import mark_missing_live_control_leases
        from zerg.services.managed_control_state import upsert_live_control_leases

        device_id = str(heartbeat["device_id"])
        received_at = heartbeat["received_at"]
        assert isinstance(received_at, datetime)
        request_sha256 = _heartbeat_request_sha256(
            heartbeat=heartbeat,
            managed_leases=managed_leases,
            managed_leases_present=managed_leases_present,
            owner_id=owner_id,
        )
        stamp = LiveHeartbeatStamp.__table__
        with _write_transaction(self.engine) as connection:
            replay = (
                connection.execute(
                    select(stamp)
                    .where(stamp.c.device_id == device_id, stamp.c.received_at == received_at)
                    .order_by(stamp.c.id.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if replay is not None:
                if replay["request_sha256"] != request_sha256:
                    return {
                        "idempotency_conflict": True,
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                stored_result = _decode_json_object(replay["catalog_result_json"])
                if not isinstance(stored_result, dict):
                    raise RuntimeError("heartbeat replay receipt is incomplete")
                return {**stored_result, "exact_replay": True}

            incoming_digest = str(heartbeat.get("sessions_digest") or "").strip() or None
            previous_sessions_digest: str | None = None
            if managed_leases_present and incoming_digest is not None:
                previous = connection.execute(
                    select(stamp.c.sessions_digest)
                    .where(stamp.c.device_id == device_id)
                    .order_by(stamp.c.received_at.desc(), stamp.c.id.desc())
                    .limit(1)
                ).scalar_one_or_none()
                previous_sessions_digest = str(previous or "").strip() or None

            cutoff = received_at - timedelta(days=30)
            connection.execute(stamp.delete().where(stamp.c.device_id == device_id, stamp.c.received_at < cutoff))
            stamp_id = connection.execute(
                insert(stamp).values(**heartbeat, request_sha256=request_sha256).returning(stamp.c.id)
            ).scalar_one()

            lease_objects = [SimpleNamespace(**lease) for lease in managed_leases]
            touched: set[UUID] = set()
            orm = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            try:
                if lease_objects:
                    touched.update(
                        upsert_live_control_leases(
                            orm,
                            lease_objects,
                            device_id=device_id,
                            received_at=received_at,
                        )
                    )
                    touched.update(
                        upsert_live_sessions_from_managed_leases(
                            orm,
                            lease_objects,
                            device_id=device_id,
                            owner_id=owner_id,
                            received_at=received_at,
                        )
                    )
                if managed_leases_present:
                    touched.update(
                        mark_missing_live_control_leases(
                            orm,
                            lease_objects,
                            device_id=device_id,
                            received_at=received_at,
                        )
                    )
                    touched.update(
                        mark_missing_live_sessions(
                            orm,
                            {UUID(str(lease.session_id)) for lease in lease_objects},
                            device_id=device_id,
                            received_at=received_at,
                        )
                    )
                orm.commit()
            except BaseException:
                orm.rollback()
                raise
            finally:
                orm.close()

            commit_seq = _advance_commit_seq(connection, received_at)
            result = {
                "previous_sessions_digest": previous_sessions_digest,
                "commit_seq": str(commit_seq),
                "touched_session_ids": sorted(str(session_id) for session_id in touched),
                "exact_replay": False,
            }
            connection.execute(
                update(stamp)
                .where(stamp.c.id == stamp_id)
                .values(catalog_result_json=json.dumps(result, sort_keys=True, separators=(",", ":")))
            )
            return result

    def apply_session_runtime(self, *, events: list[Any]) -> dict[str, Any]:
        """Atomically reduce one bounded runtime batch."""

        from zerg.services.session_runtime import ingest_live_runtime_events

        observed_at = datetime.now(UTC)
        with _write_transaction(self.engine) as connection:
            orm = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            try:
                result = ingest_live_runtime_events(orm, events)
                updated_keys = set(result.updated_runtime_keys)
                resume_session_ids = {
                    str(event.session_id)
                    for event in events
                    if event.session_id is not None
                    and event.runtime_key in updated_keys
                    and event.kind == "phase_signal"
                    and event.phase in {"thinking", "running"}
                }
                if resume_session_ids:
                    orm.query(LiveSessionCatalog).filter(
                        LiveSessionCatalog.session_id.in_(resume_session_ids),
                        LiveSessionCatalog.user_state == "snoozed",
                    ).update(
                        {"user_state": "active", "user_state_at": observed_at},
                        synchronize_session=False,
                    )
                for event in events:
                    if event.runtime_key in updated_keys and event.kind in {"pause_request", "pause_resolution"}:
                        _apply_live_interaction_event(orm, event)
                    # Binding aliases are an idempotent graph side effect, not a
                    # runtime-state mutation. A valid binding can leave the
                    # reducer snapshot unchanged and still must be persisted so
                    # the next Console turn can resume the provider thread.
                    if event.kind == "binding_signal":
                        provider_session_id = str((event.payload or {}).get("provider_session_id") or "").strip()
                        if provider_session_id and event.session_id is not None:
                            catalog = orm.get(LiveSessionCatalog, str(event.session_id))
                            thread_id = str(event.thread_id or (catalog.primary_thread_id if catalog is not None else ""))
                            alias = (
                                orm.query(LiveSessionThreadAlias)
                                .filter(
                                    LiveSessionThreadAlias.thread_id == thread_id,
                                    LiveSessionThreadAlias.provider == event.provider,
                                    LiveSessionThreadAlias.alias_kind == "provider_session_id",
                                    LiveSessionThreadAlias.alias_value == provider_session_id,
                                )
                                .one_or_none()
                            )
                            if alias is None:
                                orm.add(
                                    LiveSessionThreadAlias(
                                        thread_id=thread_id,
                                        provider=event.provider,
                                        alias_kind="provider_session_id",
                                        alias_value=provider_session_id,
                                        first_seen_at=event.occurred_at or observed_at,
                                        last_seen_at=event.occurred_at or observed_at,
                                    )
                                )
                            else:
                                alias.last_seen_at = event.occurred_at or observed_at
                orm.commit()
            except BaseException:
                orm.rollback()
                raise
            finally:
                orm.close()
            commit_seq = _advance_commit_seq(connection, observed_at)
            return {
                **result.model_dump(mode="json"),
                "commit_seq": str(commit_seq),
            }

    def register_interaction(self, *, interaction: dict[str, Any]) -> dict[str, Any]:
        """Register one held interaction and its canonical runtime state."""

        from zerg.services.session_runtime import RuntimeEventIngest
        from zerg.services.session_runtime import ingest_live_runtime_events

        event = RuntimeEventIngest(
            runtime_key=interaction["runtime_key"],
            session_id=UUID(interaction["session_id"]),
            provider=interaction["provider"],
            device_id=interaction.get("device_id"),
            source=interaction["source"] or "interaction_api",
            kind="pause_request",
            tool_name=interaction.get("tool_name"),
            occurred_at=interaction["occurred_at"],
            dedupe_key=f"interaction:{interaction['request_key']}",
            payload={
                "request_key": interaction["request_key"],
                "provider_request_id": interaction.get("provider_request_id"),
                "provider_ref": {
                    "source": interaction.get("source"),
                    "reply_transport": interaction.get("reply_transport"),
                },
                "kind": interaction["kind"],
                "tool_name": interaction.get("tool_name"),
                "title": interaction.get("title"),
                "summary": interaction.get("summary"),
                "request_payload": interaction.get("request_payload") or {},
                "can_respond": interaction["can_respond"],
                "expires_at": _encode_datetime(interaction.get("expires_at")),
                "single_active": interaction["single_active"],
            },
        )
        observed_at = interaction["occurred_at"]
        with _write_transaction(self.engine) as connection:
            orm = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            try:
                if orm.get(LiveSessionCatalog, interaction["session_id"]) is None:
                    orm.rollback()
                    return {
                        "found_session": False,
                        "interaction": None,
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                result = ingest_live_runtime_events(orm, [event])
                row = _apply_live_interaction_event(orm, event)
                orm.commit()
            except BaseException:
                orm.rollback()
                raise
            finally:
                orm.close()
            commit_seq = _advance_commit_seq(connection, observed_at)
            return {
                "found_session": True,
                "interaction": _interaction_dto(row),
                "accepted": result.accepted,
                "duplicates": result.duplicates,
                "commit_seq": str(commit_seq),
            }

    def list_interactions(self, *, session_id: str, status: str | None, limit: int) -> dict[str, Any]:
        with _read_snapshot(self.engine) as connection:
            orm = Session(bind=connection, expire_on_commit=False)
            try:
                query = orm.query(LiveInteractionRequest).filter(LiveInteractionRequest.session_id == session_id)
                if status is not None:
                    query = query.filter(LiveInteractionRequest.status == status)
                rows = (
                    query.order_by(
                        LiveInteractionRequest.last_seen_at.desc(),
                        LiveInteractionRequest.occurred_at.desc(),
                        LiveInteractionRequest.id.desc(),
                    )
                    .limit(limit)
                    .all()
                )
                result = [_interaction_dto(row) for row in rows]
                if not result and status in {None, "pending"}:
                    runtime = (
                        orm.query(LiveRuntimeState)
                        .filter(LiveRuntimeState.session_id == UUID(session_id))
                        .order_by(LiveRuntimeState.updated_at.desc(), LiveRuntimeState.runtime_version.desc())
                        .first()
                    )
                    fallback = _runtime_interaction_dto(runtime) if runtime is not None else None
                    if fallback is not None:
                        result = [fallback]
            finally:
                orm.close()
            return {
                "interactions": result,
                "total": len(result),
                "commit_seq": str(_current_commit_seq(connection)),
            }

    def resolve_interaction(
        self,
        *,
        session_id: str,
        interaction_id: str,
        status: str,
        response_payload: dict[str, Any],
        response_text: str | None,
        resolved_at: datetime,
    ) -> dict[str, Any]:
        """Resolve exactly one pending interaction and clear matching runtime truth."""

        from zerg.services.session_runtime import RuntimeEventIngest
        from zerg.services.session_runtime import ingest_live_runtime_events

        with _write_transaction(self.engine) as connection:
            orm = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            try:
                row = (
                    orm.query(LiveInteractionRequest)
                    .filter(
                        LiveInteractionRequest.id == interaction_id,
                        LiveInteractionRequest.session_id == session_id,
                    )
                    .one_or_none()
                )
                if row is None:
                    runtime = (
                        orm.query(LiveRuntimeState)
                        .filter(LiveRuntimeState.session_id == UUID(session_id))
                        .order_by(LiveRuntimeState.updated_at.desc(), LiveRuntimeState.runtime_version.desc())
                        .first()
                    )
                    fallback = _runtime_interaction_dto(runtime) if runtime is not None else None
                    if fallback is not None and fallback["id"] == interaction_id:
                        row = LiveInteractionRequest(
                            id=interaction_id,
                            session_id=session_id,
                            runtime_key=fallback["runtime_key"],
                            provider=fallback["provider"],
                            request_key=fallback["request_key"],
                            provider_request_id=fallback["provider_request_id"],
                            source=fallback["source"],
                            reply_transport=fallback["reply_transport"],
                            kind=fallback["kind"],
                            status="pending",
                            can_respond=int(fallback["can_respond"]),
                            request_payload_json={},
                            projection_json=fallback["projection"],
                            occurred_at=_as_aware_utc(runtime.pending_interaction_opened_at) or resolved_at,
                            last_seen_at=_as_aware_utc(runtime.pending_interaction_updated_at) or resolved_at,
                            created_at=_as_aware_utc(runtime.pending_interaction_opened_at) or resolved_at,
                            updated_at=_as_aware_utc(runtime.pending_interaction_updated_at) or resolved_at,
                        )
                        orm.add(row)
                        orm.flush()
                if row is None:
                    orm.rollback()
                    return {
                        "found": False,
                        "resolved": False,
                        "reason": "not_found",
                        "interaction": None,
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                if row.status != "pending":
                    result = _interaction_dto(row)
                    orm.rollback()
                    return {
                        "found": True,
                        "resolved": False,
                        "reason": "not_pending",
                        "interaction": result,
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                if not bool(row.can_respond):
                    result = _interaction_dto(row)
                    orm.rollback()
                    return {
                        "found": True,
                        "resolved": False,
                        "reason": "not_answerable",
                        "interaction": result,
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                event = RuntimeEventIngest(
                    runtime_key=row.runtime_key,
                    session_id=UUID(session_id),
                    provider=row.provider,
                    source="interaction_response",
                    kind="pause_resolution",
                    occurred_at=resolved_at,
                    dedupe_key=f"interaction-resolution:{interaction_id}:{status}",
                    payload={
                        "request_key": row.request_key,
                        "provider_request_id": row.provider_request_id,
                        "status": status,
                        "response_payload": response_payload,
                        "response_text": response_text,
                    },
                )
                ingest_live_runtime_events(orm, [event])
                resolved = _apply_live_interaction_event(orm, event)
                orm.commit()
            except BaseException:
                orm.rollback()
                raise
            finally:
                orm.close()
            commit_seq = _advance_commit_seq(connection, resolved_at)
            return {
                "found": True,
                "resolved": True,
                "reason": None,
                "interaction": _interaction_dto(resolved),
                "commit_seq": str(commit_seq),
            }

    def read_interaction_decision(
        self,
        *,
        session_id: str,
        interaction_id: str | None,
        request_key: str | None,
    ) -> dict[str, Any]:
        with _read_snapshot(self.engine) as connection:
            orm = Session(bind=connection, expire_on_commit=False)
            try:
                query = orm.query(LiveInteractionRequest).filter(
                    LiveInteractionRequest.session_id == session_id,
                    LiveInteractionRequest.kind == "permission_prompt",
                    LiveInteractionRequest.source == "claude_permission_gate",
                )
                query = (
                    query.filter(LiveInteractionRequest.id == interaction_id)
                    if interaction_id is not None
                    else query.filter(LiveInteractionRequest.request_key == request_key)
                )
                row = query.one_or_none()
                if row is None or row.status == "pending":
                    result = {"found": row is not None, "resolved": False, "decision": None, "reason": None}
                else:
                    response = row.response_payload_json if isinstance(row.response_payload_json, dict) else {}
                    raw = str(response.get("permissionDecision") or "").strip().lower()
                    result = {
                        "found": True,
                        "resolved": True,
                        "decision": "allow" if raw == "allow" else "deny",
                        "reason": response.get("permissionDecisionReason") or row.response_text,
                    }
            finally:
                orm.close()
            return {**result, "commit_seq": str(_current_commit_seq(connection))}

    def apply_control_command_result(
        self,
        *,
        owner_id: int,
        device_id: str,
        message: dict[str, Any],
    ) -> dict[str, Any]:
        """Reconcile one unmatched control result without opening SQLite in the API."""

        from zerg.services.machine_control_operations import TERMINAL_OPERATION_STATUSES
        from zerg.services.remote_session_launch import reconcile_live_launch_from_command_result

        command_id = str(message["command_id"])
        observed_at = datetime.now(UTC)
        with _write_transaction(self.engine) as connection:
            orm = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            matched = False
            changed = False
            match_kind: str | None = None
            try:
                operation = (
                    orm.query(LiveMachineControlOperation)
                    .filter(
                        LiveMachineControlOperation.command_id == command_id,
                        LiveMachineControlOperation.owner_id == owner_id,
                        LiveMachineControlOperation.device_id == device_id,
                    )
                    .first()
                )
                if operation is not None:
                    matched = True
                    match_kind = "operation"
                    if str(operation.status) not in TERMINAL_OPERATION_STATUSES:
                        operation.finished_at = observed_at
                        operation.updated_at = observed_at
                        operation.expires_at = None
                        if message["ok"]:
                            operation.status = "succeeded"
                            operation.result_json = json.dumps(message.get("result") or {}, sort_keys=True)
                            operation.error_json = None
                        else:
                            error = message.get("error") or {}
                            operation.status = "failed"
                            operation.error_json = json.dumps(
                                {
                                    "code": str(error.get("code") or "machine_control_operation_failed"),
                                    "message": str(error.get("message") or "Machine Agent control command failed"),
                                },
                                sort_keys=True,
                            )
                        changed = True
                elif command_id.startswith(("launch-", "continue-")):
                    matched = reconcile_live_launch_from_command_result(
                        orm,
                        message,
                        command_id=command_id,
                    )
                    match_kind = "launch" if matched else None
                    changed = matched
                orm.commit()
            except BaseException:
                orm.rollback()
                raise
            finally:
                orm.close()
            commit_seq = _advance_commit_seq(connection, observed_at) if changed else _current_commit_seq(connection)
            return {
                "matched": matched,
                "match_kind": match_kind,
                "commit_seq": str(commit_seq),
            }

    def prepare_control_command(
        self,
        *,
        operation_id: str,
        owner_id: int,
        session_id: str,
        device_id: str,
        provider: str,
        command_type: str,
        command_id: str,
        capability: str,
        request_payload: dict[str, Any],
        timeout_secs: int,
    ) -> dict[str, Any]:
        """Validate the command-time lease and durably reserve one operation."""

        from zerg.services.live_control_catalog import get_live_control_grant
        from zerg.services.machine_control_operations import MACHINE_OPERATION_TIMEOUT_GRACE_SECS

        observed_at = datetime.now(UTC)
        with _write_transaction(self.engine) as connection:
            orm = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            try:
                existing = orm.query(LiveMachineControlOperation).filter(LiveMachineControlOperation.command_id == command_id).one_or_none()
                if existing is not None:
                    stored_request = _decode_json_object(existing.request_json)
                    stored_grant = stored_request.pop("longhouse_control_grant", None)
                    exact_replay = (
                        str(existing.id) == operation_id
                        and existing.owner_id == owner_id
                        and str(existing.session_id or "") == session_id
                        and str(existing.device_id) == device_id
                        and str(existing.provider or "") == provider
                        and str(existing.command_type) == command_type
                        and int(existing.timeout_secs) == timeout_secs
                        and stored_request == request_payload
                        and isinstance(stored_grant, dict)
                    )
                    orm.rollback()
                    return {
                        "allowed": exact_replay,
                        "reason": None if exact_replay else "idempotency_conflict",
                        "operation_id": str(existing.id) if exact_replay else None,
                        "grant": stored_grant if exact_replay else None,
                        "exact_replay": exact_replay,
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                grant = get_live_control_grant(orm, session_id=session_id, capability=capability)
                if grant is None:
                    orm.rollback()
                    return {
                        "allowed": False,
                        "reason": "control_unavailable",
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                operation = LiveMachineControlOperation(
                    id=operation_id,
                    owner_id=owner_id,
                    session_id=session_id,
                    device_id=device_id,
                    provider=provider,
                    command_type=command_type,
                    command_id=command_id,
                    status="running",
                    request_json=json.dumps(
                        {
                            **request_payload,
                            "longhouse_control_grant": {
                                "connection_id": grant.connection_id,
                                "run_id": grant.run_id,
                                "lease_generation": grant.lease_generation,
                            },
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    timeout_secs=timeout_secs,
                    started_at=observed_at,
                    created_at=observed_at,
                    updated_at=observed_at,
                    expires_at=observed_at + timedelta(seconds=timeout_secs + MACHINE_OPERATION_TIMEOUT_GRACE_SECS),
                )
                orm.add(operation)
                orm.commit()
            except BaseException:
                orm.rollback()
                raise
            finally:
                orm.close()
            commit_seq = _advance_commit_seq(connection, observed_at)
            return {
                "allowed": True,
                "reason": None,
                "operation_id": operation_id,
                "grant": {
                    "connection_id": grant.connection_id,
                    "run_id": grant.run_id,
                    "lease_generation": grant.lease_generation,
                },
                "exact_replay": False,
                "commit_seq": str(commit_seq),
            }

    def finish_control_operation(
        self,
        *,
        operation_id: str,
        status: str,
        result: dict[str, Any] | None,
        error: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Finish one command operation in catalogd's serialized transaction."""

        from zerg.services.machine_control_operations import TERMINAL_OPERATION_STATUSES

        observed_at = datetime.now(UTC)
        with _write_transaction(self.engine) as connection:
            orm = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            changed = False
            try:
                operation = orm.get(LiveMachineControlOperation, operation_id)
                if operation is None:
                    orm.rollback()
                    return {
                        "found": False,
                        "changed": False,
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                if str(operation.status) not in TERMINAL_OPERATION_STATUSES:
                    operation.status = status
                    operation.result_json = json.dumps(result, sort_keys=True, separators=(",", ":")) if result is not None else None
                    operation.error_json = json.dumps(error, sort_keys=True, separators=(",", ":")) if error is not None else None
                    operation.finished_at = observed_at
                    operation.updated_at = observed_at
                    operation.expires_at = None
                    changed = True
                    orm.commit()
                else:
                    orm.rollback()
            except BaseException:
                orm.rollback()
                raise
            finally:
                orm.close()
            commit_seq = _advance_commit_seq(connection, observed_at) if changed else _current_commit_seq(connection)
            return {"found": True, "changed": changed, "commit_seq": str(commit_seq)}

    def prepare_machine_operation(
        self,
        *,
        operation_id: str,
        owner_id: int,
        device_id: str,
        provider: str,
        command_type: str,
        command_id: str,
        request_payload: dict[str, Any],
        timeout_secs: int,
    ) -> dict[str, Any]:
        """Reserve one machine-scoped operation without opening SQLite in the API."""

        from zerg.services.machine_control_operations import MACHINE_OPERATION_TIMEOUT_GRACE_SECS
        from zerg.services.machine_control_operations import NONTERMINAL_OPERATION_STATUSES

        observed_at = datetime.now(UTC)
        with _write_transaction(self.engine) as connection:
            orm = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            try:
                stale = (
                    orm.query(LiveMachineControlOperation)
                    .filter(
                        LiveMachineControlOperation.status.in_(NONTERMINAL_OPERATION_STATUSES),
                        LiveMachineControlOperation.expires_at.is_not(None),
                        LiveMachineControlOperation.expires_at <= observed_at,
                    )
                    .all()
                )
                for row in stale:
                    row.status = "timed_out"
                    row.error_json = json.dumps(
                        {
                            "code": "machine_control_operation_timeout",
                            "message": "Machine Agent did not report back before the operation lease expired",
                        },
                        sort_keys=True,
                    )
                    row.finished_at = _as_aware_utc(row.expires_at) or observed_at
                    row.updated_at = observed_at
                    row.expires_at = None

                existing = orm.query(LiveMachineControlOperation).filter(LiveMachineControlOperation.command_id == command_id).one_or_none()
                if existing is not None:
                    exact_replay = (
                        str(existing.id) == operation_id
                        and existing.owner_id == owner_id
                        and str(existing.device_id) == device_id
                        and str(existing.provider or "") == provider
                        and str(existing.command_type) == command_type
                        and int(existing.timeout_secs) == timeout_secs
                        and _decode_json_object(existing.request_json) == request_payload
                    )
                    orm.commit() if stale else orm.rollback()
                    commit_seq = _advance_commit_seq(connection, observed_at) if stale else _current_commit_seq(connection)
                    return {
                        "created": False,
                        "exact_replay": exact_replay,
                        "active_conflict": not exact_replay,
                        "operation": _machine_operation_dto(existing) if exact_replay else None,
                        "commit_seq": str(commit_seq),
                    }

                active = (
                    orm.query(LiveMachineControlOperation)
                    .filter(
                        LiveMachineControlOperation.owner_id == owner_id,
                        LiveMachineControlOperation.device_id == device_id,
                        LiveMachineControlOperation.provider == provider,
                        LiveMachineControlOperation.command_type == command_type,
                        LiveMachineControlOperation.status.in_(NONTERMINAL_OPERATION_STATUSES),
                    )
                    .order_by(LiveMachineControlOperation.created_at.desc())
                    .first()
                )
                if active is not None:
                    orm.commit() if stale else orm.rollback()
                    commit_seq = _advance_commit_seq(connection, observed_at) if stale else _current_commit_seq(connection)
                    return {
                        "created": False,
                        "exact_replay": False,
                        "active_conflict": True,
                        "operation": _machine_operation_dto(active),
                        "commit_seq": str(commit_seq),
                    }

                operation = LiveMachineControlOperation(
                    id=operation_id,
                    owner_id=owner_id,
                    device_id=device_id,
                    provider=provider,
                    command_type=command_type,
                    command_id=command_id,
                    status="running",
                    request_json=json.dumps(request_payload, sort_keys=True, separators=(",", ":")),
                    timeout_secs=timeout_secs,
                    started_at=observed_at,
                    created_at=observed_at,
                    updated_at=observed_at,
                    expires_at=observed_at + timedelta(seconds=timeout_secs + MACHINE_OPERATION_TIMEOUT_GRACE_SECS),
                )
                orm.add(operation)
                orm.commit()
            except BaseException:
                orm.rollback()
                raise
            finally:
                orm.close()
            commit_seq = _advance_commit_seq(connection, observed_at)
            return {
                "created": True,
                "exact_replay": False,
                "active_conflict": False,
                "operation": _machine_operation_dto(operation),
                "commit_seq": str(commit_seq),
            }

    def read_machine_operation(self, *, owner_id: int, operation_id: str) -> dict[str, Any]:
        """Read one owner-scoped operation and materialize timeout if required."""

        from zerg.services.machine_control_operations import NONTERMINAL_OPERATION_STATUSES

        observed_at = datetime.now(UTC)
        with _write_transaction(self.engine) as connection:
            orm = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            changed = False
            try:
                operation = (
                    orm.query(LiveMachineControlOperation)
                    .filter(
                        LiveMachineControlOperation.id == operation_id,
                        LiveMachineControlOperation.owner_id == owner_id,
                    )
                    .one_or_none()
                )
                if operation is None:
                    orm.rollback()
                    return {
                        "found": False,
                        "operation": None,
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                expires_at = _as_aware_utc(operation.expires_at)
                if str(operation.status) in NONTERMINAL_OPERATION_STATUSES and expires_at is not None and expires_at <= observed_at:
                    operation.status = "timed_out"
                    operation.error_json = json.dumps(
                        {
                            "code": "machine_control_operation_timeout",
                            "message": "Machine Agent did not report back before the operation lease expired",
                        },
                        sort_keys=True,
                    )
                    operation.finished_at = expires_at
                    operation.updated_at = observed_at
                    operation.expires_at = None
                    changed = True
                    orm.commit()
                    operation_payload = _machine_operation_dto(operation)
                else:
                    operation_payload = _machine_operation_dto(operation)
                    orm.rollback()
            except BaseException:
                orm.rollback()
                raise
            finally:
                orm.close()
            commit_seq = _advance_commit_seq(connection, observed_at) if changed else _current_commit_seq(connection)
            return {
                "found": True,
                "operation": operation_payload,
                "commit_seq": str(commit_seq),
            }

    def read_launch_idempotency(
        self,
        *,
        owner_id: int,
        device_id: str,
        provider: str,
        client_request_id: str,
    ) -> dict[str, Any]:
        """Resolve one launch idempotency key from a bounded snapshot."""

        from zerg.services.live_launch_readiness import get_live_launch_readiness_by_client_request

        with _read_snapshot(self.engine) as connection:
            orm = Session(bind=connection, expire_on_commit=False)
            try:
                view = get_live_launch_readiness_by_client_request(
                    orm,
                    owner_id=owner_id,
                    device_id=device_id,
                    provider=provider,
                    client_request_id=client_request_id,
                )
            finally:
                orm.close()
            return {
                "found": view is not None,
                "launch": _launch_view_dto(view) if view is not None else None,
                "commit_seq": str(_current_commit_seq(connection)),
            }

    def create_launch_intent(self, *, launch: dict[str, Any]) -> dict[str, Any]:
        """Atomically create a launch shell, readiness fact, and archive outbox row."""

        from zerg.services.live_archive_outbox import enqueue_remote_launch_outbox
        from zerg.services.live_archive_outbox import remote_launch_idempotency_key
        from zerg.services.live_catalog_launch import create_live_launch_catalog_shell
        from zerg.services.live_catalog_launch import live_launch_result
        from zerg.services.live_launch_readiness import upsert_live_launch_readiness

        observed_at = launch["started_at"]
        with _write_transaction(self.engine) as connection:
            orm = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            try:
                existing = (
                    orm.query(LiveSessionLaunchAttempt).filter(LiveSessionLaunchAttempt.command_id == launch["command_id"]).one_or_none()
                )
                if existing is not None:
                    outbox = (
                        orm.query(LiveArchiveOutbox)
                        .filter(LiveArchiveOutbox.idempotency_key == remote_launch_idempotency_key(session_id=launch["session_id"]))
                        .one_or_none()
                    )
                    stored_launch = json.loads(outbox.payload_json or "{}").get("launch") if outbox is not None else None
                    exact_replay = (
                        str(existing.session_id) == launch["session_id"]
                        and str(existing.thread_id or "") == launch["primary_thread_id"]
                        and str(existing.run_id or "") == str(launch.get("run_id") or "")
                        and existing.owner_id == launch["owner_id"]
                        and str(existing.provider) == launch["provider"]
                        and str(existing.host_id or "") == launch["device_id"]
                        and str(existing.execution_lifetime) == launch["execution_lifetime"]
                        and str(existing.client_request_id or "") == str(launch.get("client_request_id") or "")
                        and stored_launch == _canonical_outbox_value(launch)
                    )
                    result = live_launch_result(existing) if exact_replay else None
                    orm.rollback()
                    return {
                        "created": False,
                        "exact_replay": exact_replay,
                        "idempotency_conflict": not exact_replay,
                        "launch": _json_launch_result(result) if result is not None else None,
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                attempt = create_live_launch_catalog_shell(
                    orm,
                    session_id=UUID(launch["session_id"]),
                    thread_id=UUID(launch["primary_thread_id"]),
                    run_id=UUID(launch["run_id"]) if launch.get("run_id") else None,
                    owner_id=launch["owner_id"],
                    provider=launch["provider"],
                    device_id=launch["device_id"],
                    device_name=launch.get("machine_id"),
                    cwd=launch["cwd"],
                    project=launch["project"],
                    git_repo=launch.get("git_repo"),
                    git_branch=launch.get("git_branch"),
                    display_name=launch.get("display_name"),
                    initial_prompt=launch.get("initial_prompt"),
                    execution_lifetime=launch["execution_lifetime"],
                    client_request_id=launch.get("client_request_id"),
                    command_id=launch["command_id"],
                    started_at=observed_at,
                    expires_at=launch["expires_at"],
                    launch_actor=launch.get("launch_actor"),
                    launch_surface=launch.get("launch_surface"),
                )
                upsert_live_launch_readiness(
                    orm,
                    session_id=UUID(launch["session_id"]),
                    owner_id=launch["owner_id"],
                    device_id=launch["device_id"],
                    provider=launch["provider"],
                    execution_lifetime=launch["execution_lifetime"],
                    state="pending",
                    command_id=launch["command_id"],
                    client_request_id=launch.get("client_request_id"),
                    machine_id=launch.get("machine_id") or launch["device_id"],
                    project=launch["project"],
                    expires_at=launch["expires_at"],
                    now=observed_at,
                )
                enqueue_remote_launch_outbox(orm, launch=launch, completed=True)
                result = live_launch_result(attempt)
                orm.commit()
            except BaseException:
                orm.rollback()
                raise
            finally:
                orm.close()
            commit_seq = _advance_commit_seq(connection, observed_at)
            return {
                "created": True,
                "exact_replay": False,
                "idempotency_conflict": False,
                "launch": _json_launch_result(result),
                "commit_seq": str(commit_seq),
            }

    def create_console_session(self, *, data: dict[str, Any]) -> dict[str, Any]:
        """Create durable idle Console identity without a run or launch attempt."""

        from zerg.services.live_archive_outbox import enqueue_console_session_create_outbox
        from zerg.services.live_catalog_launch import create_live_console_session_shell

        observed_at = data["started_at"]
        with _write_transaction(self.engine) as connection:
            orm = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            try:
                existing = orm.get(LiveSessionCatalog, str(data["session_id"]))
                if existing is not None:
                    exact = (
                        str(existing.primary_thread_id or "") == str(data["thread_id"])
                        and str(existing.provider) == str(data["provider"])
                        and str(existing.device_id or "") == str(data["device_id"])
                        and str(existing.cwd or "") == str(data["cwd"])
                    )
                    orm.rollback()
                    return {
                        "created": False,
                        "exact_replay": exact,
                        "idempotency_conflict": not exact,
                        "session_id": str(data["session_id"]),
                        "thread_id": str(data["thread_id"]),
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                create_live_console_session_shell(orm, data=data)
                enqueue_console_session_create_outbox(orm, session=data)
                orm.commit()
            except BaseException:
                orm.rollback()
                raise
            finally:
                orm.close()
            commit_seq = _advance_commit_seq(connection, observed_at)
            return {
                "created": True,
                "exact_replay": False,
                "idempotency_conflict": False,
                "session_id": str(data["session_id"]),
                "thread_id": str(data["thread_id"]),
                "commit_seq": str(commit_seq),
            }

    def enqueue_console_turn(self, *, data: dict[str, Any]) -> dict[str, Any]:
        """Accept one idempotent Console message and claim it when the thread is idle."""

        now = data["created_at"]
        with _write_transaction(self.engine) as connection:
            orm = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            try:
                session = orm.get(LiveSessionCatalog, data["session_id"])
                if session is None or not session.primary_thread_id:
                    orm.rollback()
                    return {"found": False}
                if not self._session_belongs_to_owner(
                    connection,
                    session_id=str(data["session_id"]),
                    owner_id=int(data["owner_id"]),
                ):
                    orm.rollback()
                    return {"found": False}
                thread = orm.get(LiveSessionThread, str(session.primary_thread_id))
                if thread is None or not thread.device_id or not thread.cwd:
                    orm.rollback()
                    return {"found": True, "unavailable": "execution_target_missing"}
                existing_receipt = (
                    orm.query(LiveSessionInputReceipt)
                    .filter(
                        LiveSessionInputReceipt.owner_id == data["owner_id"],
                        LiveSessionInputReceipt.session_id == data["session_id"],
                        LiveSessionInputReceipt.client_request_id == data["client_request_id"],
                    )
                    .one_or_none()
                )
                if existing_receipt is not None:
                    turn = orm.query(LiveConsoleTurn).filter(LiveConsoleTurn.receipt_id == existing_receipt.id).one()
                    exact = existing_receipt.text == data["message"]
                    replay_turn = _live_console_turn_dto(
                        turn,
                        message=existing_receipt.text,
                        client_request_id=existing_receipt.client_request_id,
                        provider_config=thread.provider_config_json,
                    )
                    orm.rollback()
                    return {
                        "found": True,
                        "created": False,
                        "idempotency_conflict": not exact,
                        "turn": replay_turn if exact else None,
                    }
                receipt_id = str(uuid4())
                turn_id = str(uuid4())
                resume_alias = (
                    orm.query(LiveSessionThreadAlias)
                    .filter(
                        LiveSessionThreadAlias.thread_id == thread.id,
                        LiveSessionThreadAlias.provider == session.provider,
                        LiveSessionThreadAlias.alias_kind == "provider_session_id",
                    )
                    .order_by(LiveSessionThreadAlias.last_seen_at.desc())
                    .first()
                )
                receipt = LiveSessionInputReceipt(
                    id=receipt_id,
                    owner_id=data["owner_id"],
                    session_id=data["session_id"],
                    thread_id=thread.id,
                    provider=session.provider,
                    device_id=thread.device_id,
                    client_request_id=data["client_request_id"],
                    intent="auto",
                    status="queued",
                    text=data["message"],
                    created_at=now,
                    updated_at=now,
                )
                turn = LiveConsoleTurn(
                    id=turn_id,
                    session_id=data["session_id"],
                    thread_id=thread.id,
                    receipt_id=receipt_id,
                    state="queued",
                    provider=session.provider,
                    device_id=thread.device_id,
                    cwd=thread.cwd,
                    resume_provider_thread_id=resume_alias.alias_value if resume_alias is not None else None,
                    created_at=now,
                    updated_at=now,
                )
                orm.add_all([receipt, turn])
                owner = (
                    orm.query(LiveConsoleTurn.id)
                    .filter(
                        LiveConsoleTurn.thread_id == thread.id,
                        LiveConsoleTurn.state.in_(("starting", "active", "draining")),
                    )
                    .first()
                )
                if owner is None:
                    run_id = str(uuid4())
                    turn.run_id = run_id
                    turn.state = "starting"
                    receipt.status = "delivering"
                    receipt.delivery_request_id = run_id
                    orm.add(
                        LiveSessionRun(
                            id=run_id,
                            thread_id=thread.id,
                            provider=session.provider,
                            host_id=thread.device_id,
                            cwd=thread.cwd,
                            launch_origin="longhouse_spawned",
                            started_at=now,
                        )
                    )
                orm.commit()
                result = _live_console_turn_dto(
                    turn,
                    message=receipt.text,
                    client_request_id=receipt.client_request_id,
                    provider_config=thread.provider_config_json,
                )
            except BaseException:
                orm.rollback()
                raise
            finally:
                orm.close()
            commit_seq = _advance_commit_seq(connection, now)
            return {
                "found": True,
                "created": True,
                "idempotency_conflict": False,
                "turn": result,
                "commit_seq": str(commit_seq),
            }

    def update_console_turn(self, *, data: dict[str, Any]) -> dict[str, Any]:
        now = data["updated_at"]
        with _write_transaction(self.engine) as connection:
            orm = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            try:
                turn = orm.query(LiveConsoleTurn).filter(LiveConsoleTurn.run_id == data["run_id"]).one_or_none()
                if turn is None or (data.get("turn_id") and turn.id != data["turn_id"]):
                    orm.rollback()
                    return {"found": False}
                receipt = orm.get(LiveSessionInputReceipt, turn.receipt_id)
                next_state = data["state"]
                turn.state = next_state
                turn.updated_at = now
                turn.error = data.get("error")
                if receipt is not None:
                    receipt.status = "delivered" if next_state == "active" else ("failed" if next_state == "failed" else receipt.status)
                    receipt.error_json = json.dumps({"message": data["error"]}) if data.get("error") else None
                    receipt.updated_at = now
                next_turn_result = None
                if next_state in {"completed", "failed", "cancelled"}:
                    turn.terminal_at = now
                    run = orm.get(LiveSessionRun, turn.run_id)
                    if run is not None:
                        run.ended_at = now
                        run.exit_status = next_state
                    next_turn = (
                        orm.query(LiveConsoleTurn)
                        .filter(LiveConsoleTurn.thread_id == turn.thread_id, LiveConsoleTurn.state == "queued")
                        .order_by(LiveConsoleTurn.created_at.asc(), LiveConsoleTurn.id.asc())
                        .first()
                    )
                    if next_turn is not None:
                        next_receipt = orm.get(LiveSessionInputReceipt, next_turn.receipt_id)
                        thread = orm.get(LiveSessionThread, next_turn.thread_id)
                        resume_alias = (
                            orm.query(LiveSessionThreadAlias)
                            .filter(
                                LiveSessionThreadAlias.thread_id == next_turn.thread_id,
                                LiveSessionThreadAlias.provider == next_turn.provider,
                                LiveSessionThreadAlias.alias_kind == "provider_session_id",
                            )
                            .order_by(LiveSessionThreadAlias.last_seen_at.desc())
                            .first()
                        )
                        next_run_id = str(uuid4())
                        next_turn.run_id = next_run_id
                        next_turn.state = "starting"
                        next_turn.resume_provider_thread_id = resume_alias.alias_value if resume_alias is not None else None
                        next_turn.updated_at = now
                        if next_receipt is not None:
                            next_receipt.status = "delivering"
                            next_receipt.delivery_request_id = next_run_id
                            next_receipt.updated_at = now
                        orm.add(
                            LiveSessionRun(
                                id=next_run_id,
                                thread_id=next_turn.thread_id,
                                provider=next_turn.provider,
                                host_id=next_turn.device_id,
                                cwd=next_turn.cwd,
                                launch_origin="longhouse_spawned",
                                started_at=now,
                            )
                        )
                        next_turn_result = _live_console_turn_dto(
                            next_turn,
                            message=next_receipt.text if next_receipt is not None else None,
                            client_request_id=next_receipt.client_request_id if next_receipt is not None else None,
                            provider_config=thread.provider_config_json if thread is not None else None,
                        )
                orm.commit()
                result = _live_console_turn_dto(
                    turn,
                    client_request_id=receipt.client_request_id if receipt is not None else None,
                )
            except BaseException:
                orm.rollback()
                raise
            finally:
                orm.close()
            commit_seq = _advance_commit_seq(connection, now)
            return {"found": True, "turn": result, "next_turn": next_turn_result, "commit_seq": str(commit_seq)}

    def create_local_launch(self, *, launch: dict[str, Any]) -> dict[str, Any]:
        """Atomically create a Helm launch shell, control attachment, and outbox row."""

        from zerg.services.agents.session_graph_writes import primary_thread_id_for_session
        from zerg.services.live_archive_outbox import enqueue_managed_local_launch_outbox
        from zerg.services.live_archive_outbox import managed_local_launch_idempotency_key
        from zerg.services.live_catalog_launch import attach_live_catalog_control
        from zerg.services.live_catalog_launch import create_live_launch_catalog_shell
        from zerg.services.live_catalog_launch import live_launch_result
        from zerg.services.live_launch_readiness import upsert_live_launch_readiness
        from zerg.services.managed_local_launcher import managed_local_run_id_for_session
        from zerg.services.managed_local_launcher import managed_provider_has_lease_observer

        plan_payload = dict(launch["plan"])
        session_id = UUID(plan_payload["session_id"])
        plan = SimpleNamespace(**{**plan_payload, "session_id": session_id})
        command_id = f"managed-local-{session_id}"
        observed_at = launch["started_at"]
        thread_id = primary_thread_id_for_session(session_id)
        run_id = managed_local_run_id_for_session(session_id)
        with _write_transaction(self.engine) as connection:
            orm = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            try:
                existing = orm.query(LiveSessionLaunchAttempt).filter(LiveSessionLaunchAttempt.command_id == command_id).one_or_none()
                if existing is not None:
                    catalog = orm.get(LiveSessionCatalog, str(session_id))
                    outbox = (
                        orm.query(LiveArchiveOutbox)
                        .filter(LiveArchiveOutbox.idempotency_key == managed_local_launch_idempotency_key(session_id=session_id))
                        .one_or_none()
                    )
                    stored_launch = json.loads(outbox.payload_json or "{}").get("launch", {}) if outbox is not None else {}
                    expected_plan = {**plan_payload, "session_id": str(session_id)}
                    exact_replay = (
                        str(existing.session_id) == str(session_id)
                        and str(existing.thread_id or "") == str(thread_id)
                        and existing.owner_id == launch["owner_id"]
                        and str(existing.provider) == plan.provider
                        and str(existing.host_id or "") == plan.source_name
                        and catalog is not None
                        and str(catalog.cwd or "") == plan.cwd
                        and str(catalog.project or "") == plan.project
                        and str(catalog.git_repo or "") == str(launch.get("git_repo") or "")
                        and str(catalog.git_branch or "") == str(launch.get("git_branch") or "")
                        and str(catalog.loop_mode or "") == plan.loop_mode
                        and str(catalog.permission_mode or "") == plan.permission_mode
                        and stored_launch.get("owner_id") == launch["owner_id"]
                        and stored_launch.get("git_repo") == launch.get("git_repo")
                        and stored_launch.get("git_branch") == launch.get("git_branch")
                        and stored_launch.get("plan") == expected_plan
                    )
                    result = live_launch_result(existing) if exact_replay else None
                    orm.rollback()
                    return {
                        "created": False,
                        "exact_replay": exact_replay,
                        "idempotency_conflict": not exact_replay,
                        "launch": _json_launch_result(result) if result is not None else None,
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                attempt = create_live_launch_catalog_shell(
                    orm,
                    session_id=session_id,
                    thread_id=thread_id,
                    run_id=None,
                    owner_id=launch["owner_id"],
                    provider=plan.provider,
                    device_id=plan.source_name,
                    device_name=plan.source_name,
                    cwd=plan.cwd,
                    project=plan.project,
                    git_repo=launch.get("git_repo"),
                    git_branch=launch.get("git_branch"),
                    display_name=plan.display_name,
                    initial_prompt=None,
                    execution_lifetime="live_control",
                    client_request_id=None,
                    command_id=command_id,
                    started_at=observed_at,
                    expires_at=launch["expires_at"],
                    launch_actor=plan.launch_actor,
                    launch_surface=plan.launch_surface,
                )
                attach_live_catalog_control(
                    orm,
                    session_id=session_id,
                    provider=plan.provider,
                    device_id=plan.source_name,
                    state="detached" if managed_provider_has_lease_observer(plan.provider) else "attached",
                    external_name=plan.managed_session_name,
                    run_id=run_id,
                    provider_session_id=plan.provider_session_id,
                    observed_at=observed_at,
                )
                upsert_live_launch_readiness(
                    orm,
                    session_id=session_id,
                    owner_id=launch["owner_id"],
                    device_id=plan.source_name,
                    provider=plan.provider,
                    execution_lifetime="live_control",
                    state="pending",
                    command_id=command_id,
                    client_request_id=None,
                    machine_id=plan.source_name,
                    project=plan.project,
                    expires_at=launch["expires_at"],
                    now=observed_at,
                )
                enqueue_managed_local_launch_outbox(
                    orm,
                    plan=plan,
                    owner_id=launch["owner_id"],
                    git_repo=launch.get("git_repo"),
                    git_branch=launch.get("git_branch"),
                    started_at=observed_at,
                    completed=True,
                )
                result = live_launch_result(attempt)
                orm.commit()
            except BaseException:
                orm.rollback()
                raise
            finally:
                orm.close()
            commit_seq = _advance_commit_seq(connection, observed_at)
            return {
                "created": True,
                "exact_replay": False,
                "idempotency_conflict": False,
                "launch": _json_launch_result(result),
                "commit_seq": str(commit_seq),
            }

    def create_continue_intent(self, *, launch: dict[str, Any]) -> dict[str, Any]:
        """Reserve a continuation run without materializing it before adoption."""

        from zerg.services.live_archive_outbox import enqueue_remote_launch_outbox
        from zerg.services.live_archive_outbox import remote_launch_idempotency_key
        from zerg.services.live_catalog_launch import live_launch_result
        from zerg.services.live_launch_readiness import upsert_live_launch_readiness

        observed_at = launch["started_at"]
        with _write_transaction(self.engine) as connection:
            orm = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            try:
                existing = (
                    orm.query(LiveSessionLaunchAttempt).filter(LiveSessionLaunchAttempt.command_id == launch["command_id"]).one_or_none()
                )
                if existing is not None:
                    outbox = (
                        orm.query(LiveArchiveOutbox)
                        .filter(LiveArchiveOutbox.idempotency_key == remote_launch_idempotency_key(session_id=launch["session_id"]))
                        .one_or_none()
                    )
                    stored_launch = json.loads(outbox.payload_json or "{}").get("launch") if outbox is not None else None
                    exact_replay = (
                        str(existing.session_id) == launch["session_id"]
                        and str(existing.thread_id or "") == launch["primary_thread_id"]
                        and str(existing.run_id or "") == launch["run_id"]
                        and existing.owner_id == launch["owner_id"]
                        and stored_launch == _canonical_outbox_value(launch)
                    )
                    result = live_launch_result(existing) if exact_replay else None
                    orm.rollback()
                    return {
                        "created": False,
                        "exact_replay": exact_replay,
                        "idempotency_conflict": not exact_replay,
                        "launch": _json_launch_result(result) if result is not None else None,
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                catalog = orm.get(LiveSessionCatalog, launch["session_id"])
                if catalog is None or str(catalog.primary_thread_id or "") != launch["primary_thread_id"]:
                    orm.rollback()
                    return {
                        "created": False,
                        "exact_replay": False,
                        "idempotency_conflict": True,
                        "launch": None,
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                attempt = LiveSessionLaunchAttempt(
                    session_id=launch["session_id"],
                    thread_id=launch["primary_thread_id"],
                    run_id=launch["run_id"],
                    provider=launch["provider"],
                    host_id=launch["device_id"],
                    owner_id=launch["owner_id"],
                    execution_lifetime=launch["execution_lifetime"],
                    client_request_id=launch.get("client_request_id"),
                    command_id=launch["command_id"],
                    state="pending",
                    expires_at=launch["expires_at"],
                    created_at=observed_at,
                    updated_at=observed_at,
                )
                orm.add(attempt)
                upsert_live_launch_readiness(
                    orm,
                    session_id=UUID(launch["session_id"]),
                    owner_id=launch["owner_id"],
                    device_id=launch["device_id"],
                    provider=launch["provider"],
                    execution_lifetime=launch["execution_lifetime"],
                    state="pending",
                    command_id=launch["command_id"],
                    client_request_id=launch.get("client_request_id"),
                    machine_id=launch["machine_id"],
                    project=launch["project"],
                    expires_at=launch["expires_at"],
                    now=observed_at,
                )
                enqueue_remote_launch_outbox(orm, launch=launch, completed=True)
                orm.flush()
                result = live_launch_result(attempt)
                orm.commit()
            except BaseException:
                orm.rollback()
                raise
            finally:
                orm.close()
            commit_seq = _advance_commit_seq(connection, observed_at)
            return {
                "created": True,
                "exact_replay": False,
                "idempotency_conflict": False,
                "launch": _json_launch_result(result),
                "commit_seq": str(commit_seq),
            }

    def apply_launch_outcome(
        self,
        *,
        launch: dict[str, Any],
        outcome: dict[str, Any],
    ) -> dict[str, Any]:
        """Atomically project transport outcome and queue its archive record."""

        from zerg.services.live_archive_outbox import enqueue_remote_launch_outcome_outbox
        from zerg.services.live_archive_outbox import remote_launch_outcome_idempotency_key
        from zerg.services.live_catalog_launch import attach_live_catalog_control
        from zerg.services.live_catalog_launch import live_launch_result
        from zerg.services.live_catalog_launch import update_live_launch_catalog_outcome
        from zerg.services.live_launch_readiness import update_live_launch_readiness_state

        observed_at = datetime.now(UTC)
        with _write_transaction(self.engine) as connection:
            orm = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            try:
                existing = (
                    orm.query(LiveSessionLaunchAttempt).filter(LiveSessionLaunchAttempt.command_id == launch["command_id"]).one_or_none()
                )
                if existing is None or str(existing.session_id) != launch["session_id"]:
                    orm.rollback()
                    return {
                        "found": False,
                        "changed": False,
                        "launch": None,
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                outcome_key = f"{remote_launch_outcome_idempotency_key(session_id=launch['session_id'])}:{outcome['state']}"
                outcome_outbox = orm.query(LiveArchiveOutbox).filter(LiveArchiveOutbox.idempotency_key == outcome_key).one_or_none()
                stored_outcome = json.loads(outcome_outbox.payload_json or "{}").get("outcome") if outcome_outbox is not None else None
                exact_replay = (
                    str(existing.state) == outcome["state"]
                    and str(existing.error_code or "") == str(outcome.get("error_code") or "")
                    and str(existing.error_message or "") == str(outcome.get("error_message") or "")
                    and stored_outcome == _canonical_outbox_value(outcome)
                )
                if exact_replay:
                    result = live_launch_result(existing)
                    orm.rollback()
                    return {
                        "found": True,
                        "changed": False,
                        "launch": _json_launch_result(result),
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                if str(existing.state) in {"adopted", "failed", "abandoned"} and outcome["state"] == "dispatched":
                    result = live_launch_result(existing)
                    orm.rollback()
                    return {
                        "found": True,
                        "changed": False,
                        "launch": _json_launch_result(result),
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                attempt = update_live_launch_catalog_outcome(
                    orm,
                    session_id=UUID(launch["session_id"]),
                    command_id=launch["command_id"],
                    state=outcome["state"],
                    error_code=outcome.get("error_code"),
                    error_message=outcome.get("error_message"),
                    now=observed_at,
                )
                update_live_launch_readiness_state(
                    orm,
                    session_id=UUID(launch["session_id"]),
                    state=outcome["state"],
                    error_code=outcome.get("error_code"),
                    error_message=outcome.get("error_message"),
                    clear_expires=outcome["state"] in {"adopted", "failed", "abandoned"},
                    now=observed_at,
                )
                if launch.get("mode") == "continue" and outcome["state"] == "adopted":
                    resume = launch["resume"]
                    attach_live_catalog_control(
                        orm,
                        session_id=UUID(launch["session_id"]),
                        provider=launch["provider"],
                        device_id=launch["device_id"],
                        state="attached",
                        external_name=outcome.get("external_name") or launch.get("machine_id"),
                        run_id=UUID(launch["run_id"]),
                        provider_session_id=outcome.get("provider_thread_id") or resume["thread_id"],
                        source_path=outcome.get("thread_path") or resume.get("thread_path"),
                        launch_origin="longhouse_continued",
                        force_new_run=True,
                        observed_at=observed_at,
                    )
                enqueue_remote_launch_outcome_outbox(orm, launch=launch, outcome=outcome, completed=True)
                result = live_launch_result(attempt)
                orm.commit()
            except BaseException:
                orm.rollback()
                raise
            finally:
                orm.close()
            commit_seq = _advance_commit_seq(connection, observed_at)
            return {
                "found": True,
                "changed": True,
                "launch": _json_launch_result(result),
                "commit_seq": str(commit_seq),
            }

    def list_queued_input_sessions(self, *, limit: int) -> dict[str, Any]:
        """Return a bounded set of sessions with queued hot input."""

        from zerg.services.live_session_inputs import list_session_ids_with_queued_live_receipts

        with _read_snapshot(self.engine) as connection:
            orm = Session(bind=connection, expire_on_commit=False)
            try:
                session_ids = list_session_ids_with_queued_live_receipts(orm, limit=limit)
            finally:
                orm.close()
            return {
                "session_ids": [str(session_id) for session_id in session_ids],
                "commit_seq": str(_current_commit_seq(connection)),
            }

    def claim_queued_input(
        self,
        *,
        session_id: str,
        delivery_request_id: str,
    ) -> dict[str, Any]:
        """Check drainability and claim exactly one queued input receipt."""

        from zerg.services.live_control_catalog import _QUEUE_DRAINABLE_PHASES
        from zerg.services.live_control_catalog import load_live_control_session
        from zerg.services.live_session_inputs import _snapshot
        from zerg.services.live_session_inputs import claim_next_live_queued_receipt

        observed_at = datetime.now(UTC)
        with _write_transaction(self.engine) as connection:
            orm = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            try:
                session = load_live_control_session(orm, session_id)
                if session is None:
                    orm.rollback()
                    return {
                        "claimed": False,
                        "reason": "session_not_found",
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                runtime = (
                    orm.query(LiveRuntimeState)
                    .filter(LiveRuntimeState.session_id == session.id)
                    .order_by(LiveRuntimeState.updated_at.desc(), LiveRuntimeState.runtime_version.desc())
                    .first()
                )
                if runtime is None or str(runtime.phase or "").strip() not in _QUEUE_DRAINABLE_PHASES:
                    orm.rollback()
                    return {
                        "claimed": False,
                        "reason": "runtime_not_drainable",
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                replay_row = (
                    orm.query(LiveSessionInputReceipt)
                    .filter(
                        LiveSessionInputReceipt.session_id == session_id,
                        LiveSessionInputReceipt.delivery_request_id == delivery_request_id,
                        LiveSessionInputReceipt.status == "delivering",
                    )
                    .first()
                )
                if replay_row is not None:
                    snapshot = _snapshot(replay_row)
                    orm.rollback()
                    return {
                        "claimed": True,
                        "exact_replay": True,
                        "session": _live_control_session_dto(session),
                        "receipt": _input_receipt_dto(snapshot),
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                receipt = claim_next_live_queued_receipt(
                    orm,
                    session_id=session_id,
                    delivery_request_id=delivery_request_id,
                )
                if receipt is None:
                    orm.rollback()
                    return {
                        "claimed": False,
                        "reason": "queue_empty",
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
            except BaseException:
                orm.rollback()
                raise
            finally:
                orm.close()
            commit_seq = _advance_commit_seq(connection, observed_at)
            return {
                "claimed": True,
                "exact_replay": False,
                "session": _live_control_session_dto(session),
                "receipt": _input_receipt_dto(receipt),
                "commit_seq": str(commit_seq),
            }

    def finish_queued_input(
        self,
        *,
        receipt_id: str,
        delivery_request_id: str,
        status: str,
        error: str | None,
    ) -> dict[str, Any]:
        """Apply the terminal delivery result and archive projection atomically."""

        from zerg.services.live_session_inputs import _snapshot
        from zerg.services.live_session_inputs import mark_live_receipt_delivered_with_projection
        from zerg.services.live_session_inputs import mark_live_receipt_failed

        observed_at = datetime.now(UTC)
        with _write_transaction(self.engine) as connection:
            orm = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            try:
                row = orm.get(LiveSessionInputReceipt, receipt_id)
                if row is None or str(row.delivery_request_id or "") != delivery_request_id:
                    orm.rollback()
                    return {
                        "found": False,
                        "changed": False,
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                if str(row.status) == status:
                    snapshot = _snapshot(row)
                    orm.rollback()
                    return {
                        "found": True,
                        "changed": False,
                        "receipt": _input_receipt_dto(snapshot),
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                if str(row.status) != "delivering":
                    orm.rollback()
                    return {
                        "found": True,
                        "changed": False,
                        "reason": "receipt_not_delivering",
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                if status == "delivered":
                    snapshot = mark_live_receipt_delivered_with_projection(
                        orm,
                        receipt_id=receipt_id,
                        delivery_request_id=delivery_request_id,
                    )
                else:
                    snapshot = mark_live_receipt_failed(
                        orm,
                        receipt_id=receipt_id,
                        error=error or "session input delivery failed",
                    )
                if snapshot is None:
                    raise RuntimeError("claimed input receipt disappeared during finish")
            except BaseException:
                orm.rollback()
                raise
            finally:
                orm.close()
            commit_seq = _advance_commit_seq(connection, observed_at)
            return {
                "found": True,
                "changed": True,
                "receipt": _input_receipt_dto(snapshot),
                "commit_seq": str(commit_seq),
            }

    def upsert_input_receipt(self, *, receipt: dict[str, Any]) -> dict[str, Any]:
        """Persist one idempotent live input receipt and optional archive projection."""

        from zerg.services.live_session_inputs import _record_live_input_receipt
        from zerg.services.live_session_inputs import load_live_input_receipt_by_id

        observed_at = datetime.now(UTC)
        with _write_transaction(self.engine) as connection:
            orm = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            try:
                receipt_id = _record_live_input_receipt(
                    orm,
                    owner_id=receipt["owner_id"],
                    session_id=receipt["session_id"],
                    provider=receipt["provider"],
                    text=receipt["text"],
                    intent=receipt["intent"],
                    status=receipt["status"],
                    client_request_id=receipt.get("client_request_id"),
                    device_id=receipt.get("device_id"),
                    thread_id=receipt.get("thread_id"),
                    archive_session_input_id=receipt.get("archive_session_input_id"),
                    control_command_id=receipt.get("control_command_id"),
                    delivery_request_id=receipt.get("delivery_request_id"),
                    enqueue_archive_projection=receipt["enqueue_archive_projection"],
                    error=receipt.get("error"),
                    expires_at=receipt.get("expires_at"),
                )
                orm.commit()
                snapshot = load_live_input_receipt_by_id(orm, receipt_id=receipt_id)
                if snapshot is None:
                    raise RuntimeError("input receipt disappeared after upsert")
            except BaseException:
                orm.rollback()
                raise
            finally:
                orm.close()
            commit_seq = _advance_commit_seq(connection, observed_at)
            return {
                "receipt": _input_receipt_dto(snapshot),
                "commit_seq": str(commit_seq),
            }

    def create_input_attachment(self, *, attachment: dict[str, Any]) -> dict[str, Any]:
        """Create bounded attachment metadata under an existing live receipt."""

        observed_at = datetime.now(UTC)
        table = LiveSessionInputAttachment.__table__
        receipt_table = LiveSessionInputReceipt.__table__
        with _write_transaction(self.engine) as connection:
            pruned_blob_paths = list(connection.execute(select(table.c.blob_path).where(table.c.expires_at <= observed_at)).scalars())
            connection.execute(delete(table).where(table.c.expires_at <= observed_at))
            receipt = connection.execute(
                select(receipt_table.c.id).where(
                    receipt_table.c.id == attachment["input_receipt_id"],
                    receipt_table.c.owner_id == attachment["owner_id"],
                    receipt_table.c.session_id == attachment["session_id"],
                )
            ).first()
            if receipt is None:
                return {
                    "created": False,
                    "reason": "input_receipt_not_found",
                    "pruned_blob_paths": pruned_blob_paths,
                    "commit_seq": str(_current_commit_seq(connection)),
                }
            existing = connection.execute(select(table).where(table.c.id == attachment["id"])).mappings().first()
            values = {**attachment, "created_at": observed_at}
            if existing is not None:
                comparable = {key: existing[key] for key in attachment}
                if comparable != attachment:
                    return {
                        "created": False,
                        "reason": "idempotency_conflict",
                        "pruned_blob_paths": pruned_blob_paths,
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                return {
                    "created": False,
                    "attachment": _input_attachment_dto(SimpleNamespace(**existing)),
                    "pruned_blob_paths": pruned_blob_paths,
                    "commit_seq": str(_current_commit_seq(connection)),
                }
            connection.execute(insert(table).values(**values))
            row = connection.execute(select(table).where(table.c.id == attachment["id"])).mappings().one()
            commit_seq = _advance_commit_seq(connection, observed_at)
            return {
                "created": True,
                "attachment": _input_attachment_dto(SimpleNamespace(**row)),
                "pruned_blob_paths": pruned_blob_paths,
                "commit_seq": str(commit_seq),
            }

    def read_input_attachment(
        self,
        *,
        owner_id: int,
        session_id: str,
        input_receipt_id: str,
        attachment_id: str,
    ) -> dict[str, Any]:
        """Read one unexpired attachment through its full ownership boundary."""

        table = LiveSessionInputAttachment.__table__
        observed_at = datetime.now(UTC)
        with _read_snapshot(self.engine) as connection:
            row = (
                connection.execute(
                    select(table).where(
                        table.c.id == attachment_id,
                        table.c.input_receipt_id == input_receipt_id,
                        table.c.owner_id == owner_id,
                        table.c.session_id == session_id,
                        table.c.expires_at > observed_at,
                    )
                )
                .mappings()
                .first()
            )
            return {
                "found": row is not None,
                "attachment": _input_attachment_dto(SimpleNamespace(**row)) if row is not None else None,
                "commit_seq": str(_current_commit_seq(connection)),
            }

    def read_input_receipt(
        self,
        *,
        owner_id: int,
        session_id: str,
        client_request_id: str,
    ) -> dict[str, Any]:
        """Read one public input idempotency receipt."""

        from zerg.services.live_session_inputs import get_live_input_receipt_by_client_request

        with _read_snapshot(self.engine) as connection:
            orm = Session(bind=connection, expire_on_commit=False)
            try:
                receipt = get_live_input_receipt_by_client_request(
                    orm,
                    owner_id=owner_id,
                    session_id=session_id,
                    client_request_id=client_request_id,
                )
            finally:
                orm.close()
            return {
                "found": receipt is not None,
                "receipt": _input_receipt_dto(receipt) if receipt is not None else None,
                "commit_seq": str(_current_commit_seq(connection)),
            }

    def list_recent_input_receipts(self, *, session_id: str) -> dict[str, Any]:
        """Return bounded queued/delivering/recent-failed receipts for UI state."""

        from zerg.services.live_session_inputs import count_live_queued_receipts
        from zerg.services.live_session_inputs import list_recent_live_input_receipts

        with _read_snapshot(self.engine) as connection:
            orm = Session(bind=connection, expire_on_commit=False)
            try:
                receipts = list_recent_live_input_receipts(orm, session_id=session_id)
                queued_count = count_live_queued_receipts(orm, session_id=session_id)
            finally:
                orm.close()
            return {
                "receipts": [_input_receipt_dto(receipt) for receipt in receipts],
                "queued_count": queued_count,
                "commit_seq": str(_current_commit_seq(connection)),
            }

    def cancel_input_receipt(self, *, session_id: str, receipt_id: str) -> dict[str, Any]:
        """Cancel one still-queued receipt through catalogd's writer."""

        from zerg.services.live_session_inputs import cancel_live_queued_receipt

        observed_at = datetime.now(UTC)
        with _write_transaction(self.engine) as connection:
            orm = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            try:
                receipt = cancel_live_queued_receipt(orm, session_id=session_id, receipt_id=receipt_id)
                if receipt is None:
                    orm.rollback()
                    return {
                        "cancelled": False,
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
            except BaseException:
                orm.rollback()
                raise
            finally:
                orm.close()
            commit_seq = _advance_commit_seq(connection, observed_at)
            return {
                "cancelled": True,
                "receipt": _input_receipt_dto(receipt),
                "commit_seq": str(commit_seq),
            }

    def list_session_timeline(
        self,
        *,
        project: str | None,
        provider: str | None,
        environment: str | None,
        include_test: bool,
        hide_autonomous: bool,
        include_automation: bool,
        device_id: str | None,
        days_back: int,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        """Return one bounded timeline page and all raw facts in one snapshot."""

        observed_at = datetime.now(UTC)
        since = observed_at - timedelta(days=days_back)
        card = LiveTimelineCard.__table__
        catalog = LiveSessionCatalog.__table__
        storage = StorageSession.__table__
        tombstones = LiveSessionTombstone.__table__
        with _read_snapshot(self.engine) as connection:
            legacy_where = [
                func.coalesce(card.c.last_activity_at, card.c.started_at) >= since,
                ~select(storage.c.session_id).where(storage.c.session_id == card.c.session_id).exists(),
            ]
            storage_where = [
                storage.c.last_activity_at >= since,
                storage.c.hidden_from_default_timeline == 0,
                storage.c.user_state != "deleted",
                ~select(tombstones.c.session_id).where(tombstones.c.session_id == storage.c.session_id).exists(),
            ]
            if project is not None:
                legacy_where.append(card.c.project == project)
                storage_where.append(storage.c.project == project)
            if provider is not None:
                legacy_where.append(card.c.provider == provider)
                storage_where.append(storage.c.provider == provider)
            if environment is not None:
                legacy_where.append(card.c.environment == environment)
                storage_where.append(storage.c.environment == environment)
            elif not include_test:
                legacy_where.append(card.c.environment.notin_(("test", "e2e")))
                storage_where.append(storage.c.environment.notin_(("test", "e2e")))
            if device_id is not None:
                legacy_where.append(card.c.device_id == device_id)
                storage_where.append(storage.c.machine_id == device_id)
            if hide_autonomous:
                legacy_where.append(
                    or_(
                        card.c.user_messages > 0,
                        card.c.archive_state == "pending",
                        card.c.launch_actor == "human_ui",
                        card.c.launch_surface.in_(("web", "ios", "api")),
                    )
                )
                storage_where.append(
                    or_(
                        storage.c.user_messages > 0,
                        storage.c.launch_actor == "human_ui",
                        storage.c.launch_surface.in_(("web", "ios", "api")),
                    )
                )
            if not include_automation:
                legacy_where.append(or_(card.c.origin_kind.is_(None), card.c.origin_kind != "hatch_automation"))
                storage_where.append(or_(storage.c.origin_kind.is_(None), storage.c.origin_kind != "hatch_automation"))

            joined = card.join(catalog, catalog.c.session_id == card.c.session_id)
            candidates = union_all(
                select(
                    card.c.session_id.label("session_id"),
                    func.coalesce(card.c.last_activity_at, card.c.started_at).label("order_at"),
                )
                .select_from(joined)
                .where(*legacy_where),
                select(storage.c.session_id.label("session_id"), storage.c.last_activity_at.label("order_at")).where(*storage_where),
            ).subquery()
            total = int(connection.execute(select(func.count()).select_from(candidates)).scalar_one())
            session_ids = [
                str(value)
                for value in connection.execute(
                    select(candidates.c.session_id)
                    .order_by(candidates.c.order_at.desc(), candidates.c.session_id.desc())
                    .limit(limit)
                    .offset(offset)
                ).scalars()
            ]
            facts = _assemble_session_facts(
                connection,
                session_ids=session_ids,
                observed_at=observed_at,
                compact=True,
            )
            has_real_sessions = total == 0 or any((item["catalog"].get("device_id") or "") != "demo-mac" for item in facts)
            return {
                "commit_seq": str(_current_commit_seq(connection)),
                "observed_at": observed_at.isoformat(),
                "rows": [
                    {
                        "thread_id": item["primary_thread"]["id"] if item["primary_thread"] is not None else None,
                        "facts": item,
                    }
                    for item in facts
                ],
                "total": total,
                "has_real_sessions": has_real_sessions,
            }

    def read_session(self, *, session_id: str, owner_id: int | None = None) -> dict[str, Any]:
        observed_at = datetime.now(UTC)
        with _read_snapshot(self.engine) as connection:
            if owner_id is not None and not self._session_belongs_to_owner(
                connection,
                session_id=session_id,
                owner_id=owner_id,
            ):
                return {
                    "commit_seq": str(_current_commit_seq(connection)),
                    "observed_at": observed_at.isoformat(),
                    "found": False,
                    "facts": None,
                }
            facts = _assemble_session_facts(
                connection,
                session_ids=[session_id],
                observed_at=observed_at,
                compact=False,
            )
            return {
                "commit_seq": str(_current_commit_seq(connection)),
                "observed_at": observed_at.isoformat(),
                "found": bool(facts),
                "facts": facts[0] if facts else None,
            }

    def read_sessions(self, *, session_ids: list[str]) -> dict[str, Any]:
        observed_at = datetime.now(UTC)
        with _read_snapshot(self.engine) as connection:
            facts = _assemble_session_facts(
                connection,
                session_ids=session_ids,
                observed_at=observed_at,
                compact=False,
            )
            return {
                "commit_seq": str(_current_commit_seq(connection)),
                "observed_at": observed_at.isoformat(),
                "facts": facts,
            }

    @staticmethod
    def _session_belongs_to_owner(connection: Any, *, session_id: str, owner_id: int) -> bool:
        """Validate catalog session ownership, including single-tenant legacy cards."""

        owner_exists = connection.execute(
            select(LiveUser.id).where(LiveUser.id == owner_id, LiveUser.is_active.is_(True))
        ).scalar_one_or_none()
        if owner_exists is None:
            return False

        owner_text = str(owner_id)
        live_owner = connection.execute(select(LiveSession.owner_id).where(LiveSession.session_id == session_id)).scalar_one_or_none()
        if live_owner is not None:
            return str(live_owner) == owner_text

        storage_owner = connection.execute(
            select(StorageSession.owner_id).where(StorageSession.session_id == session_id)
        ).scalar_one_or_none()
        if storage_owner is not None:
            return str(storage_owner) == owner_text

        # Legacy live cards predate per-session ownership. They are still
        # unambiguously owned by the one active tenant in this catalog.
        catalog_row = connection.execute(
            select(LiveSessionCatalog.session_id, LiveSessionCatalog.origin_kind).where(LiveSessionCatalog.session_id == session_id)
        ).one_or_none()
        if catalog_row is None:
            return False
        if catalog_row.origin_kind == "console":
            outbox_owner = connection.execute(
                select(func.json_extract(LiveArchiveOutbox.payload_json, "$.session.owner_id")).where(
                    LiveArchiveOutbox.idempotency_key == f"console_session_create.v1:{session_id}"
                )
            ).scalar_one_or_none()
            return outbox_owner is not None and str(outbox_owner) == owner_text
        return True

    def create_session_message(
        self,
        *,
        message_key: str,
        owner_id: int,
        from_session_id: str,
        to_session_id: str,
        text: str,
        source_event_id: int | None,
        created_at: datetime,
    ) -> dict[str, Any]:
        table = LiveSessionMessage.__table__
        with _write_transaction(self.engine) as connection:
            existing = connection.execute(select(table).where(table.c.message_key == message_key)).mappings().first()
            if existing is not None:
                exact = (
                    int(existing["owner_id"]) == owner_id
                    and str(existing["from_session_id"]) == from_session_id
                    and str(existing["to_session_id"]) == to_session_id
                    and str(existing["text"]) == text
                    and existing["source_event_id"] == source_event_id
                    and _as_aware_utc(existing["created_at"]) == created_at
                )
                return {
                    "created": False,
                    "idempotency_conflict": not exact,
                    "message": _session_message_dto(SimpleNamespace(**existing)) if exact else None,
                    "commit_seq": str(_current_commit_seq(connection)),
                }
            if from_session_id == to_session_id:
                return {"invalid": "same_session", "commit_seq": str(_current_commit_seq(connection))}
            if not self._session_belongs_to_owner(connection, session_id=from_session_id, owner_id=owner_id):
                return {"not_found": "sender", "commit_seq": str(_current_commit_seq(connection))}
            if not self._session_belongs_to_owner(connection, session_id=to_session_id, owner_id=owner_id):
                return {"not_found": "target", "commit_seq": str(_current_commit_seq(connection))}
            result = connection.execute(
                insert(table)
                .values(
                    message_key=message_key,
                    owner_id=owner_id,
                    from_session_id=from_session_id,
                    to_session_id=to_session_id,
                    text=text,
                    source_event_id=source_event_id,
                    delivery_status="stored_only",
                    delivery_attempts=0,
                    created_at=created_at,
                    updated_at=created_at,
                )
                .returning(table.c.id)
            )
            message_id = int(result.scalar_one())
            commit_seq = _advance_commit_seq(connection, created_at)
            row = connection.execute(select(table).where(table.c.id == message_id)).mappings().one()
            return {
                "created": True,
                "idempotency_conflict": False,
                "message": _session_message_dto(SimpleNamespace(**row)),
                "commit_seq": str(commit_seq),
            }

    def list_session_messages(
        self,
        *,
        owner_id: int,
        session_id: str,
        direction: str,
        unacknowledged_only: bool,
        limit: int,
    ) -> dict[str, Any]:
        table = LiveSessionMessage.__table__
        with _read_snapshot(self.engine) as connection:
            if not self._session_belongs_to_owner(connection, session_id=session_id, owner_id=owner_id):
                return {"found": False, "messages": [], "commit_seq": str(_current_commit_seq(connection))}
            query = select(table).where(table.c.owner_id == owner_id)
            if direction == "inbound":
                query = query.where(table.c.to_session_id == session_id)
            elif direction == "outbound":
                query = query.where(table.c.from_session_id == session_id)
            else:
                query = query.where(or_(table.c.to_session_id == session_id, table.c.from_session_id == session_id))
            if unacknowledged_only:
                query = query.where(table.c.acknowledged_at.is_(None))
            rows = connection.execute(query.order_by(table.c.created_at.desc(), table.c.id.desc()).limit(limit)).mappings().all()
            return {
                "found": True,
                "messages": [_session_message_dto(SimpleNamespace(**row)) for row in rows],
                "commit_seq": str(_current_commit_seq(connection)),
            }

    def acknowledge_session_message(
        self,
        *,
        owner_id: int,
        message_id: int,
        target_session_id: str,
        acknowledged_at: datetime,
    ) -> dict[str, Any]:
        table = LiveSessionMessage.__table__
        with _write_transaction(self.engine) as connection:
            row = connection.execute(select(table).where(table.c.id == message_id, table.c.owner_id == owner_id)).mappings().first()
            if row is None:
                return {"not_found": True, "commit_seq": str(_current_commit_seq(connection))}
            if str(row["to_session_id"]) != target_session_id:
                return {"forbidden": True, "commit_seq": str(_current_commit_seq(connection))}
            if row["delivery_status"] in {"queued", "delivering"}:
                return {"conflict": "not_delivered", "commit_seq": str(_current_commit_seq(connection))}
            if row["delivery_status"] == "failed":
                return {"conflict": "failed", "commit_seq": str(_current_commit_seq(connection))}
            changed = row["acknowledged_at"] is None
            if changed:
                connection.execute(
                    update(table).where(table.c.id == message_id).values(acknowledged_at=acknowledged_at, updated_at=acknowledged_at)
                )
                commit_seq = _advance_commit_seq(connection, acknowledged_at)
                row = connection.execute(select(table).where(table.c.id == message_id)).mappings().one()
            else:
                commit_seq = _current_commit_seq(connection)
            return {
                "changed": changed,
                "message": _session_message_dto(SimpleNamespace(**row)),
                "commit_seq": str(commit_seq),
            }

    def update_session_message_delivery(
        self,
        *,
        owner_id: int,
        message_id: int,
        expected_status: str,
        delivery_status: str,
        delivery_attempts: int,
        last_error: str | None,
        delivered_via: str | None,
        delivered_at: datetime | None,
        updated_at: datetime,
    ) -> dict[str, Any]:
        table = LiveSessionMessage.__table__
        with _write_transaction(self.engine) as connection:
            row = connection.execute(select(table).where(table.c.id == message_id, table.c.owner_id == owner_id)).mappings().first()
            if row is None:
                return {"not_found": True, "commit_seq": str(_current_commit_seq(connection))}
            desired = {
                "delivery_status": delivery_status,
                "delivery_attempts": delivery_attempts,
                "last_error": last_error,
                "delivered_via": delivered_via,
                "delivered_at": delivered_at,
            }
            replay = all((_as_aware_utc(row[key]) if key == "delivered_at" else row[key]) == value for key, value in desired.items())
            if replay:
                return {
                    "changed": False,
                    "message": _session_message_dto(SimpleNamespace(**row)),
                    "commit_seq": str(_current_commit_seq(connection)),
                }
            if row["delivery_status"] != expected_status:
                return {"conflict": "status_changed", "commit_seq": str(_current_commit_seq(connection))}
            connection.execute(update(table).where(table.c.id == message_id).values(**desired, updated_at=updated_at))
            commit_seq = _advance_commit_seq(connection, updated_at)
            row = connection.execute(select(table).where(table.c.id == message_id)).mappings().one()
            return {
                "changed": True,
                "message": _session_message_dto(SimpleNamespace(**row)),
                "commit_seq": str(commit_seq),
            }

    def pending_session_message_counts(self, *, owner_id: int, session_ids: list[str]) -> dict[str, Any]:
        table = LiveSessionMessage.__table__
        with _read_snapshot(self.engine) as connection:
            rows = connection.execute(
                select(table.c.to_session_id, func.count(table.c.id))
                .where(
                    table.c.owner_id == owner_id,
                    table.c.to_session_id.in_(session_ids),
                    table.c.acknowledged_at.is_(None),
                    table.c.delivery_status != "failed",
                )
                .group_by(table.c.to_session_id)
            ).all()
            counts = {session_id: 0 for session_id in session_ids}
            counts.update({str(session_id): int(count) for session_id, count in rows})
            return {"counts": counts, "commit_seq": str(_current_commit_seq(connection))}

    def list_active_session_ids(self, *, limit: int, days_back: int, observed_at: datetime) -> dict[str, Any]:
        """Return bounded recently observed session identities from the live lane."""

        live = LiveSession.__table__
        catalog = LiveSessionCatalog.__table__
        cutoff = observed_at - timedelta(days=days_back)
        with _read_snapshot(self.engine) as connection:
            rows = connection.execute(
                select(live.c.session_id)
                .join(catalog, catalog.c.session_id == live.c.session_id)
                .where(
                    live.c.state.notin_(("missing", "ended")),
                    catalog.c.user_state.notin_(("archived", "snoozed")),
                    live.c.last_seen_at >= cutoff,
                )
                .order_by(live.c.last_seen_at.desc(), live.c.updated_at.desc(), live.c.session_id.desc())
                .limit(limit)
            ).all()
            return {
                "session_ids": [str(row[0]) for row in rows],
                "commit_seq": str(_current_commit_seq(connection)),
            }

    def update_session_preferences(
        self,
        *,
        session_id: str,
        user_state: str | None,
        loop_mode: str | None,
        notification_muted: bool | None,
        observed_at: datetime,
    ) -> dict[str, Any]:
        """Update bounded user-owned session state in one catalog transaction."""

        table = LiveSessionCatalog.__table__
        with _write_transaction(self.engine) as connection:
            current = (
                connection.execute(
                    select(
                        table.c.user_state,
                        table.c.user_state_at,
                        table.c.loop_mode,
                        table.c.notification_muted,
                    ).where(table.c.session_id == session_id)
                )
                .mappings()
                .first()
            )
            if current is None:
                return {
                    "found": False,
                    "preferences": None,
                    "commit_seq": str(_current_commit_seq(connection)),
                }
            values: dict[str, Any] = {}
            if user_state is not None and user_state != str(current["user_state"] or "active"):
                values["user_state"] = user_state
                values["user_state_at"] = observed_at
            if loop_mode is not None and loop_mode != str(current["loop_mode"] or "assist"):
                values["loop_mode"] = loop_mode
            if notification_muted is not None and notification_muted != bool(current["notification_muted"]):
                values["notification_muted"] = int(notification_muted)
            if values:
                values["updated_at"] = observed_at
                connection.execute(update(table).where(table.c.session_id == session_id).values(**values))
                commit_seq = _advance_commit_seq(connection, observed_at)
            else:
                commit_seq = _current_commit_seq(connection)
            return {
                "found": True,
                "preferences": {
                    "user_state": user_state if user_state is not None else str(current["user_state"] or "active"),
                    "user_state_at": _encode_datetime(observed_at if "user_state" in values else current["user_state_at"]),
                    "loop_mode": loop_mode if loop_mode is not None else str(current["loop_mode"] or "assist"),
                    "notification_muted": (notification_muted if notification_muted is not None else bool(current["notification_muted"])),
                },
                "updated": bool(values),
                "commit_seq": str(commit_seq),
            }

    def resolve_session_prefix(self, *, prefix: str) -> dict[str, Any]:
        observed_at = datetime.now(UTC)
        catalog = LiveSessionCatalog.__table__
        user = LiveUser.__table__
        with _read_snapshot(self.engine) as connection:
            matches = list(
                connection.execute(
                    select(
                        catalog.c.session_id,
                        catalog.c.provider,
                        catalog.c.device_name,
                        catalog.c.started_at,
                        catalog.c.ended_at,
                    )
                    .where(catalog.c.session_id.like(f"{prefix}%"))
                    .order_by(catalog.c.session_id.asc())
                    .limit(2)
                ).mappings()
            )
            status = "unique" if len(matches) == 1 else "ambiguous" if len(matches) > 1 else "missing"
            session_preview: dict[str, Any] | None = None
            owner_preview: dict[str, str | None] | None = None
            if status == "unique":
                match = matches[0]
                session_preview = {
                    "session_id": str(match["session_id"]),
                    "provider": str(match["provider"]),
                    "device_name": match["device_name"],
                    "started_at": _encode_datetime(match["started_at"]),
                    "ended_at": _encode_datetime(match["ended_at"]),
                }
                owner_row = (
                    connection.execute(select(user.c.display_name, user.c.email).order_by(user.c.id.asc()).limit(1)).mappings().first()
                )
                if owner_row is not None:
                    display_name = str(owner_row["display_name"] or "").strip() or None
                    email = str(owner_row["email"] or "").strip()
                    email_local = email.split("@", 1)[0] or None if "@" in email else None
                    owner_preview = {"display_name": display_name, "email_local": email_local}
            return {
                "commit_seq": str(_current_commit_seq(connection)),
                "observed_at": observed_at.isoformat(),
                "status": status,
                "session_id": session_preview["session_id"] if session_preview is not None else None,
                "session": session_preview,
                "owner": owner_preview,
            }

    def list_machine_enrollments(self, *, owner_id: int) -> dict[str, Any]:
        observed_at = datetime.now(UTC)
        token = LiveDeviceToken.__table__
        with _read_snapshot(self.engine) as connection:
            rows = connection.execute(
                select(token.c.device_id, token.c.machine_name, token.c.last_used_at, token.c.created_at)
                .where(token.c.owner_id == owner_id, token.c.revoked_at.is_(None))
                .order_by(token.c.device_id.asc(), token.c.last_used_at.desc(), token.c.created_at.desc())
                .limit(MACHINE_ENROLLMENT_LIMIT + 1)
            ).all()
            if len(rows) > MACHINE_ENROLLMENT_LIMIT:
                return {
                    "commit_seq": str(_current_commit_seq(connection)),
                    "observed_at": observed_at.isoformat(),
                    "enrollments": [],
                    "total": 0,
                    "limit_exceeded": True,
                }
            latest: dict[str, datetime | None] = {}
            created: dict[str, datetime | None] = {}
            names: dict[str, str | None] = {}
            for raw_device_id, machine_name, last_used_at, created_at in rows:
                key = str(raw_device_id or "")
                if not key:
                    continue
                candidate = _as_aware_utc(last_used_at or created_at)
                if key not in latest or (candidate is not None and (latest[key] is None or candidate > latest[key])):
                    latest[key] = candidate
                    created[key] = _as_aware_utc(created_at)
                clean_name = str(machine_name or "").strip() or None
                if clean_name is not None and key not in names:
                    names[key] = clean_name
            enrollments = [
                {
                    "device_id": key,
                    "machine_name": names.get(key),
                    "last_used_at": _encode_datetime(latest[key]),
                    "created_at": _encode_datetime(created[key]),
                }
                for key in sorted(latest)
            ]
            return {
                "commit_seq": str(_current_commit_seq(connection)),
                "observed_at": observed_at.isoformat(),
                "enrollments": enrollments,
                "total": len(enrollments),
                "limit_exceeded": False,
            }

    def list_machine_workspaces(
        self,
        *,
        owner_id: int,
        device_id: str,
        limit: int,
        days_back: int,
    ) -> dict[str, Any]:
        observed_at = datetime.now(UTC)
        since = observed_at - timedelta(days=days_back)
        token = LiveDeviceToken.__table__
        catalog = LiveSessionCatalog.__table__
        with _read_snapshot(self.engine) as connection:
            enrolled = connection.execute(
                select(token.c.id)
                .where(
                    token.c.owner_id == owner_id,
                    token.c.device_id == device_id,
                    token.c.revoked_at.is_(None),
                )
                .limit(1)
            ).first()
            rows = []
            if enrolled is not None:
                rows = connection.execute(
                    select(
                        catalog.c.cwd,
                        catalog.c.git_repo,
                        catalog.c.git_branch,
                        catalog.c.last_activity_at,
                        catalog.c.started_at,
                    )
                    .where(
                        catalog.c.device_id == device_id,
                        catalog.c.cwd.is_not(None),
                        catalog.c.cwd.like("/%"),
                        catalog.c.environment.notin_(_EXCLUDED_WORKSPACE_ENVIRONMENTS),
                        func.coalesce(catalog.c.last_activity_at, catalog.c.started_at) >= since,
                    )
                    .order_by(func.coalesce(catalog.c.last_activity_at, catalog.c.started_at).desc())
                    .limit(WORKSPACE_CANDIDATE_LIMIT + 1)
                ).all()
            limit_exceeded = len(rows) > WORKSPACE_CANDIDATE_LIMIT
            if limit_exceeded:
                rows = rows[:WORKSPACE_CANDIDATE_LIMIT]
            groups: dict[str, dict[str, Any]] = {}
            for cwd, git_repo, git_branch, last_activity_at, started_at in rows:
                used_at = _as_aware_utc(last_activity_at or started_at)
                path = str(cwd or "")
                if used_at is None or not path:
                    continue
                group = groups.setdefault(
                    path,
                    {"score": 0.0, "session_count": 0, "last_used_at": None, "git_repo": None, "git_branch": None},
                )
                group["score"] += _recency_weight(max(0.0, (observed_at - used_at).total_seconds() / 86400.0))
                group["session_count"] += 1
                if group["last_used_at"] is None or used_at > group["last_used_at"]:
                    group.update(last_used_at=used_at, git_repo=git_repo, git_branch=git_branch)
            workspaces = [
                {
                    "path": path,
                    "label": _workspace_label(path, group["git_repo"], group["git_branch"]),
                    "git_repo": group["git_repo"],
                    "git_branch": group["git_branch"],
                    "score": group["score"],
                    "last_used_at": _encode_datetime(group["last_used_at"]),
                    "session_count": group["session_count"],
                }
                for path, group in groups.items()
            ]
            workspaces.sort(key=lambda item: (item["score"], item["last_used_at"] or ""), reverse=True)
            return {
                "commit_seq": str(_current_commit_seq(connection)),
                "observed_at": observed_at.isoformat(),
                "device_id": device_id,
                "workspaces": workspaces[:limit],
                "limit_exceeded": limit_exceeded,
            }

    def open_source_epoch(
        self,
        *,
        tenant_id: str,
        machine_id: str,
        provider: str,
        opaque_source_id: str,
        source_epoch: UUID,
        range_kind: str,
        predecessor_source_epoch: UUID | None,
        opened_at: datetime,
    ) -> dict[str, Any]:
        epoch = LiveSourceEpoch.__table__
        epoch_id = str(source_epoch)
        identity_filters = (
            epoch.c.tenant_id == tenant_id,
            epoch.c.machine_id == machine_id,
            epoch.c.provider == provider,
            epoch.c.opaque_source_id == opaque_source_id,
        )
        with _write_transaction(self.engine) as connection:
            existing = connection.execute(select(epoch).where(epoch.c.source_epoch == epoch_id)).mappings().first()
            expected_predecessor = str(predecessor_source_epoch) if predecessor_source_epoch is not None else None
            if existing is not None:
                exact = all(
                    (
                        existing["tenant_id"] == tenant_id,
                        existing["machine_id"] == machine_id,
                        existing["provider"] == provider,
                        existing["opaque_source_id"] == opaque_source_id,
                        existing["range_kind"] == range_kind,
                        existing["predecessor_source_epoch"] == expected_predecessor,
                        _as_aware_utc(existing["opened_at"]) == opened_at,
                    )
                )
                if not exact:
                    return {"source_epoch_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
                return {
                    "created": False,
                    "exact_replay": True,
                    "source_epoch": _source_epoch_dto(existing),
                    "commit_seq": str(existing["commit_seq"]),
                }

            open_rows = (
                connection.execute(select(epoch).where(*identity_filters, epoch.c.state == "open").order_by(epoch.c.opened_at.desc()))
                .mappings()
                .all()
            )
            predecessor = None
            if predecessor_source_epoch is not None:
                predecessor = connection.execute(select(epoch).where(epoch.c.source_epoch == expected_predecessor)).mappings().first()
                if predecessor is None or any(
                    (
                        predecessor["tenant_id"] != tenant_id,
                        predecessor["machine_id"] != machine_id,
                        predecessor["provider"] != provider,
                        predecessor["opaque_source_id"] != opaque_source_id,
                        predecessor["state"] != "open",
                    )
                ):
                    return {"source_epoch_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
                if any(row["source_epoch"] != expected_predecessor for row in open_rows):
                    return {"source_epoch_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
            elif open_rows:
                return {"source_epoch_conflict": True, "commit_seq": str(_current_commit_seq(connection))}

            commit_time = datetime.now(UTC)
            commit_seq = _advance_commit_seq(connection, commit_time)
            if predecessor is not None:
                connection.execute(
                    update(epoch)
                    .where(epoch.c.source_epoch == expected_predecessor)
                    .values(
                        state="closed",
                        replaced_by_source_epoch=epoch_id,
                        closed_at=opened_at,
                        close_reason="replaced",
                        closed_commit_seq=commit_seq,
                        updated_at=commit_time,
                    )
                )
            connection.execute(
                insert(epoch).values(
                    source_epoch=epoch_id,
                    tenant_id=tenant_id,
                    machine_id=machine_id,
                    provider=provider,
                    opaque_source_id=opaque_source_id,
                    range_kind=range_kind,
                    state="open",
                    predecessor_source_epoch=expected_predecessor,
                    accepted_through=_u64_key(0),
                    object_count=0,
                    commit_seq=commit_seq,
                    opened_at=opened_at,
                    created_at=commit_time,
                    updated_at=commit_time,
                )
            )
            row = connection.execute(select(epoch).where(epoch.c.source_epoch == epoch_id)).mappings().one()
            return {
                "created": True,
                "exact_replay": False,
                "source_epoch": _source_epoch_dto(row),
                "commit_seq": str(commit_seq),
            }

    def commit_raw_object(
        self,
        *,
        protocol_version: int,
        tenant_id: str,
        owner_id: str | None,
        session_id: UUID,
        machine_id: str,
        provider: str,
        opaque_source_id: str,
        source_epoch: UUID,
        predecessor_source_epoch: UUID | None,
        epoch_opened_at: datetime,
        range_kind: str,
        range_start: int,
        range_end: int,
        record_hashes: tuple[bytes, ...],
        envelope_id: str,
        object_hash: str,
        payload_hash: str,
        compressed_hash: str,
        object_path: str,
        uncompressed_size: int,
        compressed_size: int,
        provenance_kind: str,
        render_state: str,
        media_refs: tuple[dict[str, Any], ...],
        projectors: tuple[str, ...],
        render_manifest: dict[str, Any] | None,
        session_facts: dict[str, Any],
        sealed_at: datetime,
    ) -> dict[str, Any]:
        del protocol_version  # validated as v2 by the RPC boundary
        identity = EnvelopeIdentity(
            tenant_id=tenant_id,
            machine_id=machine_id,
            provider=provider,
            opaque_source_id=opaque_source_id,
            source_epoch=source_epoch,
            range_kind=range_kind,
            range_start=range_start,
            range_end=range_end,
            record_hashes=record_hashes,
        )
        if compute_envelope_id(identity) != envelope_id:
            return {"identity_mismatch": True}

        epoch = LiveSourceEpoch.__table__
        raw = LiveRawObject.__table__
        tombstone = LiveSessionTombstone.__table__
        storage_session = StorageSession.__table__
        live_session_catalog = LiveSessionCatalog.__table__
        render_generation = RenderGeneration.__table__
        render_object = RenderObject.__table__
        media_object = MediaObject.__table__
        session_media_ref = SessionMediaRef.__table__
        session_key = str(session_id)
        epoch_key = str(source_epoch)
        record_hashes_hash = hashlib.sha256(b"".join(record_hashes)).hexdigest()
        canonical_media_refs = json.dumps(list(media_refs), sort_keys=True, separators=(",", ":"))
        media_refs_hash = hashlib.sha256(canonical_media_refs.encode()).hexdigest()
        range_start_key = _u64_key(range_start)
        range_end_key = _u64_key(range_end)
        immutable_base = {
            "tenant_id": tenant_id,
            "session_id": session_key,
            "machine_id": machine_id,
            "provider": provider,
            "opaque_source_id": opaque_source_id,
            "source_epoch": epoch_key,
            "range_kind": range_kind,
            "range_start": range_start_key,
            "range_end": range_end_key,
            "record_count": len(record_hashes),
            "record_hashes_hash": record_hashes_hash,
            "object_hash": object_hash,
            "payload_hash": payload_hash,
            "compressed_hash": compressed_hash,
            "object_path": object_path,
            "uncompressed_size": uncompressed_size,
            "compressed_size": compressed_size,
            "provenance_kind": provenance_kind,
            "media_refs_hash": media_refs_hash,
            "sealed_at": sealed_at,
        }
        # Envelope identity deliberately excludes session membership, clocks,
        # compression, and object placement. A retry after relinking or a
        # codec upgrade must return the original durable receipt instead of
        # inventing a conflicting second representation.
        replay_identity = {
            key: immutable_base[key]
            for key in (
                "tenant_id",
                "machine_id",
                "provider",
                "opaque_source_id",
                "source_epoch",
                "range_kind",
                "range_start",
                "range_end",
                "record_count",
                "record_hashes_hash",
                "media_refs_hash",
            )
        }
        with _write_transaction(self.engine) as connection:
            deleted = connection.execute(
                select(tombstone.c.deletion_revision).where(tombstone.c.session_id == session_key)
            ).scalar_one_or_none()
            if deleted is not None:
                return {"session_deleted": True, "deletion_revision": str(deleted)}

            existing = connection.execute(select(raw).where(raw.c.envelope_id == envelope_id)).mappings().first()
            if existing is not None:
                if existing["retired_at"] is not None or existing["retirement_revision"] is not None:
                    return {"source_epoch_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
                if not _raw_object_matches(existing, replay_identity):
                    return {"source_epoch_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
                return {
                    "created": False,
                    "exact_replay": True,
                    "receipt": _raw_object_receipt(existing),
                }

            referenced_hashes = sorted({str(ref["media_hash"]) for ref in media_refs})
            media_rows = (
                connection.execute(select(media_object).where(media_object.c.media_hash.in_(referenced_hashes))).mappings().all()
                if referenced_hashes
                else []
            )
            media_by_hash = {str(row["media_hash"]): row for row in media_rows}
            unavailable = sorted(
                {
                    str(ref["media_hash"])
                    for ref in media_refs
                    if ref["availability"] == "available"
                    and (str(ref["media_hash"]) not in media_by_hash or str(media_by_hash[str(ref["media_hash"])]["state"]) != "present")
                }
            )
            if unavailable:
                return {
                    "media_unavailable": True,
                    "media_hashes": unavailable,
                    "commit_seq": str(_current_commit_seq(connection)),
                }
            missing_media_hashes = tuple(
                sorted(
                    {
                        str(ref["media_hash"])
                        for ref in media_refs
                        if ref["availability"] == "missing"
                        and (
                            str(ref["media_hash"]) not in media_by_hash or str(media_by_hash[str(ref["media_hash"])]["state"]) != "present"
                        )
                    }
                )
            )
            media_state = "missing" if missing_media_hashes else "complete"
            missing_json = json.dumps(list(missing_media_hashes), separators=(",", ":"))
            immutable = {
                **immutable_base,
                "render_state": render_state,
                "media_state": media_state,
                "missing_media_hashes_json": missing_json,
            }

            existing_session = (
                connection.execute(select(storage_session).where(storage_session.c.session_id == session_key)).mappings().first()
            )
            live_console_session = (
                connection.execute(
                    select(live_session_catalog).where(
                        live_session_catalog.c.session_id == session_key,
                        live_session_catalog.c.origin_kind == "console",
                    )
                )
                .mappings()
                .first()
            )
            if existing_session is not None and any(
                (
                    existing_session["tenant_id"] != tenant_id,
                    existing_session["provider"] != provider,
                )
            ):
                return {"source_epoch_conflict": True, "commit_seq": str(_current_commit_seq(connection))}

            existing_generation = None
            if render_manifest is not None:
                generation_key = str(render_manifest["generation_id"])
                existing_generation = (
                    connection.execute(select(render_generation).where(render_generation.c.generation_id == generation_key))
                    .mappings()
                    .first()
                )
                if existing_generation is not None and any(
                    (
                        existing_generation["session_id"] != session_key,
                        existing_generation["parser_revision"] != render_manifest["parser_revision"],
                        existing_generation["ordering_revision"] != render_manifest["ordering_revision"],
                        existing_generation["state"] == "failed",
                    )
                ):
                    return {"source_epoch_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
                existing_render = (
                    connection.execute(
                        select(render_object).where(
                            or_(
                                render_object.c.object_id == render_manifest["object_id"],
                                (render_object.c.generation_id == generation_key) & (render_object.c.source_envelope_id == envelope_id),
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if existing_render is not None:
                    return {"source_epoch_conflict": True, "commit_seq": str(_current_commit_seq(connection))}

            expected_predecessor = str(predecessor_source_epoch) if predecessor_source_epoch is not None else None
            epoch_row = connection.execute(select(epoch).where(epoch.c.source_epoch == epoch_key)).mappings().first()
            epoch_is_new = epoch_row is None
            predecessor_row = None
            predecessor_raw_rows: list[Any] = []
            identity_filters = (
                epoch.c.tenant_id == tenant_id,
                epoch.c.machine_id == machine_id,
                epoch.c.provider == provider,
                epoch.c.opaque_source_id == opaque_source_id,
            )
            if epoch_is_new:
                open_rows = connection.execute(select(epoch).where(*identity_filters, epoch.c.state == "open")).mappings().all()
                if predecessor_source_epoch is not None:
                    predecessor_row = (
                        connection.execute(select(epoch).where(epoch.c.source_epoch == expected_predecessor)).mappings().first()
                    )
                    if predecessor_row is None or any(
                        (
                            predecessor_row["tenant_id"] != tenant_id,
                            predecessor_row["machine_id"] != machine_id,
                            predecessor_row["provider"] != provider,
                            predecessor_row["opaque_source_id"] != opaque_source_id,
                            predecessor_row["state"] != "open",
                        )
                    ):
                        return {"source_epoch_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
                    if any(row["source_epoch"] != expected_predecessor for row in open_rows):
                        return {"source_epoch_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
                    predecessor_raw_rows = list(
                        connection.execute(select(raw).where(raw.c.source_epoch == expected_predecessor, raw.c.retired_at.is_(None)))
                        .mappings()
                        .all()
                    )
                elif open_rows:
                    return {"source_epoch_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
                # Source offsets are coordinates, not byte counts. An initial
                # storage-v2 epoch may begin at a proven legacy cursor, so its
                # first durable envelope defines the contiguous base.
                accepted_through = range_start_key
            else:
                if any(
                    (
                        epoch_row["tenant_id"] != tenant_id,
                        epoch_row["machine_id"] != machine_id,
                        epoch_row["provider"] != provider,
                        epoch_row["opaque_source_id"] != opaque_source_id,
                        epoch_row["range_kind"] != range_kind,
                        epoch_row["predecessor_source_epoch"] != expected_predecessor,
                        _as_aware_utc(epoch_row["opened_at"]) != epoch_opened_at,
                    )
                ):
                    return {"source_epoch_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
                if epoch_row["state"] != "open":
                    return {"source_epoch_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
                linked_session = connection.execute(
                    select(raw.c.session_id).where(raw.c.source_epoch == epoch_key).limit(1)
                ).scalar_one_or_none()
                if linked_session is not None and str(linked_session) != session_key:
                    return {"source_epoch_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
                existing_raw_ranges = connection.execute(
                    select(raw.c.range_start, raw.c.range_end)
                    .where(raw.c.source_epoch == epoch_key, raw.c.retired_at.is_(None))
                    .order_by(raw.c.range_start.asc(), raw.c.range_end.asc())
                ).all()
                accepted_through = (
                    range_start_key
                    if not existing_raw_ranges and int(epoch_row["object_count"] or 0) == 0
                    else _u64_key(_contiguous_range_prefix(existing_raw_ranges))
                )
                if existing_raw_ranges and accepted_through != str(epoch_row["accepted_through"]):
                    connection.execute(
                        update(epoch)
                        .where(epoch.c.source_epoch == epoch_key)
                        .values(accepted_through=accepted_through, updated_at=datetime.now(UTC))
                    )

            same_range = connection.execute(
                select(raw.c.envelope_id).where(
                    raw.c.source_epoch == epoch_key,
                    raw.c.range_start == range_start_key,
                    raw.c.range_end == range_end_key,
                )
            ).first()
            if same_range is not None:
                return {
                    "source_epoch_conflict": True,
                    "commit_seq": str(_current_commit_seq(connection)),
                    "conflict_details": {
                        "reason": "same_range_different_identity",
                        "accepted_through": str(int(accepted_through)),
                        "requested_range_start": str(range_start),
                        "requested_range_end": str(range_end),
                        "overlapping_envelope_ids": [str(same_range[0])],
                    },
                }
            if range_start_key != accepted_through:
                return {
                    "source_epoch_conflict": True,
                    "commit_seq": str(_current_commit_seq(connection)),
                    "conflict_details": {
                        "reason": "range_overlap" if range_start_key < accepted_through else "range_gap",
                        "accepted_through": str(int(accepted_through)),
                        "requested_range_start": str(range_start),
                        "requested_range_end": str(range_end),
                        "overlapping_envelope_ids": [],
                    },
                }
            if range_start < range_end:
                overlap = connection.execute(
                    select(raw.c.envelope_id)
                    .where(
                        raw.c.source_epoch == epoch_key,
                        raw.c.range_start < range_end_key,
                        raw.c.range_end > range_start_key,
                    )
                    .limit(1)
                ).first()
                if overlap is not None:
                    return {
                        "source_epoch_conflict": True,
                        "commit_seq": str(_current_commit_seq(connection)),
                        "conflict_details": {
                            "reason": "range_overlap",
                            "accepted_through": str(int(accepted_through)),
                            "requested_range_start": str(range_start),
                            "requested_range_end": str(range_end),
                            "overlapping_envelope_ids": [str(overlap[0])],
                        },
                    }

            commit_time = datetime.now(UTC)
            commit_seq = _advance_commit_seq(connection, commit_time)
            if epoch_is_new:
                if predecessor_row is not None:
                    connection.execute(
                        update(epoch)
                        .where(epoch.c.source_epoch == expected_predecessor)
                        .values(
                            state="closed",
                            replaced_by_source_epoch=epoch_key,
                            closed_at=epoch_opened_at,
                            close_reason="replaced",
                            closed_commit_seq=commit_seq,
                            updated_at=commit_time,
                        )
                    )
                    retired_envelope_ids = [str(row["envelope_id"]) for row in predecessor_raw_rows]
                    replaced_session_ids = {str(row["session_id"]) for row in predecessor_raw_rows if str(row["session_id"]) != session_key}
                    if retired_envelope_ids:
                        connection.execute(
                            update(raw)
                            .where(raw.c.envelope_id.in_(retired_envelope_ids))
                            .values(retired_at=commit_time, retirement_revision=commit_seq)
                        )
                        connection.execute(
                            update(render_object)
                            .where(
                                render_object.c.source_envelope_id.in_(retired_envelope_ids),
                                render_object.c.retired_at.is_(None),
                            )
                            .values(retired_at=commit_time, retirement_revision=commit_seq)
                        )
                        connection.execute(
                            update(session_media_ref)
                            .where(
                                session_media_ref.c.envelope_id.in_(retired_envelope_ids),
                                session_media_ref.c.state == "active",
                            )
                            .values(
                                state="retired",
                                retired_at=commit_time,
                                deletion_revision=commit_seq,
                                commit_seq=commit_seq,
                            )
                        )
                    for replaced_session_id in replaced_session_ids:
                        has_active_raw = connection.execute(
                            select(raw.c.envelope_id)
                            .where(
                                raw.c.session_id == replaced_session_id,
                                raw.c.retired_at.is_(None),
                            )
                            .limit(1)
                        ).first()
                        if has_active_raw is None:
                            connection.execute(
                                update(storage_session)
                                .where(storage_session.c.session_id == replaced_session_id)
                                .values(
                                    hidden_from_default_timeline=1,
                                    raw_state="retired",
                                    render_state="retired",
                                    commit_seq=commit_seq,
                                    updated_at=commit_time,
                                )
                            )
                            connection.execute(
                                update(render_generation)
                                .where(
                                    render_generation.c.session_id == replaced_session_id,
                                    render_generation.c.state == "current",
                                )
                                .values(
                                    state="superseded",
                                    superseded_at=commit_time,
                                    commit_seq=commit_seq,
                                    updated_at=commit_time,
                                )
                            )
                connection.execute(
                    insert(epoch).values(
                        source_epoch=epoch_key,
                        tenant_id=tenant_id,
                        machine_id=machine_id,
                        provider=provider,
                        opaque_source_id=opaque_source_id,
                        range_kind=range_kind,
                        state="open",
                        predecessor_source_epoch=expected_predecessor,
                        accepted_through=range_end_key,
                        object_count=1,
                        commit_seq=commit_seq,
                        opened_at=epoch_opened_at,
                        created_at=commit_time,
                        updated_at=commit_time,
                    )
                )
            connection.execute(
                insert(raw).values(
                    envelope_id=envelope_id,
                    **immutable,
                    commit_seq=commit_seq,
                    created_at=commit_time,
                )
            )
            missing_object_hashes = sorted(media_hash for media_hash in missing_media_hashes if media_hash not in media_by_hash)
            if missing_object_hashes:
                connection.execute(
                    insert(media_object),
                    [
                        {
                            "media_hash": media_hash,
                            "state": "missing",
                            "mime_type": None,
                            "byte_size": None,
                            "object_path": None,
                            "commit_seq": commit_seq,
                            "observed_at": commit_time,
                            "verified_at": None,
                            "deleted_at": None,
                            "created_at": commit_time,
                            "updated_at": commit_time,
                        }
                        for media_hash in missing_object_hashes
                    ],
                )
            if media_refs:
                connection.execute(
                    insert(session_media_ref),
                    [
                        {
                            "session_id": session_key,
                            "media_hash": ref["media_hash"],
                            "envelope_id": envelope_id,
                            "ref_key": ref["ref_key"],
                            "state": "active",
                            "commit_seq": commit_seq,
                            "created_at": commit_time,
                        }
                        for ref in media_refs
                    ],
                )
            if not epoch_is_new:
                connection.execute(
                    update(epoch)
                    .where(epoch.c.source_epoch == epoch_key)
                    .values(
                        accepted_through=range_end_key,
                        object_count=int(epoch_row["object_count"] or 0) + 1,
                        updated_at=commit_time,
                    )
                )
            session_values = {
                "owner_id": owner_id,
                "environment": session_facts["environment"],
                "project": session_facts["project"],
                "cwd": session_facts["cwd"],
                "git_repo": session_facts["git_repo"],
                "git_branch": session_facts["git_branch"],
                "ended_at": session_facts["ended_at"],
                "origin_kind": session_facts["origin_kind"],
                "hidden_from_default_timeline": int(session_facts["hidden_from_default_timeline"]),
                "launch_actor": session_facts["launch_actor"],
                "launch_surface": session_facts["launch_surface"],
                "raw_state": "durable",
                "render_state": render_state,
                "media_state": media_state,
                "missing_media_hashes_json": missing_json,
                "transcript_revision": commit_seq,
                "commit_seq": commit_seq,
                "updated_at": commit_time,
            }
            if live_console_session is not None:
                # A Console session outlives each bounded provider process. The
                # provider transcript does not carry the Console launch
                # provenance and its ended_at closes only that one process, not
                # the reusable Longhouse session.
                session_values["origin_kind"] = "console"
                session_values["launch_actor"] = live_console_session["launch_actor"]
                session_values["launch_surface"] = live_console_session["launch_surface"]
                session_values["hidden_from_default_timeline"] = int(live_console_session["hidden_from_default_timeline"] or 0)
                session_values["ended_at"] = None
            if existing_session is None:
                connection.execute(
                    insert(storage_session).values(
                        session_id=session_key,
                        tenant_id=tenant_id,
                        provider=provider,
                        machine_id=machine_id,
                        started_at=session_facts["started_at"],
                        last_activity_at=session_facts["last_activity_at"],
                        created_at=commit_time,
                        **session_values,
                    )
                )
            else:
                if predecessor_row is not None:
                    active_missing: set[str] = set()
                    for active_raw in connection.execute(
                        select(raw.c.missing_media_hashes_json).where(
                            raw.c.session_id == session_key,
                            raw.c.retired_at.is_(None),
                        )
                    ):
                        active_missing.update(json.loads(active_raw[0] or "[]"))
                    bounded_missing = sorted(active_missing)[:1_000]
                    session_values["media_state"] = "missing" if bounded_missing else "complete"
                    session_values["missing_media_hashes_json"] = json.dumps(bounded_missing, separators=(",", ":"))
                elif existing_session["media_state"] == "missing":
                    previous_missing = json.loads(existing_session["missing_media_hashes_json"] or "[]")
                    combined_missing = sorted(set(previous_missing) | set(missing_media_hashes))[:1_000]
                    session_values["media_state"] = "missing"
                    session_values["missing_media_hashes_json"] = json.dumps(combined_missing, separators=(",", ":"))
                for optional_field in (
                    "owner_id",
                    "project",
                    "cwd",
                    "git_repo",
                    "git_branch",
                    "ended_at",
                    "origin_kind",
                    "launch_actor",
                    "launch_surface",
                ):
                    if session_values[optional_field] is None:
                        del session_values[optional_field]
                session_values["started_at"] = min(
                    _as_aware_utc(existing_session["started_at"]) or session_facts["started_at"],
                    session_facts["started_at"],
                )
                session_values["last_activity_at"] = max(
                    _as_aware_utc(existing_session["last_activity_at"]) or session_facts["last_activity_at"],
                    session_facts["last_activity_at"],
                )
                connection.execute(update(storage_session).where(storage_session.c.session_id == session_key).values(**session_values))
            if render_manifest is not None:
                generation_key = str(render_manifest["generation_id"])
                publish_render = render_state == "ready"
                if existing_generation is None:
                    connection.execute(
                        insert(render_generation).values(
                            generation_id=generation_key,
                            session_id=session_key,
                            parser_revision=render_manifest["parser_revision"],
                            ordering_revision=render_manifest["ordering_revision"],
                            state="current" if publish_render else "pending",
                            source_chain_hash=hashlib.sha256(bytes.fromhex(envelope_id)).hexdigest(),
                            object_count=1,
                            event_count=render_manifest["event_count"],
                            first_order_key=render_manifest["first_order_key"],
                            last_order_key=render_manifest["last_order_key"],
                            commit_seq=commit_seq,
                            created_at=commit_time,
                            updated_at=commit_time,
                        )
                    )
                else:
                    connection.execute(
                        update(render_generation)
                        .where(render_generation.c.generation_id == generation_key)
                        .values(
                            state="current" if publish_render else "pending",
                            source_chain_hash=hashlib.sha256(
                                bytes.fromhex(str(existing_generation["source_chain_hash"])) + bytes.fromhex(envelope_id)
                            ).hexdigest(),
                            object_count=int(existing_generation["object_count"]) + 1,
                            event_count=int(existing_generation["event_count"]) + render_manifest["event_count"],
                            first_order_key=_minimum_order_key(existing_generation["first_order_key"], render_manifest["first_order_key"]),
                            last_order_key=_maximum_order_key(existing_generation["last_order_key"], render_manifest["last_order_key"]),
                            commit_seq=commit_seq,
                            updated_at=commit_time,
                        )
                    )
                if publish_render:
                    connection.execute(
                        update(render_generation)
                        .where(
                            render_generation.c.session_id == session_key,
                            render_generation.c.generation_id != generation_key,
                            render_generation.c.state == "current",
                        )
                        .values(state="superseded", superseded_at=commit_time, commit_seq=commit_seq, updated_at=commit_time)
                    )
                connection.execute(
                    insert(render_object).values(
                        object_id=render_manifest["object_id"],
                        generation_id=generation_key,
                        session_id=session_key,
                        source_envelope_id=envelope_id,
                        object_hash=render_manifest["object_hash"],
                        payload_hash=render_manifest["payload_hash"],
                        object_path=render_manifest["object_path"],
                        uncompressed_size=render_manifest["uncompressed_size"],
                        compressed_size=render_manifest["compressed_size"],
                        event_count=render_manifest["event_count"],
                        user_messages=render_manifest["user_messages"],
                        assistant_messages=render_manifest["assistant_messages"],
                        tool_calls=render_manifest["tool_calls"],
                        first_user_message_preview=render_manifest["first_user_message_preview"],
                        last_visible_text_preview=render_manifest["last_visible_text_preview"],
                        first_order_key=render_manifest["first_order_key"],
                        last_order_key=render_manifest["last_order_key"],
                        **_render_order_columns(
                            render_manifest["first_order_key"],
                            render_manifest["last_order_key"],
                        ),
                        commit_seq=commit_seq,
                        created_at=commit_time,
                    )
                )
                if publish_render:
                    projection_values: dict[str, Any] = {
                        "current_render_generation": generation_key,
                        "render_state": "ready",
                        "user_messages": int((existing_session or {}).get("user_messages") or 0) + render_manifest["user_messages"],
                        "assistant_messages": int((existing_session or {}).get("assistant_messages") or 0)
                        + render_manifest["assistant_messages"],
                        "tool_calls": int((existing_session or {}).get("tool_calls") or 0) + render_manifest["tool_calls"],
                    }
                    immediate_title = sanitize_title(render_manifest["first_user_message_preview"], max_words=6)
                    if not (existing_session or {}).get("summary_title") and immediate_title:
                        projection_values["summary_title"] = immediate_title
                    if not (existing_session or {}).get("first_user_message_preview") and render_manifest["first_user_message_preview"]:
                        projection_values["first_user_message_preview"] = render_manifest["first_user_message_preview"]
                    if render_manifest["last_visible_text_preview"]:
                        projection_values["last_visible_text_preview"] = render_manifest["last_visible_text_preview"]
                    connection.execute(
                        update(storage_session).where(storage_session.c.session_id == session_key).values(**projection_values)
                    )
            if predecessor_row is not None and render_state == "ready":
                generation_to_recompute = (
                    str(render_manifest["generation_id"])
                    if render_manifest is not None
                    else str((existing_session or {}).get("current_render_generation") or "")
                )
                if generation_to_recompute:
                    _recompute_render_generation_projection(
                        connection,
                        session_id=session_key,
                        generation_id=generation_to_recompute,
                        commit_seq=commit_seq,
                        commit_time=commit_time,
                    )
            projector_table = ProjectorState.__table__
            for projector in projectors:
                projector_row = (
                    connection.execute(
                        select(projector_table).where(
                            projector_table.c.projector == projector,
                            projector_table.c.session_id == session_key,
                        )
                    )
                    .mappings()
                    .first()
                )
                if projector_row is None:
                    connection.execute(
                        insert(projector_table).values(
                            projector=projector,
                            session_id=session_key,
                            desired_revision=commit_seq,
                            completed_revision=0,
                            status="idle",
                            failure_count=0,
                            commit_seq=commit_seq,
                            created_at=commit_time,
                            updated_at=commit_time,
                        )
                    )
                elif int(projector_row["desired_revision"]) < commit_seq:
                    connection.execute(
                        update(projector_table)
                        .where(
                            projector_table.c.projector == projector,
                            projector_table.c.session_id == session_key,
                        )
                        .values(
                            desired_revision=commit_seq,
                            commit_seq=commit_seq,
                            updated_at=commit_time,
                        )
                    )
            row = connection.execute(select(raw).where(raw.c.envelope_id == envelope_id)).mappings().one()
            return {
                "created": True,
                "exact_replay": False,
                "receipt": _raw_object_receipt(row),
            }

    def read_source_epoch_manifest(
        self,
        *,
        source_epoch: UUID,
        after_position: int | None,
        limit: int,
    ) -> dict[str, Any]:
        epoch = LiveSourceEpoch.__table__
        raw = LiveRawObject.__table__
        epoch_key = str(source_epoch)
        observed_at = datetime.now(UTC)
        with _read_snapshot(self.engine) as connection:
            epoch_row = connection.execute(select(epoch).where(epoch.c.source_epoch == epoch_key)).mappings().first()
            if epoch_row is None:
                return {
                    "found": False,
                    "commit_seq": str(_current_commit_seq(connection)),
                    "observed_at": observed_at.isoformat(),
                }
            statement = select(raw).where(raw.c.source_epoch == epoch_key)
            if after_position is not None:
                statement = statement.where(raw.c.range_start >= _u64_key(after_position))
            rows = (
                connection.execute(statement.order_by(raw.c.range_start.asc(), raw.c.range_end.asc(), raw.c.envelope_id.asc()).limit(limit))
                .mappings()
                .all()
            )
            return {
                "found": True,
                "source_epoch": _source_epoch_dto(epoch_row),
                "objects": [_raw_object_manifest_dto(row) for row in rows],
                "commit_seq": str(_current_commit_seq(connection)),
                "observed_at": observed_at.isoformat(),
            }

    def raw_objects_exist_batch(self, *, envelope_ids: tuple[str, ...]) -> dict[str, Any]:
        raw = LiveRawObject.__table__
        observed_at = datetime.now(UTC)
        with _read_snapshot(self.engine) as connection:
            rows = connection.execute(select(raw).where(raw.c.envelope_id.in_(envelope_ids))).mappings().all()
            by_id = {str(row["envelope_id"]): row for row in rows}
            return {
                "objects": [
                    {
                        "envelope_id": envelope_id,
                        "exists": envelope_id in by_id,
                        "state": (
                            "deleted"
                            if envelope_id in by_id and by_id[envelope_id]["retired_at"] is not None
                            else "durable"
                            if envelope_id in by_id
                            else "missing"
                        ),
                        "object_hash": str(by_id[envelope_id]["object_hash"]) if envelope_id in by_id else None,
                        "commit_seq": str(by_id[envelope_id]["commit_seq"]) if envelope_id in by_id else None,
                        "receipt": _raw_object_receipt(by_id[envelope_id]) if envelope_id in by_id else None,
                    }
                    for envelope_id in envelope_ids
                ],
                "commit_seq": str(_current_commit_seq(connection)),
                "observed_at": observed_at.isoformat(),
            }

    def delete_storage_session(
        self,
        *,
        session_id: UUID,
        deletion_id: UUID,
        reason: str | None,
        deleted_at: datetime,
    ) -> dict[str, Any]:
        """Fence a session, retire durable manifests, and remove bounded live state."""

        session_key = str(session_id)
        deletion_key = str(deletion_id)
        tombstones = LiveSessionTombstone.__table__
        sessions = StorageSession.__table__
        raw = LiveRawObject.__table__
        render_objects = RenderObject.__table__
        generations = RenderGeneration.__table__
        media_refs = SessionMediaRef.__table__
        projector_state = ProjectorState.__table__
        with _write_transaction(self.engine) as connection:
            existing = connection.execute(select(tombstones).where(tombstones.c.session_id == session_key)).mappings().first()
            if existing is not None:
                return {
                    "changed": False,
                    "exact_replay": existing["deletion_id"] == deletion_key,
                    "session_id": session_key,
                    "deletion_id": existing["deletion_id"],
                    "deletion_revision": str(existing["deletion_revision"]),
                    "commit_seq": str(existing["commit_seq"]),
                }
            commit_seq = _advance_commit_seq(connection, deleted_at)
            connection.execute(
                insert(tombstones).values(
                    session_id=session_key,
                    deletion_id=deletion_key,
                    deletion_revision=commit_seq,
                    deleted_at=deleted_at,
                    reason=reason,
                    commit_seq=commit_seq,
                )
            )
            retired_raw = connection.execute(
                update(raw)
                .where(raw.c.session_id == session_key, raw.c.retired_at.is_(None))
                .values(retired_at=deleted_at, retirement_revision=commit_seq)
            ).rowcount
            retired_render = connection.execute(
                update(render_objects)
                .where(render_objects.c.session_id == session_key, render_objects.c.retired_at.is_(None))
                .values(retired_at=deleted_at, retirement_revision=commit_seq)
            ).rowcount
            retired_generations = connection.execute(
                update(generations)
                .where(generations.c.session_id == session_key, generations.c.state != "superseded")
                .values(state="superseded", superseded_at=deleted_at, updated_at=deleted_at, commit_seq=commit_seq)
            ).rowcount
            retired_media_refs = connection.execute(
                update(media_refs)
                .where(media_refs.c.session_id == session_key, media_refs.c.state == "active")
                .values(
                    state="retired",
                    retired_at=deleted_at,
                    deletion_revision=commit_seq,
                    commit_seq=commit_seq,
                )
            ).rowcount
            connection.execute(
                update(sessions)
                .where(sessions.c.session_id == session_key)
                .values(
                    user_state="deleted",
                    ended_at=func.coalesce(sessions.c.ended_at, deleted_at),
                    updated_at=deleted_at,
                    commit_seq=commit_seq,
                )
            )
            search_state = (
                connection.execute(
                    select(projector_state).where(
                        projector_state.c.projector == "search-v2",
                        projector_state.c.session_id == session_key,
                    )
                )
                .mappings()
                .first()
            )
            if search_state is None:
                connection.execute(
                    insert(projector_state).values(
                        projector="search-v2",
                        session_id=session_key,
                        desired_revision=commit_seq,
                        completed_revision=0,
                        status="idle",
                        failure_count=0,
                        commit_seq=commit_seq,
                        created_at=deleted_at,
                        updated_at=deleted_at,
                    )
                )
            else:
                connection.execute(
                    update(projector_state)
                    .where(
                        projector_state.c.projector == "search-v2",
                        projector_state.c.session_id == session_key,
                    )
                    .values(
                        desired_revision=commit_seq,
                        claimed_revision=None,
                        claim_token=None,
                        worker_id=None,
                        claim_expires_at=None,
                        status="idle",
                        retry_at=None,
                        commit_seq=commit_seq,
                        updated_at=deleted_at,
                    )
                )
            live_deleted = _delete_bounded_live_session_state(connection, session_key=session_key, deleted_at=deleted_at)
            return {
                "changed": True,
                "exact_replay": False,
                "session_id": session_key,
                "deletion_id": deletion_key,
                "deletion_revision": str(commit_seq),
                "retired_raw_objects": int(retired_raw or 0),
                "retired_render_objects": int(retired_render or 0),
                "retired_render_generations": int(retired_generations or 0),
                "retired_media_refs": int(retired_media_refs or 0),
                "live_rows_removed": live_deleted,
                "commit_seq": str(commit_seq),
            }

    def read_storage_session(self, *, session_id: UUID) -> dict[str, Any]:
        table = StorageSession.__table__
        tombstone = LiveSessionTombstone.__table__
        session_key = str(session_id)
        observed_at = datetime.now(UTC)
        with _read_snapshot(self.engine) as connection:
            deleted = connection.execute(
                select(tombstone.c.deletion_revision).where(tombstone.c.session_id == session_key)
            ).scalar_one_or_none()
            row = connection.execute(select(table).where(table.c.session_id == session_key)).mappings().first()
            return {
                "found": row is not None and deleted is None,
                "deleted": deleted is not None,
                "deletion_revision": str(deleted) if deleted is not None else None,
                "session": _storage_session_dto(row) if row is not None and deleted is None else None,
                "commit_seq": str(_current_commit_seq(connection)),
                "observed_at": observed_at.isoformat(),
            }

    def list_storage_sessions(
        self,
        *,
        owner_id: str,
        before_last_activity_at: datetime | None,
        before_session_id: UUID | None,
        project: str | None,
        provider: str | None,
        include_test: bool,
        limit: int,
    ) -> dict[str, Any]:
        table = StorageSession.__table__
        tombstone = LiveSessionTombstone.__table__
        observed_at = datetime.now(UTC)
        statement = select(table).where(
            table.c.owner_id == owner_id,
            table.c.hidden_from_default_timeline == 0,
            table.c.user_state != "deleted",
            ~select(tombstone.c.session_id).where(tombstone.c.session_id == table.c.session_id).exists(),
        )
        if project is not None:
            statement = statement.where(table.c.project == project)
        if provider is not None:
            statement = statement.where(table.c.provider == provider)
        if not include_test:
            statement = statement.where(table.c.environment.notin_(("test", "e2e")))
        if before_last_activity_at is not None and before_session_id is not None:
            statement = statement.where(
                or_(
                    table.c.last_activity_at < before_last_activity_at,
                    and_(
                        table.c.last_activity_at == before_last_activity_at,
                        table.c.session_id > str(before_session_id),
                    ),
                )
            )
        with _read_snapshot(self.engine) as connection:
            rows = list(
                connection.execute(statement.order_by(table.c.last_activity_at.desc(), table.c.session_id.asc()).limit(limit + 1))
                .mappings()
                .all()
            )
            has_more = len(rows) > limit
            rows = rows[:limit]
            return {
                "sessions": [_storage_session_dto(row) for row in rows],
                "has_more": has_more,
                "commit_seq": str(_current_commit_seq(connection)),
                "observed_at": observed_at.isoformat(),
            }

    def read_storage_health(self, *, owner_id: str) -> dict[str, Any]:
        """Return bounded ingest-freshness facts without scanning the retired monolith."""

        sessions = StorageSession.__table__
        tombstones = LiveSessionTombstone.__table__
        heartbeats = LiveHeartbeatStamp.__table__
        observed_at = datetime.now(UTC)
        visible = and_(
            sessions.c.owner_id == owner_id,
            sessions.c.user_state != "deleted",
            ~select(tombstones.c.session_id).where(tombstones.c.session_id == sessions.c.session_id).exists(),
        )
        with _read_snapshot(self.engine) as connection:
            session_count, last_session_at, media_repair_refs = connection.execute(
                select(
                    func.count(sessions.c.session_id),
                    func.max(sessions.c.last_activity_at),
                    func.sum(case((sessions.c.media_state != "complete", 1), else_=0)),
                ).where(visible)
            ).one()
            last_heartbeat_at = connection.execute(
                select(func.max(heartbeats.c.received_at)).where(heartbeats.c.is_offline == 0)
            ).scalar_one_or_none()
            return {
                "session_count": int(session_count or 0),
                "last_session_at": _encode_datetime(last_session_at),
                "last_heartbeat_at": _encode_datetime(last_heartbeat_at),
                "media_repair_refs": int(media_repair_refs or 0),
                "media_repair_bytes": 0,
                "commit_seq": str(_current_commit_seq(connection)),
                "observed_at": observed_at.isoformat(),
            }

    def read_storage_session_raw_manifest(
        self,
        *,
        session_id: UUID,
        owner_id: str,
        after_source_key: str | None,
        limit: int,
    ) -> dict[str, Any]:
        session_table = StorageSession.__table__
        raw = LiveRawObject.__table__
        tombstone = LiveSessionTombstone.__table__
        session_key = str(session_id)
        observed_at = datetime.now(UTC)
        with _read_snapshot(self.engine) as connection:
            deleted = connection.execute(
                select(tombstone.c.deletion_revision).where(tombstone.c.session_id == session_key)
            ).scalar_one_or_none()
            session_row = (
                connection.execute(
                    select(session_table).where(
                        session_table.c.session_id == session_key,
                        session_table.c.owner_id == owner_id,
                    )
                )
                .mappings()
                .first()
            )
            statement = select(raw).where(raw.c.session_id == session_key, raw.c.retired_at.is_(None))
            if after_source_key is not None:
                statement = statement.where(
                    tuple_(
                        raw.c.machine_id,
                        raw.c.provider,
                        raw.c.opaque_source_id,
                        raw.c.source_epoch,
                        raw.c.range_start,
                        raw.c.envelope_id,
                    )
                    > tuple(json.loads(after_source_key))
                )
            rows = (
                list(
                    connection.execute(
                        statement.order_by(
                            raw.c.machine_id.asc(),
                            raw.c.provider.asc(),
                            raw.c.opaque_source_id.asc(),
                            raw.c.source_epoch.asc(),
                            raw.c.range_start.asc(),
                            raw.c.envelope_id.asc(),
                        ).limit(limit + 1)
                    )
                    .mappings()
                    .all()
                )
                if deleted is None and session_row is not None
                else []
            )
            objects_truncated = len(rows) > limit
            rows = rows[:limit]
            return {
                "found": session_row is not None and deleted is None,
                "deleted": deleted is not None,
                "deletion_revision": str(deleted) if deleted is not None else None,
                "session": _storage_session_dto(session_row) if session_row is not None and deleted is None else None,
                "objects": [_raw_object_manifest_dto(row) for row in rows],
                "objects_truncated": objects_truncated,
                "commit_seq": str(_current_commit_seq(connection)),
                "observed_at": observed_at.isoformat(),
            }

    def read_storage_session_render_manifest(
        self,
        *,
        session_id: UUID,
        owner_id: str,
        generation_id: UUID | None,
        after_order_key: str | None,
        before_order_key: str | None,
        limit: int,
    ) -> dict[str, Any]:
        session_table = StorageSession.__table__
        generation_table = RenderGeneration.__table__
        object_table = RenderObject.__table__
        tombstone = LiveSessionTombstone.__table__
        session_key = str(session_id)
        observed_at = datetime.now(UTC)
        with _read_snapshot(self.engine) as connection:
            deleted = connection.execute(
                select(tombstone.c.deletion_revision).where(tombstone.c.session_id == session_key)
            ).scalar_one_or_none()
            session_row = (
                connection.execute(
                    select(session_table).where(
                        session_table.c.session_id == session_key,
                        session_table.c.owner_id == owner_id,
                    )
                )
                .mappings()
                .first()
            )
            current_generation = (
                str(session_row["current_render_generation"])
                if session_row is not None and session_row["current_render_generation"] is not None
                else None
            )
            requested_generation = str(generation_id) if generation_id is not None else current_generation
            stale_generation = generation_id is not None and requested_generation != current_generation
            generation_row = None
            rows: list[Any] = []
            if deleted is None and requested_generation is not None and not stale_generation:
                generation_row = (
                    connection.execute(select(generation_table).where(generation_table.c.generation_id == requested_generation))
                    .mappings()
                    .first()
                )
                statement = select(object_table).where(
                    object_table.c.generation_id == requested_generation,
                    object_table.c.retired_at.is_(None),
                    object_table.c.event_count > 0,
                )
                if after_order_key is not None:
                    after_values = tuple(json.loads(after_order_key))
                    statement = statement.where(
                        tuple_(
                            object_table.c.last_order_time_us,
                            object_table.c.last_machine_id,
                            object_table.c.last_provider,
                            object_table.c.last_opaque_source_id,
                            object_table.c.last_source_epoch,
                            object_table.c.last_source_position,
                            object_table.c.last_event_subordinal,
                        )
                        > after_values
                    )
                if before_order_key is not None:
                    before_values = tuple(json.loads(before_order_key))
                    statement = statement.where(
                        tuple_(
                            object_table.c.first_order_time_us,
                            object_table.c.first_machine_id,
                            object_table.c.first_provider,
                            object_table.c.first_opaque_source_id,
                            object_table.c.first_source_epoch,
                            object_table.c.first_source_position,
                            object_table.c.first_event_subordinal,
                        )
                        < before_values
                    )
                ordering = (
                    (
                        object_table.c.last_order_time_us.desc(),
                        object_table.c.last_machine_id.desc(),
                        object_table.c.last_provider.desc(),
                        object_table.c.last_opaque_source_id.desc(),
                        object_table.c.last_source_epoch.desc(),
                        object_table.c.last_source_position.desc(),
                        object_table.c.last_event_subordinal.desc(),
                        object_table.c.object_id.desc(),
                    )
                    if before_order_key is not None
                    else (
                        object_table.c.first_order_time_us.asc(),
                        object_table.c.first_machine_id.asc(),
                        object_table.c.first_provider.asc(),
                        object_table.c.first_opaque_source_id.asc(),
                        object_table.c.first_source_epoch.asc(),
                        object_table.c.first_source_position.asc(),
                        object_table.c.first_event_subordinal.asc(),
                        object_table.c.object_id.asc(),
                    )
                )
                rows = list(connection.execute(statement.order_by(*ordering).limit(limit + 1)).mappings().all())
            objects_truncated = len(rows) > limit
            if objects_truncated:
                rows = rows[:limit]
            return {
                "found": session_row is not None and deleted is None,
                "deleted": deleted is not None,
                "deletion_revision": str(deleted) if deleted is not None else None,
                "stale_generation": stale_generation,
                "current_generation_id": current_generation,
                "generation": _render_generation_dto(generation_row) if generation_row is not None else None,
                "objects": [_render_object_manifest_dto(row) for row in rows],
                "objects_truncated": objects_truncated,
                "commit_seq": str(_current_commit_seq(connection)),
                "observed_at": observed_at.isoformat(),
            }

    def list_storage_session_render_objects(
        self,
        *,
        session_id: UUID,
        generation_id: UUID | None,
        snapshot_revision: int,
        after_object_id: str | None,
        limit: int,
    ) -> dict[str, Any]:
        """Page one immutable render-object set at a claimed projector revision."""

        session_table = StorageSession.__table__
        generation_table = RenderGeneration.__table__
        object_table = RenderObject.__table__
        tombstone = LiveSessionTombstone.__table__
        session_key = str(session_id)
        observed_at = datetime.now(UTC)
        with _read_snapshot(self.engine) as connection:
            deleted = connection.execute(
                select(tombstone.c.deletion_revision).where(tombstone.c.session_id == session_key)
            ).scalar_one_or_none()
            session_row = connection.execute(select(session_table).where(session_table.c.session_id == session_key)).mappings().first()
            current_generation = (
                str(session_row["current_render_generation"])
                if session_row is not None and session_row["current_render_generation"] is not None
                else None
            )
            selected_generation = str(generation_id) if generation_id is not None else current_generation
            generation_row = (
                connection.execute(
                    select(generation_table).where(
                        generation_table.c.generation_id == selected_generation,
                        generation_table.c.session_id == session_key,
                    )
                )
                .mappings()
                .first()
                if selected_generation is not None
                else None
            )
            base = (
                object_table.c.session_id == session_key,
                object_table.c.generation_id == selected_generation,
                object_table.c.commit_seq <= snapshot_revision,
                or_(
                    object_table.c.retirement_revision.is_(None),
                    object_table.c.retirement_revision > snapshot_revision,
                ),
            )
            rows: list[Any] = []
            snapshot_object_count = 0
            snapshot_event_count = 0
            if deleted is None and session_row is not None and generation_row is not None:
                counts = connection.execute(select(func.count(), func.coalesce(func.sum(object_table.c.event_count), 0)).where(*base)).one()
                snapshot_object_count = int(counts[0])
                snapshot_event_count = int(counts[1])
                statement = select(object_table).where(*base)
                if after_object_id is not None:
                    statement = statement.where(object_table.c.object_id > after_object_id)
                rows = list(connection.execute(statement.order_by(object_table.c.object_id.asc()).limit(limit + 1)).mappings().all())
            has_more = len(rows) > limit
            rows = rows[:limit]
            return {
                "found": session_row is not None and deleted is None,
                "deleted": deleted is not None,
                "deletion_revision": str(deleted) if deleted is not None else None,
                "snapshot_revision": str(snapshot_revision),
                "current_generation_id": current_generation,
                "generation_id": selected_generation,
                "generation": _render_generation_dto(generation_row) if generation_row is not None else None,
                "session": _storage_session_dto(session_row) if session_row is not None and deleted is None else None,
                "snapshot_object_count": snapshot_object_count,
                "snapshot_event_count": snapshot_event_count,
                "objects": [_render_object_manifest_dto(row) for row in rows],
                "has_more": has_more,
                "commit_seq": str(_current_commit_seq(connection)),
                "observed_at": observed_at.isoformat(),
            }

    def commit_media_object(
        self,
        *,
        media_hash: str,
        state: str,
        mime_type: str | None,
        byte_size: int | None,
        object_path: str | None,
        session_refs: tuple[dict[str, Any], ...],
        observed_at: datetime,
    ) -> dict[str, Any]:
        media = MediaObject.__table__
        refs = SessionMediaRef.__table__
        tombstones = LiveSessionTombstone.__table__
        raw = LiveRawObject.__table__
        with _write_transaction(self.engine) as connection:
            for ref in session_refs:
                deleted = connection.execute(
                    select(tombstones.c.deletion_revision).where(tombstones.c.session_id == str(ref["session_id"]))
                ).scalar_one_or_none()
                if deleted is not None:
                    return {"session_deleted": True, "deletion_revision": str(deleted)}
                envelope = ref["envelope_id"]
                if envelope is not None:
                    raw_row = (
                        connection.execute(select(raw.c.session_id, raw.c.retired_at).where(raw.c.envelope_id == envelope))
                        .mappings()
                        .first()
                    )
                    if raw_row is None or raw_row["retired_at"] is not None or str(raw_row["session_id"]) != str(ref["session_id"]):
                        return {"manifest_conflict": True, "commit_seq": str(_current_commit_seq(connection))}

            existing = connection.execute(select(media).where(media.c.media_hash == media_hash)).mappings().first()
            if existing is not None:
                if existing["state"] == "deleted" and state != "deleted":
                    return {"manifest_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
                if existing["byte_size"] is not None and byte_size is not None and existing["byte_size"] != byte_size:
                    return {"manifest_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
                allowed = {
                    "missing": {"missing", "present", "corrupt", "deleted"},
                    "present": {"present", "corrupt", "deleted"},
                    "corrupt": {"corrupt", "present", "deleted"},
                    "deleted": {"deleted"},
                }
                if state not in allowed[str(existing["state"])]:
                    return {"manifest_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
                if state == "deleted":
                    active_ref = connection.execute(
                        select(refs.c.id).where(refs.c.media_hash == media_hash, refs.c.state == "active").limit(1)
                    ).first()
                    if active_ref is not None:
                        return {"manifest_conflict": True, "commit_seq": str(_current_commit_seq(connection))}

            existing_refs: dict[tuple[str, str | None, str], Any] = {}
            if session_refs:
                session_ids = sorted({str(ref["session_id"]) for ref in session_refs})
                for row in connection.execute(
                    select(refs).where(refs.c.media_hash == media_hash, refs.c.session_id.in_(session_ids))
                ).mappings():
                    existing_refs[(str(row["session_id"]), row["envelope_id"], str(row["ref_key"]))] = row

            new_refs: list[dict[str, Any]] = []
            for ref in session_refs:
                key = (str(ref["session_id"]), ref["envelope_id"], str(ref["ref_key"]))
                prior = existing_refs.get(key)
                if prior is not None:
                    if prior["state"] != "active" or prior["retired_at"] is not None:
                        return {
                            "session_deleted": True,
                            "deletion_revision": str(prior["deletion_revision"] or 0),
                        }
                    continue
                new_refs.append(
                    {
                        "session_id": key[0],
                        "media_hash": media_hash,
                        "envelope_id": key[1],
                        "ref_key": key[2],
                    }
                )

            object_changed = existing is None
            if existing is not None:
                object_changed = any(
                    (
                        existing["state"] != state,
                        existing["mime_type"] is None and mime_type is not None,
                        existing["byte_size"] is None and byte_size is not None,
                        existing["object_path"] is None and object_path is not None,
                    )
                )
            if not object_changed and not new_refs:
                selected_refs = [existing_refs[(str(ref["session_id"]), ref["envelope_id"], ref["ref_key"])] for ref in session_refs]
                return {
                    "created": False,
                    "changed": False,
                    "exact_replay": True,
                    "media": _media_object_dto(existing),
                    "refs": [_media_ref_dto(row) for row in selected_refs],
                    "commit_seq": str(existing["commit_seq"]),
                }

            commit_time = datetime.now(UTC)
            commit_seq = _advance_commit_seq(connection, commit_time)
            if existing is None:
                connection.execute(
                    insert(media).values(
                        media_hash=media_hash,
                        state=state,
                        mime_type=mime_type,
                        byte_size=byte_size,
                        object_path=object_path,
                        commit_seq=commit_seq,
                        observed_at=observed_at,
                        verified_at=observed_at if state == "present" else None,
                        deleted_at=observed_at if state == "deleted" else None,
                        created_at=commit_time,
                        updated_at=commit_time,
                    )
                )
            elif object_changed:
                connection.execute(
                    update(media)
                    .where(media.c.media_hash == media_hash)
                    .values(
                        state=state,
                        mime_type=existing["mime_type"] or mime_type,
                        byte_size=existing["byte_size"] if existing["byte_size"] is not None else byte_size,
                        object_path=object_path or existing["object_path"],
                        commit_seq=commit_seq,
                        observed_at=observed_at,
                        verified_at=observed_at if state == "present" else existing["verified_at"],
                        deleted_at=observed_at if state == "deleted" else None,
                        updated_at=commit_time,
                    )
                )
            if new_refs:
                connection.execute(
                    insert(refs),
                    [
                        {
                            **ref,
                            "state": "active",
                            "commit_seq": commit_seq,
                            "created_at": commit_time,
                        }
                        for ref in new_refs
                    ],
                )
            media_row = connection.execute(select(media).where(media.c.media_hash == media_hash)).mappings().one()
            persisted_refs = (
                connection.execute(
                    select(refs).where(
                        refs.c.media_hash == media_hash,
                        refs.c.session_id.in_(sorted({str(ref["session_id"]) for ref in session_refs})),
                    )
                )
                .mappings()
                .all()
                if session_refs
                else []
            )
            persisted_by_key = {(str(row["session_id"]), row["envelope_id"], str(row["ref_key"])): row for row in persisted_refs}
            ref_rows = [persisted_by_key[(str(ref["session_id"]), ref["envelope_id"], str(ref["ref_key"]))] for ref in session_refs]
            return {
                "created": existing is None,
                "changed": True,
                "exact_replay": False,
                "media": _media_object_dto(media_row),
                "refs": [_media_ref_dto(row) for row in ref_rows],
                "commit_seq": str(commit_seq),
            }

    def read_media_object(
        self,
        *,
        media_hash: str,
        session_id: UUID | None,
        limit: int,
    ) -> dict[str, Any]:
        media = MediaObject.__table__
        refs = SessionMediaRef.__table__
        observed_at = datetime.now(UTC)
        with _read_snapshot(self.engine) as connection:
            row = connection.execute(select(media).where(media.c.media_hash == media_hash)).mappings().first()
            if row is None:
                return {
                    "found": False,
                    "commit_seq": str(_current_commit_seq(connection)),
                    "observed_at": observed_at.isoformat(),
                }
            statement = select(refs).where(refs.c.media_hash == media_hash)
            if session_id is not None:
                statement = statement.where(refs.c.session_id == str(session_id))
            ref_rows = connection.execute(statement.order_by(refs.c.id.asc()).limit(limit)).mappings().all()
            return {
                "found": True,
                "media": _media_object_dto(row),
                "refs": [_media_ref_dto(ref) for ref in ref_rows],
                "commit_seq": str(_current_commit_seq(connection)),
                "observed_at": observed_at.isoformat(),
            }

    def media_objects_exist_batch(self, *, media_hashes: tuple[str, ...]) -> dict[str, Any]:
        media = MediaObject.__table__
        observed_at = datetime.now(UTC)
        with _read_snapshot(self.engine) as connection:
            rows = connection.execute(select(media).where(media.c.media_hash.in_(media_hashes))).mappings().all()
            by_hash = {str(row["media_hash"]): row for row in rows}
            return {
                "objects": [
                    {
                        "media_hash": media_hash,
                        "exists": media_hash in by_hash,
                        "state": str(by_hash[media_hash]["state"]) if media_hash in by_hash else "missing",
                        "byte_size": (
                            int(by_hash[media_hash]["byte_size"])
                            if media_hash in by_hash and by_hash[media_hash]["byte_size"] is not None
                            else None
                        ),
                        "mime_type": str(by_hash[media_hash]["mime_type"])
                        if media_hash in by_hash and by_hash[media_hash]["mime_type"]
                        else None,
                        "object_path": (
                            str(by_hash[media_hash]["object_path"])
                            if media_hash in by_hash and by_hash[media_hash]["object_path"]
                            else None
                        ),
                        "commit_seq": str(by_hash[media_hash]["commit_seq"]) if media_hash in by_hash else None,
                    }
                    for media_hash in media_hashes
                ],
                "commit_seq": str(_current_commit_seq(connection)),
                "observed_at": observed_at.isoformat(),
            }

    def advance_projector_state(
        self,
        *,
        projector: str,
        session_id: UUID,
        desired_revision: int,
        observed_at: datetime,
    ) -> dict[str, Any]:
        table = ProjectorState.__table__
        tombstones = LiveSessionTombstone.__table__
        session_key = str(session_id)
        with _write_transaction(self.engine) as connection:
            deletion_revision = connection.execute(
                select(tombstones.c.deletion_revision).where(tombstones.c.session_id == session_key)
            ).scalar_one_or_none()
            if deletion_revision is not None:
                return {
                    "session_deleted": True,
                    "deletion_revision": str(deletion_revision),
                    "commit_seq": str(_current_commit_seq(connection)),
                }
            row = (
                connection.execute(select(table).where(table.c.projector == projector, table.c.session_id == session_key))
                .mappings()
                .first()
            )
            if row is not None and int(row["desired_revision"]) >= desired_revision:
                return {
                    "changed": False,
                    "state": _projector_state_dto(row),
                    "commit_seq": str(row["commit_seq"]),
                }
            commit_time = datetime.now(UTC)
            commit_seq = _advance_commit_seq(connection, commit_time)
            if row is None:
                connection.execute(
                    insert(table).values(
                        projector=projector,
                        session_id=session_key,
                        desired_revision=desired_revision,
                        completed_revision=0,
                        status="idle",
                        failure_count=0,
                        commit_seq=commit_seq,
                        created_at=commit_time,
                        updated_at=commit_time,
                    )
                )
            else:
                connection.execute(
                    update(table)
                    .where(table.c.projector == projector, table.c.session_id == session_key)
                    .values(desired_revision=desired_revision, commit_seq=commit_seq, updated_at=commit_time)
                )
            updated = (
                connection.execute(select(table).where(table.c.projector == projector, table.c.session_id == session_key)).mappings().one()
            )
            return {
                "changed": True,
                "state": _projector_state_dto(updated),
                "commit_seq": str(commit_seq),
            }

    def claim_projector_lag(
        self,
        *,
        projector: str,
        worker_id: str,
        claim_token: str,
        now: datetime,
        lease_seconds: int,
        limit: int,
    ) -> dict[str, Any]:
        table = ProjectorState.__table__
        tombstones = LiveSessionTombstone.__table__
        with _write_transaction(self.engine) as connection:
            replay_rows = (
                connection.execute(
                    select(table)
                    .where(table.c.projector == projector, table.c.claim_token == claim_token)
                    .order_by(table.c.session_id.asc())
                )
                .mappings()
                .all()
            )
            if replay_rows:
                if any(row["worker_id"] != worker_id for row in replay_rows):
                    return {"claim_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
                return {
                    "claimed": [_projector_state_dto(row) for row in replay_rows],
                    "exact_replay": True,
                    "commit_seq": str(replay_rows[0]["commit_seq"]),
                }
            terminal_replay = connection.execute(
                select(table.c.commit_seq).where(
                    table.c.projector == projector,
                    or_(
                        table.c.last_completion_token == claim_token,
                        table.c.last_failure_token == claim_token,
                    ),
                )
            ).first()
            if terminal_replay is not None:
                return {
                    "claimed": [],
                    "exact_replay": True,
                    "commit_seq": str(terminal_replay[0]),
                }
            eligible_predicates = [
                table.c.projector == projector,
                table.c.desired_revision > table.c.completed_revision,
                or_(table.c.claim_expires_at.is_(None), table.c.claim_expires_at <= now),
                or_(table.c.retry_at.is_(None), table.c.retry_at <= now),
            ]
            if projector != "search-v2":
                eligible_predicates.append(~select(tombstones.c.session_id).where(tombstones.c.session_id == table.c.session_id).exists())
            claim_order = (
                (table.c.desired_revision.desc(), table.c.session_id.asc())
                if projector == "search-v2"
                else (table.c.updated_at.asc(), table.c.session_id.asc())
            )
            eligible = connection.execute(select(table).where(*eligible_predicates).order_by(*claim_order).limit(limit)).mappings().all()
            if not eligible:
                return {
                    "claimed": [],
                    "exact_replay": False,
                    "commit_seq": str(_current_commit_seq(connection)),
                }
            commit_time = datetime.now(UTC)
            commit_seq = _advance_commit_seq(connection, commit_time)
            expires_at = now + timedelta(seconds=lease_seconds)
            for row in eligible:
                session_key = row["session_id"]
                connection.execute(
                    update(table)
                    .where(table.c.projector == projector, table.c.session_id == session_key)
                    .values(
                        claimed_revision=row["desired_revision"],
                        claim_token=claim_token,
                        worker_id=worker_id,
                        claim_expires_at=expires_at,
                        status="claimed",
                        retry_at=None,
                        commit_seq=commit_seq,
                        updated_at=commit_time,
                    )
                )
            claimed = (
                connection.execute(
                    select(table)
                    .where(table.c.projector == projector, table.c.claim_token == claim_token)
                    .order_by(table.c.session_id.asc())
                )
                .mappings()
                .all()
            )
            return {
                "claimed": [_projector_state_dto(row) for row in claimed],
                "exact_replay": False,
                "commit_seq": str(commit_seq),
            }

    def complete_projector_claim(
        self,
        *,
        projector: str,
        session_id: UUID,
        claim_token: str,
        completed_revision: int,
        completed_at: datetime,
    ) -> dict[str, Any]:
        table = ProjectorState.__table__
        session_key = str(session_id)
        with _write_transaction(self.engine) as connection:
            row = (
                connection.execute(select(table).where(table.c.projector == projector, table.c.session_id == session_key))
                .mappings()
                .first()
            )
            if row is None:
                return {"claim_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
            if row["last_completion_token"] == claim_token and int(row["completed_revision"]) >= completed_revision:
                return {
                    "changed": False,
                    "exact_replay": True,
                    "state": _projector_state_dto(row),
                    "commit_seq": str(row["commit_seq"]),
                }
            if (
                row["claim_token"] != claim_token
                or row["claimed_revision"] is None
                or int(row["claimed_revision"]) != completed_revision
                or completed_revision > int(row["desired_revision"])
            ):
                return {"claim_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
            commit_time = datetime.now(UTC)
            commit_seq = _advance_commit_seq(connection, commit_time)
            connection.execute(
                update(table)
                .where(table.c.projector == projector, table.c.session_id == session_key)
                .values(
                    completed_revision=completed_revision,
                    claimed_revision=None,
                    claim_token=None,
                    worker_id=None,
                    claim_expires_at=None,
                    status="idle",
                    failure_count=0,
                    last_error_code=None,
                    last_error_message=None,
                    retry_at=None,
                    last_completion_token=claim_token,
                    commit_seq=commit_seq,
                    updated_at=commit_time,
                )
            )
            updated = (
                connection.execute(select(table).where(table.c.projector == projector, table.c.session_id == session_key)).mappings().one()
            )
            return {
                "changed": True,
                "exact_replay": False,
                "state": _projector_state_dto(updated),
                "commit_seq": str(commit_seq),
            }

    def fail_projector_claim(
        self,
        *,
        projector: str,
        session_id: UUID,
        claim_token: str,
        error_code: str,
        error_message: str | None,
        failed_at: datetime,
        retry_at: datetime,
    ) -> dict[str, Any]:
        table = ProjectorState.__table__
        session_key = str(session_id)
        with _write_transaction(self.engine) as connection:
            row = (
                connection.execute(select(table).where(table.c.projector == projector, table.c.session_id == session_key))
                .mappings()
                .first()
            )
            if row is None:
                return {"claim_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
            if row["last_failure_token"] == claim_token:
                return {
                    "changed": False,
                    "exact_replay": True,
                    "state": _projector_state_dto(row),
                    "commit_seq": str(row["commit_seq"]),
                }
            if row["claim_token"] != claim_token or row["claimed_revision"] is None:
                return {"claim_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
            commit_time = datetime.now(UTC)
            commit_seq = _advance_commit_seq(connection, commit_time)
            connection.execute(
                update(table)
                .where(table.c.projector == projector, table.c.session_id == session_key)
                .values(
                    claimed_revision=None,
                    claim_token=None,
                    worker_id=None,
                    claim_expires_at=None,
                    status="failed",
                    failure_count=int(row["failure_count"] or 0) + 1,
                    last_error_code=error_code,
                    last_error_message=error_message,
                    retry_at=retry_at,
                    last_failure_token=claim_token,
                    commit_seq=commit_seq,
                    updated_at=commit_time,
                )
            )
            updated = (
                connection.execute(select(table).where(table.c.projector == projector, table.c.session_id == session_key)).mappings().one()
            )
            return {
                "changed": True,
                "exact_replay": False,
                "state": _projector_state_dto(updated),
                "commit_seq": str(commit_seq),
            }

    def list_projector_lag(
        self,
        *,
        projector: str,
        after_session_id: str | None,
        limit: int,
    ) -> dict[str, Any]:
        table = ProjectorState.__table__
        tombstones = LiveSessionTombstone.__table__
        observed_at = datetime.now(UTC)
        with _read_snapshot(self.engine) as connection:
            lag_predicate = [
                table.c.projector == projector,
                table.c.desired_revision > table.c.completed_revision,
            ]
            if projector != "search-v2":
                lag_predicate.append(~select(tombstones.c.session_id).where(tombstones.c.session_id == table.c.session_id).exists())
            statement = select(table).where(*lag_predicate)
            if after_session_id is not None:
                statement = statement.where(table.c.session_id > after_session_id)
            rows = connection.execute(statement.order_by(table.c.session_id.asc()).limit(limit)).mappings().all()
            lag_count, first_lag_revision = connection.execute(
                select(func.count(), func.min(table.c.desired_revision)).where(*lag_predicate)
            ).one()
            commit_seq = _current_commit_seq(connection)
            return {
                "states": [_projector_state_dto(row) for row in rows],
                "lag_count": int(lag_count),
                "indexed_through": (str(int(first_lag_revision) - 1) if first_lag_revision is not None else str(commit_seq)),
                "commit_seq": str(commit_seq),
                "observed_at": observed_at.isoformat(),
            }

    def bind_projector_store(
        self,
        *,
        projector: str,
        store_id: UUID,
        schema_generation: str,
        observed_at: datetime,
    ) -> dict[str, Any]:
        """Invalidate completed rows exactly once when a disposable store is replaced."""

        bindings = ProjectorStoreBinding.__table__
        states = ProjectorState.__table__
        store_key = str(store_id)
        with _write_transaction(self.engine) as connection:
            existing = connection.execute(select(bindings).where(bindings.c.projector == projector)).mappings().first()
            if existing is not None and str(existing["store_id"]) == store_key and str(existing["schema_generation"]) == schema_generation:
                return {
                    "changed": False,
                    "invalidated_states": 0,
                    "store_id": store_key,
                    "schema_generation": schema_generation,
                    "commit_seq": str(_current_commit_seq(connection)),
                }
            commit_seq = _advance_commit_seq(connection, observed_at)
            invalidated = connection.execute(
                update(states)
                .where(states.c.projector == projector)
                .values(
                    completed_revision=0,
                    claimed_revision=None,
                    claim_token=None,
                    worker_id=None,
                    claim_expires_at=None,
                    status="idle",
                    failure_count=0,
                    last_error_code=None,
                    last_error_message=None,
                    retry_at=None,
                    last_completion_token=None,
                    last_failure_token=None,
                    commit_seq=commit_seq,
                    updated_at=observed_at,
                )
            ).rowcount
            if existing is None:
                connection.execute(
                    insert(bindings).values(
                        projector=projector,
                        store_id=store_key,
                        schema_generation=schema_generation,
                        commit_seq=commit_seq,
                        created_at=observed_at,
                        updated_at=observed_at,
                    )
                )
            else:
                connection.execute(
                    update(bindings)
                    .where(bindings.c.projector == projector)
                    .values(
                        store_id=store_key,
                        schema_generation=schema_generation,
                        commit_seq=commit_seq,
                        updated_at=observed_at,
                    )
                )
            return {
                "changed": True,
                "invalidated_states": int(invalidated or 0),
                "store_id": store_key,
                "schema_generation": schema_generation,
                "commit_seq": str(commit_seq),
            }

    def create_legacy_migration_run(
        self,
        *,
        run_id: UUID,
        legacy_high_watermark: str,
        expected_session_count: int,
        created_at: datetime,
    ) -> dict[str, Any]:
        runs = LegacyMigrationRun.__table__
        run_key = str(run_id)
        with _write_transaction(self.engine) as connection:
            row = connection.execute(select(runs).where(runs.c.run_id == run_key)).mappings().first()
            if row is not None:
                if row["legacy_high_watermark"] != legacy_high_watermark or int(row["expected_session_count"]) != expected_session_count:
                    return {"run_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
                return {
                    "created": False,
                    "exact_replay": True,
                    "run": _legacy_migration_run_dto(row),
                    "commit_seq": str(row["commit_seq"]),
                }
            commit_seq = _advance_commit_seq(connection, created_at)
            connection.execute(
                insert(runs).values(
                    run_id=run_key,
                    legacy_high_watermark=legacy_high_watermark,
                    expected_session_count=expected_session_count,
                    state="complete" if expected_session_count == 0 else "inventory",
                    commit_seq=commit_seq,
                    created_at=created_at,
                    updated_at=created_at,
                    completed_at=created_at if expected_session_count == 0 else None,
                )
            )
            row = connection.execute(select(runs).where(runs.c.run_id == run_key)).mappings().one()
            return {
                "created": True,
                "exact_replay": False,
                "run": _legacy_migration_run_dto(row),
                "commit_seq": str(commit_seq),
            }

    def register_legacy_migration_sessions(
        self,
        *,
        run_id: UUID,
        sessions: list[dict[str, Any]],
        registered_at: datetime,
    ) -> dict[str, Any]:
        runs = LegacyMigrationRun.__table__
        rows = LegacyMigrationSession.__table__
        run_key = str(run_id)
        with _write_transaction(self.engine) as connection:
            run = connection.execute(select(runs).where(runs.c.run_id == run_key)).mappings().first()
            if run is None:
                return {"run_missing": True, "commit_seq": str(_current_commit_seq(connection))}
            if run["state"] in {"complete", "degraded"}:
                return {"run_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
            session_ids = [str(item["session_id"]) for item in sessions]
            existing = {
                str(row["session_id"]): row
                for row in connection.execute(select(rows).where(rows.c.run_id == run_key, rows.c.session_id.in_(session_ids)))
                .mappings()
                .all()
            }
            new_rows: list[dict[str, Any]] = []
            for item in sessions:
                session_key = str(item["session_id"])
                current = existing.get(session_key)
                if current is not None:
                    if (
                        int(current["source_expected"]) != item["source_expected"]
                        or int(current["media_expected"]) != item["media_expected"]
                    ):
                        return {"session_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
                    continue
                new_rows.append(item)
            registered_count = int(connection.execute(select(func.count()).select_from(rows).where(rows.c.run_id == run_key)).scalar_one())
            if registered_count + len(new_rows) > int(run["expected_session_count"]):
                return {"run_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
            if not new_rows:
                return {
                    "registered": 0,
                    "exact_replay": True,
                    "registered_session_count": registered_count,
                    "commit_seq": str(_current_commit_seq(connection)),
                }
            commit_seq = _advance_commit_seq(connection, registered_at)
            connection.execute(
                insert(rows),
                [
                    {
                        "run_id": run_key,
                        "session_id": str(item["session_id"]),
                        "state": "pending",
                        "source_expected": item["source_expected"],
                        "media_expected": item["media_expected"],
                        "commit_seq": commit_seq,
                        "created_at": registered_at,
                        "updated_at": registered_at,
                    }
                    for item in new_rows
                ],
            )
            total = registered_count + len(new_rows)
            connection.execute(
                update(runs)
                .where(runs.c.run_id == run_key)
                .values(
                    state="migrating" if total == int(run["expected_session_count"]) else "inventory",
                    commit_seq=commit_seq,
                    updated_at=registered_at,
                )
            )
            return {
                "registered": len(new_rows),
                "exact_replay": False,
                "registered_session_count": total,
                "commit_seq": str(commit_seq),
            }

    def read_legacy_migration_run(self, *, run_id: UUID) -> dict[str, Any]:
        runs = LegacyMigrationRun.__table__
        with _read_snapshot(self.engine) as connection:
            row = connection.execute(select(runs).where(runs.c.run_id == str(run_id))).mappings().first()
            if row is None:
                return {"run": None, "commit_seq": str(_current_commit_seq(connection))}
            return {
                "run": _legacy_migration_run_dto(row),
                "summary": _legacy_migration_summary(connection, str(run_id)),
                "commit_seq": str(_current_commit_seq(connection)),
            }

    def claim_legacy_migration_sessions(
        self,
        *,
        run_id: UUID,
        worker_id: str,
        claim_token: str,
        now: datetime,
        lease_seconds: int,
        limit: int,
    ) -> dict[str, Any]:
        runs = LegacyMigrationRun.__table__
        rows = LegacyMigrationSession.__table__
        run_key = str(run_id)
        with _write_transaction(self.engine) as connection:
            run = connection.execute(select(runs).where(runs.c.run_id == run_key)).mappings().first()
            if run is None:
                return {"run_missing": True, "commit_seq": str(_current_commit_seq(connection))}
            replay = (
                connection.execute(
                    select(rows).where(rows.c.run_id == run_key, rows.c.claim_token == claim_token).order_by(rows.c.session_id)
                )
                .mappings()
                .all()
            )
            if replay:
                if any(row["worker_id"] != worker_id for row in replay):
                    return {"claim_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
                return {
                    "claimed": [_legacy_migration_session_dto(row) for row in replay],
                    "exact_replay": True,
                    "commit_seq": str(replay[0]["commit_seq"]),
                }
            terminal = connection.execute(
                select(rows.c.commit_seq).where(
                    rows.c.run_id == run_key,
                    or_(rows.c.last_completion_token == claim_token, rows.c.last_failure_token == claim_token),
                )
            ).first()
            if terminal is not None:
                return {"claimed": [], "exact_replay": True, "commit_seq": str(terminal[0])}
            eligible = (
                connection.execute(
                    select(rows)
                    .where(
                        rows.c.run_id == run_key,
                        or_(
                            rows.c.state == "pending",
                            and_(rows.c.state == "migrating", rows.c.lease_expires_at <= now),
                            and_(rows.c.state == "degraded", rows.c.retry_at.is_not(None), rows.c.retry_at <= now),
                        ),
                    )
                    .order_by(rows.c.updated_at.asc(), rows.c.session_id.asc())
                    .limit(limit)
                )
                .mappings()
                .all()
            )
            if not eligible:
                return {"claimed": [], "exact_replay": False, "commit_seq": str(_current_commit_seq(connection))}
            commit_seq = _advance_commit_seq(connection, now)
            expires_at = now + timedelta(seconds=lease_seconds)
            ids = [row["session_id"] for row in eligible]
            connection.execute(
                update(rows)
                .where(rows.c.run_id == run_key, rows.c.session_id.in_(ids))
                .values(
                    state="migrating",
                    claim_token=claim_token,
                    worker_id=worker_id,
                    lease_expires_at=expires_at,
                    retry_at=None,
                    attempts=rows.c.attempts + 1,
                    commit_seq=commit_seq,
                    updated_at=now,
                )
            )
            connection.execute(
                update(runs)
                .where(runs.c.run_id == run_key)
                .values(state="migrating", commit_seq=commit_seq, updated_at=now, completed_at=None)
            )
            claimed = (
                connection.execute(
                    select(rows).where(rows.c.run_id == run_key, rows.c.claim_token == claim_token).order_by(rows.c.session_id)
                )
                .mappings()
                .all()
            )
            return {
                "claimed": [_legacy_migration_session_dto(row) for row in claimed],
                "exact_replay": False,
                "commit_seq": str(commit_seq),
            }

    def complete_legacy_migration_session(
        self,
        *,
        run_id: UUID,
        session_id: UUID,
        claim_token: str,
        source_covered: int,
        source_missing: int,
        media_covered: int,
        media_missing: int,
        output_proof_hash: str,
        parity_proof_hash: str,
        render_generation_id: UUID | None,
        degradation_code: str | None,
        degradation_message: str | None,
        completed_at: datetime,
    ) -> dict[str, Any]:
        rows = LegacyMigrationSession.__table__
        run_key, session_key = str(run_id), str(session_id)
        with _write_transaction(self.engine) as connection:
            row = connection.execute(select(rows).where(rows.c.run_id == run_key, rows.c.session_id == session_key)).mappings().first()
            if row is None:
                return {"session_missing": True, "commit_seq": str(_current_commit_seq(connection))}
            if degradation_code is None and (source_missing or media_missing):
                if source_missing and media_missing:
                    degradation_code = "source_and_media_coverage_missing"
                elif source_missing:
                    degradation_code = "source_coverage_missing"
                else:
                    degradation_code = "media_coverage_missing"
                degradation_message = f"source_missing={source_missing}; media_missing={media_missing}"
            if row["last_completion_token"] == claim_token:
                if (
                    int(row["source_covered"]) != source_covered
                    or int(row["source_missing"]) != source_missing
                    or int(row["media_covered"]) != media_covered
                    or int(row["media_missing"]) != media_missing
                    or row["output_proof_hash"] != output_proof_hash
                    or row["parity_proof_hash"] != parity_proof_hash
                    or row["error_code"] != degradation_code
                    or row["error_message"] != degradation_message
                ):
                    return {"claim_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
                return {
                    "changed": False,
                    "exact_replay": True,
                    "session": _legacy_migration_session_dto(row),
                    "commit_seq": str(row["commit_seq"]),
                }
            if row["claim_token"] != claim_token:
                return {"claim_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
            if source_covered + source_missing != int(row["source_expected"]) or media_covered + media_missing != int(
                row["media_expected"]
            ):
                return {"coverage_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
            generation = RenderGeneration.__table__
            generation_key = str(render_generation_id) if render_generation_id is not None else None
            if generation_key is not None:
                generation_row = (
                    connection.execute(
                        select(generation).where(
                            generation.c.generation_id == generation_key,
                            generation.c.session_id == session_key,
                        )
                    )
                    .mappings()
                    .first()
                )
                if generation_row is None or generation_row["state"] != "pending":
                    return {"render_generation_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
            state = "verified" if degradation_code is None else "degraded"
            commit_seq = _advance_commit_seq(connection, completed_at)
            if generation_key is not None:
                connection.execute(
                    update(generation)
                    .where(
                        generation.c.session_id == session_key,
                        generation.c.generation_id != generation_key,
                        generation.c.state == "current",
                    )
                    .values(
                        state="superseded",
                        superseded_at=completed_at,
                        commit_seq=commit_seq,
                        updated_at=completed_at,
                    )
                )
                _recompute_render_generation_projection(
                    connection,
                    session_id=session_key,
                    generation_id=generation_key,
                    commit_seq=commit_seq,
                    commit_time=completed_at,
                )
                connection.execute(
                    update(StorageSession.__table__)
                    .where(StorageSession.__table__.c.session_id == session_key)
                    .values(render_state="ready", commit_seq=commit_seq, updated_at=completed_at)
                )
                projector = ProjectorState.__table__
                projector_row = (
                    connection.execute(
                        select(projector).where(
                            projector.c.projector == "search-v2",
                            projector.c.session_id == session_key,
                        )
                    )
                    .mappings()
                    .first()
                )
                if projector_row is None:
                    connection.execute(
                        insert(projector).values(
                            projector="search-v2",
                            session_id=session_key,
                            desired_revision=commit_seq,
                            completed_revision=0,
                            status="idle",
                            failure_count=0,
                            commit_seq=commit_seq,
                            created_at=completed_at,
                            updated_at=completed_at,
                        )
                    )
                else:
                    connection.execute(
                        update(projector)
                        .where(projector.c.projector == "search-v2", projector.c.session_id == session_key)
                        .values(desired_revision=commit_seq, commit_seq=commit_seq, updated_at=completed_at)
                    )
            connection.execute(
                update(rows)
                .where(rows.c.run_id == run_key, rows.c.session_id == session_key)
                .values(
                    state=state,
                    source_covered=source_covered,
                    source_missing=source_missing,
                    media_covered=media_covered,
                    media_missing=media_missing,
                    output_proof_hash=output_proof_hash,
                    parity_proof_hash=parity_proof_hash,
                    error_code=degradation_code,
                    error_message=degradation_message,
                    claim_token=None,
                    worker_id=None,
                    lease_expires_at=None,
                    retry_at=None,
                    last_completion_token=claim_token,
                    commit_seq=commit_seq,
                    updated_at=completed_at,
                    verified_at=completed_at,
                )
            )
            _refresh_legacy_migration_run(connection, run_key, commit_seq, completed_at)
            updated = connection.execute(select(rows).where(rows.c.run_id == run_key, rows.c.session_id == session_key)).mappings().one()
            return {
                "changed": True,
                "exact_replay": False,
                "session": _legacy_migration_session_dto(updated),
                "commit_seq": str(commit_seq),
            }

    def fail_legacy_migration_session(
        self,
        *,
        run_id: UUID,
        session_id: UUID,
        claim_token: str,
        error_code: str,
        error_message: str | None,
        failed_at: datetime,
        retry_at: datetime,
    ) -> dict[str, Any]:
        rows = LegacyMigrationSession.__table__
        run_key, session_key = str(run_id), str(session_id)
        with _write_transaction(self.engine) as connection:
            row = connection.execute(select(rows).where(rows.c.run_id == run_key, rows.c.session_id == session_key)).mappings().first()
            if row is None:
                return {"session_missing": True, "commit_seq": str(_current_commit_seq(connection))}
            if row["last_failure_token"] == claim_token:
                if (
                    row["error_code"] != error_code
                    or row["error_message"] != error_message
                    or _as_aware_utc(row["updated_at"]) != failed_at
                    or _as_aware_utc(row["retry_at"]) != retry_at
                ):
                    return {"claim_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
                return {
                    "changed": False,
                    "exact_replay": True,
                    "session": _legacy_migration_session_dto(row),
                    "commit_seq": str(row["commit_seq"]),
                }
            if row["claim_token"] != claim_token:
                return {"claim_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
            commit_seq = _advance_commit_seq(connection, failed_at)
            connection.execute(
                update(rows)
                .where(rows.c.run_id == run_key, rows.c.session_id == session_key)
                .values(
                    state="degraded",
                    error_code=error_code,
                    error_message=error_message,
                    claim_token=None,
                    worker_id=None,
                    lease_expires_at=None,
                    retry_at=retry_at,
                    last_failure_token=claim_token,
                    commit_seq=commit_seq,
                    updated_at=failed_at,
                    verified_at=None,
                )
            )
            _refresh_legacy_migration_run(connection, run_key, commit_seq, failed_at)
            updated = connection.execute(select(rows).where(rows.c.run_id == run_key, rows.c.session_id == session_key)).mappings().one()
            return {
                "changed": True,
                "exact_replay": False,
                "session": _legacy_migration_session_dto(updated),
                "commit_seq": str(commit_seq),
            }

    def repair_legacy_migration_render(
        self,
        *,
        run_id: UUID,
        session_ids: tuple[UUID, ...],
        parser_revision: str,
        ordering_revision: str,
        observed_at: datetime,
    ) -> dict[str, Any]:
        """Requeue explicitly failed migration renders without direct catalog SQL."""

        runs = LegacyMigrationRun.__table__
        rows = LegacyMigrationSession.__table__
        generations = RenderGeneration.__table__
        objects = RenderObject.__table__
        sessions = StorageSession.__table__
        projectors = ProjectorState.__table__
        run_key = str(run_id)
        session_keys = tuple(str(value) for value in session_ids)
        with _write_transaction(self.engine) as connection:
            if connection.execute(select(runs.c.run_id).where(runs.c.run_id == run_key)).first() is None:
                return {"run_missing": True, "commit_seq": str(_current_commit_seq(connection))}
            selected = list(
                connection.execute(select(rows).where(rows.c.run_id == run_key, rows.c.session_id.in_(session_keys))).mappings().all()
            )
            session_states = {
                str(row["session_id"]): row
                for row in connection.execute(
                    select(
                        sessions.c.session_id,
                        sessions.c.render_state,
                        sessions.c.current_render_generation,
                    ).where(sessions.c.session_id.in_(session_keys))
                )
                .mappings()
                .all()
            }
            eligible = {
                str(row["session_id"])
                for row in selected
                if row["state"] == "degraded"
                and (
                    row["error_code"] == "render_projection_failed"
                    or (
                        int(row["attempts"]) >= 2
                        and (session_state := session_states.get(str(row["session_id"]))) is not None
                        and session_state["render_state"] == "pending"
                        and session_state["current_render_generation"] is None
                    )
                )
            }
            conflicts = sorted(set(session_keys) - eligible)
            if conflicts:
                return {"sessions_conflict": conflicts, "commit_seq": str(_current_commit_seq(connection))}
            generation_rows = list(
                connection.execute(
                    select(generations).where(
                        generations.c.session_id.in_(session_keys),
                        generations.c.parser_revision == parser_revision,
                        generations.c.ordering_revision == ordering_revision,
                        generations.c.state.in_(("pending", "current")),
                    )
                )
                .mappings()
                .all()
            )
            generation_ids = tuple(str(row["generation_id"]) for row in generation_rows)
            commit_seq = _advance_commit_seq(connection, observed_at)
            if generation_ids:
                connection.execute(delete(objects).where(objects.c.generation_id.in_(generation_ids)))
                connection.execute(delete(generations).where(generations.c.generation_id.in_(generation_ids)))
            connection.execute(
                update(sessions)
                .where(sessions.c.session_id.in_(session_keys))
                .values(
                    current_render_generation=None,
                    render_state="pending",
                    user_messages=0,
                    assistant_messages=0,
                    tool_calls=0,
                    first_user_message_preview=None,
                    last_visible_text_preview=None,
                    commit_seq=commit_seq,
                    updated_at=observed_at,
                )
            )
            connection.execute(
                update(rows)
                .where(rows.c.run_id == run_key, rows.c.session_id.in_(session_keys))
                .values(
                    state="pending",
                    source_covered=0,
                    source_missing=0,
                    media_covered=0,
                    media_missing=0,
                    output_proof_hash=None,
                    parity_proof_hash=None,
                    error_code=None,
                    error_message=None,
                    claim_token=None,
                    worker_id=None,
                    lease_expires_at=None,
                    retry_at=None,
                    verified_at=None,
                    commit_seq=commit_seq,
                    updated_at=observed_at,
                )
            )
            for session_key in session_keys:
                projector_row = connection.execute(
                    select(projectors).where(
                        projectors.c.projector == "search-v2",
                        projectors.c.session_id == session_key,
                    )
                ).first()
                if projector_row is None:
                    connection.execute(
                        insert(projectors).values(
                            projector="search-v2",
                            session_id=session_key,
                            desired_revision=commit_seq,
                            completed_revision=0,
                            status="idle",
                            failure_count=0,
                            commit_seq=commit_seq,
                            created_at=observed_at,
                            updated_at=observed_at,
                        )
                    )
                else:
                    connection.execute(
                        update(projectors)
                        .where(projectors.c.projector == "search-v2", projectors.c.session_id == session_key)
                        .values(desired_revision=commit_seq, commit_seq=commit_seq, updated_at=observed_at)
                    )
            _refresh_legacy_migration_run(connection, run_key, commit_seq, observed_at)
            return {
                "repaired": len(session_keys),
                "session_ids": list(session_keys),
                "retired_generations": len(generation_ids),
                "commit_seq": str(commit_seq),
            }

    def summarize_legacy_migration_run(self, *, run_id: UUID) -> dict[str, Any]:
        runs = LegacyMigrationRun.__table__
        with _read_snapshot(self.engine) as connection:
            run = connection.execute(select(runs).where(runs.c.run_id == str(run_id))).mappings().first()
            if run is None:
                return {"run_missing": True, "commit_seq": str(_current_commit_seq(connection))}
            return {
                "run": _legacy_migration_run_dto(run),
                "summary": _legacy_migration_summary(connection, str(run_id)),
                "commit_seq": str(_current_commit_seq(connection)),
            }

    def reconcile_legacy_migration_run(
        self,
        *,
        run_id: UUID,
        observed_at: datetime,
        release_claims: bool,
    ) -> dict[str, Any]:
        """Classify legacy coverage gaps and optionally requeue stopped-worker claims."""

        runs = LegacyMigrationRun.__table__
        rows = LegacyMigrationSession.__table__
        run_key = str(run_id)
        with _write_transaction(self.engine) as connection:
            if connection.execute(select(runs.c.run_id).where(runs.c.run_id == run_key)).first() is None:
                return {"run_missing": True, "commit_seq": str(_current_commit_seq(connection))}
            coverage = and_(
                rows.c.run_id == run_key,
                rows.c.state == "degraded",
                rows.c.retry_at.is_(None),
                rows.c.error_code.is_(None),
            )
            categories = (
                (
                    and_(rows.c.source_missing > 0, rows.c.media_missing > 0),
                    "source_and_media_coverage_missing",
                    "source and media coverage missing; see counters",
                ),
                (rows.c.source_missing > 0, "source_coverage_missing", "source coverage missing; see counters"),
                (rows.c.media_missing > 0, "media_coverage_missing", "media coverage missing; see counters"),
            )
            classified = 0
            commit_seq = _current_commit_seq(connection)
            for predicate, code, message in categories:
                result = connection.execute(
                    update(rows).where(coverage, predicate).values(error_code=code, error_message=message, updated_at=observed_at)
                )
                classified += int(result.rowcount or 0)
            released = 0
            if release_claims:
                result = connection.execute(
                    update(rows)
                    .where(rows.c.run_id == run_key, rows.c.state == "migrating")
                    .values(
                        state="pending",
                        claim_token=None,
                        worker_id=None,
                        lease_expires_at=None,
                        retry_at=None,
                        updated_at=observed_at,
                    )
                )
                released = int(result.rowcount or 0)
            if classified or released:
                commit_seq = _advance_commit_seq(connection, observed_at)
                connection.execute(
                    update(rows).where(rows.c.run_id == run_key, rows.c.updated_at == observed_at).values(commit_seq=commit_seq)
                )
                _refresh_legacy_migration_run(connection, run_key, commit_seq, observed_at)
            return {
                "classified": classified,
                "released_claims": released,
                "summary": _legacy_migration_summary(connection, run_key),
                "commit_seq": str(commit_seq),
            }

    def list_legacy_migration_gaps(self, *, run_id: UUID, after_session_id: str | None, limit: int) -> dict[str, Any]:
        runs = LegacyMigrationRun.__table__
        rows = LegacyMigrationSession.__table__
        run_key = str(run_id)
        with _read_snapshot(self.engine) as connection:
            if connection.execute(select(runs.c.run_id).where(runs.c.run_id == run_key)).first() is None:
                return {"run_missing": True, "commit_seq": str(_current_commit_seq(connection))}
            statement = select(rows).where(rows.c.run_id == run_key, rows.c.state != "verified")
            if after_session_id is not None:
                statement = statement.where(rows.c.session_id > after_session_id)
            result = connection.execute(statement.order_by(rows.c.session_id.asc()).limit(limit)).mappings().all()
            return {
                "gaps": [_legacy_migration_session_dto(row) for row in result],
                "next_after_session_id": str(result[-1]["session_id"]) if len(result) == limit else None,
                "commit_seq": str(_current_commit_seq(connection)),
            }

    def checkpoint_passive(self) -> dict[str, int]:
        """Run a non-blocking WAL checkpoint owned by catalogd."""

        with self.engine.connect() as connection:
            busy, log_frames, checkpointed_frames = connection.exec_driver_sql("PRAGMA wal_checkpoint(PASSIVE)").one()
        return {
            "busy": int(busy),
            "log_frames": int(log_frames),
            "checkpointed_frames": int(checkpointed_frames),
        }


def _storage_catalog_compat_row(row) -> dict[str, Any]:
    title = str(row["summary_title"] or "").strip() or sanitize_title(row["first_user_message_preview"], max_words=6)
    return {
        "session_id": str(row["session_id"]),
        "provider": str(row["provider"]),
        "environment": str(row["environment"]),
        "project": row["project"],
        "device_id": str(row["machine_id"]),
        "device_name": str(row["machine_id"]),
        "cwd": row["cwd"],
        "git_repo": row["git_repo"],
        "git_branch": row["git_branch"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "closed_at": row["ended_at"],
        "close_reason": None,
        "last_activity_at": row["last_activity_at"],
        "user_messages": int(row["user_messages"]),
        "assistant_messages": int(row["assistant_messages"]),
        "tool_calls": int(row["tool_calls"]),
        "summary": None,
        "summary_title": title,
        "anchor_title": title,
        "first_user_message_preview": row["first_user_message_preview"],
        "transcript_revision": int(row["transcript_revision"]),
        "summary_revision": 0,
        "user_state": str(row["user_state"]),
        "user_state_at": row["updated_at"],
        "primary_thread_id": None,
        "loop_mode": str(row["loop_mode"]),
        "notification_muted": bool(row["notification_muted"]),
        "origin_kind": row["origin_kind"],
        "hidden_from_default_timeline": int(row["hidden_from_default_timeline"]),
        "launch_actor": row["launch_actor"],
        "launch_surface": row["launch_surface"],
        "permission_mode": "bypass",
    }


def _legacy_migration_run_dto(row) -> dict[str, Any]:
    return {
        "run_id": str(row["run_id"]),
        "legacy_high_watermark": str(row["legacy_high_watermark"]),
        "expected_session_count": int(row["expected_session_count"]),
        "state": str(row["state"]),
        "commit_seq": str(row["commit_seq"]),
        "created_at": _encode_datetime(row["created_at"]),
        "updated_at": _encode_datetime(row["updated_at"]),
        "completed_at": _encode_datetime(row["completed_at"]),
    }


def _legacy_migration_session_dto(row) -> dict[str, Any]:
    return {
        "run_id": str(row["run_id"]),
        "session_id": str(row["session_id"]),
        "state": str(row["state"]),
        "source_expected": int(row["source_expected"]),
        "source_covered": int(row["source_covered"]),
        "source_missing": int(row["source_missing"]),
        "media_expected": int(row["media_expected"]),
        "media_covered": int(row["media_covered"]),
        "media_missing": int(row["media_missing"]),
        "output_proof_hash": row["output_proof_hash"],
        "parity_proof_hash": row["parity_proof_hash"],
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "attempts": int(row["attempts"]),
        "claim_token": row["claim_token"],
        "worker_id": row["worker_id"],
        "lease_expires_at": _encode_datetime(row["lease_expires_at"]),
        "retry_at": _encode_datetime(row["retry_at"]),
        "commit_seq": str(row["commit_seq"]),
        "created_at": _encode_datetime(row["created_at"]),
        "updated_at": _encode_datetime(row["updated_at"]),
        "verified_at": _encode_datetime(row["verified_at"]),
    }


def _legacy_migration_summary(connection, run_id: str) -> dict[str, Any]:
    rows = LegacyMigrationSession.__table__
    state_counts = {
        str(state): int(count)
        for state, count in connection.execute(
            select(rows.c.state, func.count()).where(rows.c.run_id == run_id).group_by(rows.c.state)
        ).all()
    }
    totals = connection.execute(
        select(
            func.count(),
            func.coalesce(func.sum(rows.c.source_expected), 0),
            func.coalesce(func.sum(rows.c.source_covered), 0),
            func.coalesce(func.sum(rows.c.source_missing), 0),
            func.coalesce(func.sum(rows.c.media_expected), 0),
            func.coalesce(func.sum(rows.c.media_covered), 0),
            func.coalesce(func.sum(rows.c.media_missing), 0),
        ).where(rows.c.run_id == run_id)
    ).one()
    return {
        "registered_session_count": int(totals[0]),
        "state_counts": {state: state_counts.get(state, 0) for state in ("pending", "migrating", "verified", "degraded")},
        "source_expected": int(totals[1]),
        "source_covered": int(totals[2]),
        "source_missing": int(totals[3]),
        "media_expected": int(totals[4]),
        "media_covered": int(totals[5]),
        "media_missing": int(totals[6]),
    }


def _refresh_legacy_migration_run(connection, run_id: str, commit_seq: int, observed_at: datetime) -> None:
    runs = LegacyMigrationRun.__table__
    rows = LegacyMigrationSession.__table__
    run = connection.execute(select(runs).where(runs.c.run_id == run_id)).mappings().one()
    expected = int(run["expected_session_count"])
    if run["state"] == "inventory":
        registered = int(connection.execute(select(func.count()).select_from(rows).where(rows.c.run_id == run_id)).scalar_one())
        if registered < expected:
            state, completed_at = "inventory", None
        else:
            state, completed_at = "migrating", None
    elif connection.execute(
        select(rows.c.session_id).where(rows.c.run_id == run_id, rows.c.state.in_(("pending", "migrating"))).limit(1)
    ).first():
        state, completed_at = "migrating", None
    elif connection.execute(select(rows.c.session_id).where(rows.c.run_id == run_id, rows.c.state == "degraded").limit(1)).first():
        state, completed_at = "degraded", observed_at
    else:
        state, completed_at = "complete", observed_at
    connection.execute(
        update(runs)
        .where(runs.c.run_id == run_id)
        .values(state=state, commit_seq=commit_seq, updated_at=observed_at, completed_at=completed_at)
    )


def _storage_card_compat_row(row) -> dict[str, Any]:
    title = str(row["summary_title"] or "").strip() or sanitize_title(row["first_user_message_preview"], max_words=6)
    return {
        "session_id": str(row["session_id"]),
        "last_activity_at": row["last_activity_at"],
        "summary_title": title,
        "first_user_message_preview": row["first_user_message_preview"],
        "user_messages": int(row["user_messages"]),
        "assistant_messages": int(row["assistant_messages"]),
        "tool_calls": int(row["tool_calls"]),
        "transcript_revision": int(row["transcript_revision"]),
        "archive_state": "current" if row["render_state"] == "ready" else "pending",
    }


def _assemble_session_facts(
    connection,
    *,
    session_ids: list[str],
    observed_at: datetime,
    compact: bool,
) -> list[dict[str, Any]]:
    """Bulk-load response-relevant session facts without presentation inference."""

    if not session_ids:
        return []
    catalog_table = LiveSessionCatalog.__table__
    card_table = LiveTimelineCard.__table__
    runtime_table = LiveRuntimeState.__table__
    readiness_table = LiveLaunchReadiness.__table__
    thread_table = LiveSessionThread.__table__
    run_table = LiveSessionRun.__table__
    connection_table = LiveSessionConnection.__table__
    control_lease_table = LiveControlLease.__table__
    live_preview_table = LiveSessionLivePreview.__table__
    alias_table = LiveSessionThreadAlias.__table__
    console_turn_table = LiveConsoleTurn.__table__
    storage_table = StorageSession.__table__
    live_session_table = LiveSession.__table__
    tombstone_table = LiveSessionTombstone.__table__

    catalogs = {
        str(row["session_id"]): row
        for row in connection.execute(select(catalog_table).where(catalog_table.c.session_id.in_(session_ids))).mappings()
    }
    cards = {
        str(row["session_id"]): row
        for row in connection.execute(select(card_table).where(card_table.c.session_id.in_(session_ids))).mappings()
    }
    storage_rows = {
        str(row["session_id"]): row
        for row in connection.execute(
            select(storage_table).where(
                storage_table.c.session_id.in_(session_ids),
                ~select(tombstone_table.c.session_id).where(tombstone_table.c.session_id == storage_table.c.session_id).exists(),
            )
        ).mappings()
    }
    owner_by_session = {
        str(row["session_id"]): int(row["owner_id"])
        for row in connection.execute(
            select(live_session_table.c.session_id, live_session_table.c.owner_id).where(live_session_table.c.session_id.in_(session_ids))
        ).mappings()
        if row["owner_id"] is not None
    }
    for session_id, row in storage_rows.items():
        if row.get("owner_id") is not None:
            owner_by_session.setdefault(session_id, int(row["owner_id"]))
    missing_console_owner_ids = [
        session_id for session_id, row in catalogs.items() if session_id not in owner_by_session and row.get("origin_kind") == "console"
    ]
    if missing_console_owner_ids:
        outbox_table = LiveArchiveOutbox.__table__
        keys = [f"console_session_create.v1:{session_id}" for session_id in missing_console_owner_ids]
        for row in connection.execute(
            select(
                outbox_table.c.idempotency_key,
                func.json_extract(outbox_table.c.payload_json, "$.session.owner_id").label("owner_id"),
            ).where(outbox_table.c.idempotency_key.in_(keys))
        ).mappings():
            if row["owner_id"] is not None:
                owner_by_session[str(row["idempotency_key"]).rsplit(":", 1)[-1]] = int(row["owner_id"])
    for session_id, row in storage_rows.items():
        catalogs[session_id] = _storage_catalog_compat_row(row)
        cards[session_id] = _storage_card_compat_row(row)
    runtime_by_session: dict[str, Any] = {}
    for row in connection.execute(
        select(runtime_table)
        .where(runtime_table.c.session_id.in_(session_ids))
        .order_by(
            runtime_table.c.updated_at.desc(),
            runtime_table.c.runtime_version.desc(),
            runtime_table.c.runtime_key.desc(),
        )
    ).mappings():
        runtime_by_session.setdefault(str(row["session_id"]), row)
    readiness_by_session = {
        str(row["session_id"]): row
        for row in connection.execute(select(readiness_table).where(readiness_table.c.session_id.in_(session_ids))).mappings()
        if _as_aware_utc(row["expires_at"]) is None or _as_aware_utc(row["expires_at"]) > observed_at
    }

    thread_rows = list(
        connection.execute(
            select(thread_table)
            .where(thread_table.c.session_id.in_(session_ids))
            .order_by(thread_table.c.created_at.asc(), thread_table.c.id.asc())
        ).mappings()
    )
    threads_by_session: dict[str, list[Any]] = {}
    for row in thread_rows:
        threads_by_session.setdefault(str(row["session_id"]), []).append(row)
    primary_by_session: dict[str, Any] = {}
    for session_id, rows in threads_by_session.items():
        requested = catalogs.get(session_id, {}).get("primary_thread_id")
        primary_by_session[session_id] = next(
            (row for row in rows if requested is not None and str(row["id"]) == str(requested)),
            next((row for row in rows if int(row["is_primary"] or 0) == 1), rows[0]),
        )

    thread_ids = [str(row["id"]) for row in primary_by_session.values()]
    latest_run_by_thread: dict[str, Any] = {}
    if thread_ids:
        for row in connection.execute(
            select(run_table).where(run_table.c.thread_id.in_(thread_ids)).order_by(run_table.c.started_at.desc(), run_table.c.id.desc())
        ).mappings():
            latest_run_by_thread.setdefault(str(row["thread_id"]), row)
    run_ids = [str(row["id"]) for row in latest_run_by_thread.values()]
    connections_by_run: dict[str, list[Any]] = {}
    if run_ids:
        for row in connection.execute(
            select(connection_table)
            .where(connection_table.c.run_id.in_(run_ids))
            .order_by(connection_table.c.acquired_at.asc(), connection_table.c.id.asc())
        ).mappings():
            connections_by_run.setdefault(str(row["run_id"]), []).append(row)

    console_turn_by_session: dict[str, Any] = {}
    turn_priority = {"queued": 1, "starting": 2, "active": 3, "draining": 4}
    for row in connection.execute(
        select(console_turn_table)
        .where(
            console_turn_table.c.session_id.in_(session_ids),
            console_turn_table.c.state.in_(tuple(turn_priority)),
        )
        .order_by(console_turn_table.c.created_at.asc(), console_turn_table.c.id.asc())
    ).mappings():
        session_id = str(row["session_id"])
        current = console_turn_by_session.get(session_id)
        if current is None or turn_priority[str(row["state"])] > turn_priority[str(current["state"])]:
            console_turn_by_session[session_id] = row

    control_leases_by_session: dict[str, list[Any]] = {}
    live_preview_by_session: dict[str, Any] = {}
    if not compact:
        ranked_control_leases = (
            select(
                control_lease_table,
                func.row_number()
                .over(
                    partition_by=control_lease_table.c.session_id,
                    order_by=(control_lease_table.c.heartbeat_at.desc(), control_lease_table.c.id.desc()),
                )
                .label("lease_rank"),
            )
            .where(control_lease_table.c.session_id.in_(session_ids))
            .subquery()
        )
        for row in connection.execute(
            select(ranked_control_leases)
            .where(ranked_control_leases.c.lease_rank <= 8)
            .order_by(
                ranked_control_leases.c.session_id.asc(),
                ranked_control_leases.c.heartbeat_at.desc(),
                ranked_control_leases.c.id.desc(),
            )
        ).mappings():
            control_leases_by_session.setdefault(str(row["session_id"]), []).append(row)
        live_preview_by_session = {
            str(row["session_id"]): row
            for row in connection.execute(
                select(live_preview_table).where(
                    live_preview_table.c.session_id.in_(session_ids),
                    live_preview_table.c.superseded_at.is_(None),
                )
            ).mappings()
        }

    provider_alias_by_thread: dict[str, Any] = {}
    source_alias_by_thread: dict[str, Any] = {}
    if thread_ids:
        for row in connection.execute(
            select(alias_table)
            .where(alias_table.c.thread_id.in_(thread_ids))
            .order_by(alias_table.c.last_seen_at.desc(), alias_table.c.id.desc())
        ).mappings():
            target = provider_alias_by_thread if row["alias_kind"] == "provider_session_id" else source_alias_by_thread
            if row["alias_kind"] in {"provider_session_id", "source_path"}:
                target.setdefault(str(row["thread_id"]), row)

    ever_managed_threads: set[str] = set()
    if thread_ids:
        ever_managed_threads = {
            str(row[0])
            for row in connection.execute(
                select(run_table.c.thread_id)
                .join(connection_table, connection_table.c.run_id == run_table.c.id)
                .where(run_table.c.thread_id.in_(thread_ids))
                .distinct()
            )
        }

    result: list[dict[str, Any]] = []
    for session_id in session_ids:
        catalog = catalogs.get(session_id)
        card = cards.get(session_id)
        if catalog is None:
            continue
        primary_thread = primary_by_session.get(session_id)
        thread_id = str(primary_thread["id"]) if primary_thread is not None else None
        latest_run = latest_run_by_thread.get(thread_id) if thread_id is not None else None
        run_id = str(latest_run["id"]) if latest_run is not None else None
        result.append(
            {
                "owner_id": owner_by_session.get(session_id),
                "catalog": _row_dto(catalog, fields=_CATALOG_FIELDS, text_limits=_CATALOG_TEXT_LIMITS),
                "card": _row_dto(card, fields=_CARD_FIELDS, text_limits=_CARD_TEXT_LIMITS),
                "runtime": _runtime_dto(runtime_by_session.get(session_id), compact=compact),
                "readiness": _row_dto(
                    readiness_by_session.get(session_id),
                    fields=_READINESS_FIELDS,
                    text_limits={"error_message": 256},
                ),
                "primary_thread": _row_dto(primary_thread, fields=_THREAD_FIELDS),
                "latest_run": _row_dto(latest_run, fields=_RUN_FIELDS, text_limits=_RUN_TEXT_LIMITS),
                "connections": [
                    _row_dto(row, fields=_CONNECTION_FIELDS, text_limits=_CONNECTION_TEXT_LIMITS)
                    for row in _bounded_connections(connections_by_run.get(run_id, []), observed_at=observed_at)
                ],
                "latest_console_turn": _row_dto(
                    console_turn_by_session.get(session_id),
                    fields=frozenset({"id", "session_id", "thread_id", "run_id", "state", "created_at", "updated_at"}),
                ),
                **(
                    {
                        "control_leases": [
                            _row_dto(row, fields=_CONTROL_LEASE_FIELDS, text_limits=_CONTROL_LEASE_TEXT_LIMITS)
                            for row in control_leases_by_session.get(session_id, [])
                        ],
                        "live_preview": _row_dto(
                            live_preview_by_session.get(session_id),
                            fields=_LIVE_PREVIEW_FIELDS,
                            text_limits=_LIVE_PREVIEW_TEXT_LIMITS,
                        ),
                    }
                    if not compact
                    else {}
                ),
                "provider_alias": (
                    _truncate_utf8(str(provider_alias_by_thread[thread_id]["alias_value"]), 512)
                    if not compact and thread_id in provider_alias_by_thread
                    else None
                ),
                "resume": (
                    {
                        "provider_session_id": (
                            _truncate_utf8(str(provider_alias_by_thread[thread_id]["alias_value"]), 512)
                            if thread_id in provider_alias_by_thread
                            else None
                        ),
                        "source_path": (
                            _truncate_utf8(str(source_alias_by_thread[thread_id]["alias_value"]), 4096)
                            if thread_id in source_alias_by_thread
                            else None
                        ),
                        "ever_managed": thread_id in ever_managed_threads,
                    }
                    if thread_id is not None and not compact
                    else None
                ),
            }
        )
    return result


_CATALOG_TEXT_LIMITS = {
    "provider": 64,
    "environment": 32,
    "project": 255,
    "device_id": 255,
    "device_name": 255,
    "cwd": 512,
    "git_repo": 512,
    "git_branch": 255,
    "summary": 768,
    "summary_title": 255,
    "anchor_title": 255,
    "first_user_message_preview": 384,
}
_CARD_TEXT_LIMITS = {
    "summary_title": 255,
    "first_user_message_preview": 384,
    "archive_state": 32,
    "origin_kind": 64,
    "launch_actor": 32,
    "launch_surface": 32,
}

_CATALOG_FIELDS = frozenset(
    {
        "session_id",
        "provider",
        "environment",
        "project",
        "device_id",
        "device_name",
        "cwd",
        "git_repo",
        "git_branch",
        "started_at",
        "ended_at",
        "closed_at",
        "close_reason",
        "last_activity_at",
        "user_messages",
        "assistant_messages",
        "tool_calls",
        "summary",
        "summary_title",
        "anchor_title",
        "first_user_message_preview",
        "transcript_revision",
        "summary_revision",
        "user_state",
        "user_state_at",
        "primary_thread_id",
        "loop_mode",
        "notification_muted",
        "origin_kind",
        "hidden_from_default_timeline",
        "launch_actor",
        "launch_surface",
        "permission_mode",
    }
)
_CARD_FIELDS = frozenset(
    {
        "session_id",
        "last_activity_at",
        "summary_title",
        "first_user_message_preview",
        "user_messages",
        "assistant_messages",
        "tool_calls",
        "transcript_revision",
        "archive_state",
    }
)
_RUNTIME_FIELDS = frozenset(
    {
        "runtime_key",
        "session_id",
        "thread_id",
        "run_id",
        "provider",
        "device_id",
        "phase",
        "phase_source",
        "active_tool",
        "phase_started_at",
        "execution_started_at",
        "last_runtime_signal_at",
        "last_progress_at",
        "last_live_at",
        "timeline_anchor_at",
        "freshness_expires_at",
        "terminal_state",
        "terminal_reason",
        "terminal_source",
        "terminal_at",
        "pending_interaction_id",
        "pending_interaction_kind",
        "pending_interaction_opened_at",
        "pending_interaction_updated_at",
        "pending_interaction_projection_json",
        "pending_interaction_can_respond",
        "runtime_version",
        "updated_at",
    }
)
_READINESS_FIELDS = frozenset(
    {
        "session_id",
        "owner_id",
        "client_request_id",
        "provider",
        "device_id",
        "machine_id",
        "project",
        "execution_lifetime",
        "state",
        "command_id",
        "error_code",
        "error_message",
        "expires_at",
        "created_at",
        "updated_at",
    }
)
_THREAD_FIELDS = frozenset(
    {
        "id",
        "session_id",
        "provider",
        "device_id",
        "cwd",
        "provider_config_json",
        "parent_thread_id",
        "parent_event_id",
        "branch_kind",
        "origin_kind",
        "hidden_from_default_timeline",
        "is_primary",
        "created_at",
        "updated_at",
    }
)
_RUN_FIELDS = frozenset(
    {
        "id",
        "thread_id",
        "provider",
        "host_id",
        "boot_id",
        "pid",
        "process_start_time",
        "cwd",
        "launch_origin",
        "started_at",
        "ended_at",
        "exit_status",
    }
)
_CONNECTION_FIELDS = frozenset(
    {
        "id",
        "run_id",
        "control_plane",
        "acquisition_kind",
        "state",
        "device_id",
        "can_send_input",
        "can_interrupt",
        "can_terminate",
        "can_tail_output",
        "can_resume",
        "acquired_at",
        "released_at",
        "last_health_at",
    }
)
_CONTROL_LEASE_FIELDS = frozenset(
    {
        "id",
        "session_id",
        "provider",
        "device_id",
        "machine_id",
        "state",
        "sequence",
        "heartbeat_at",
        "payload_json",
        "updated_at",
    }
)
_LIVE_PREVIEW_FIELDS = frozenset(
    {
        "session_id",
        "thread_id",
        "turn_key",
        "seq",
        "preview_text",
        "provisional_cursor",
        "provisional_complete",
        "event_origin",
        "preview_observed_at",
        "preview_updated_at",
        "source",
        "last_observation_id",
        "superseded_at",
    }
)
_RUNTIME_TEXT_LIMITS = {
    "runtime_key": 255,
    "provider": 64,
    "device_id": 255,
    "phase": 32,
    "phase_source": 32,
    "active_tool": 128,
    "terminal_state": 32,
    "terminal_reason": 64,
    "terminal_source": 64,
    "pending_interaction_id": 255,
    "pending_interaction_kind": 32,
}
_RUN_TEXT_LIMITS = {
    "provider": 64,
    "host_id": 255,
    "boot_id": 64,
    "cwd": 512,
    "launch_origin": 32,
    "exit_status": 64,
}
_CONNECTION_TEXT_LIMITS = {
    "control_plane": 64,
    "acquisition_kind": 32,
    "state": 32,
    "device_id": 255,
}
_CONTROL_LEASE_TEXT_LIMITS = {
    "provider": 64,
    "device_id": 255,
    "machine_id": 255,
    "state": 32,
    "payload_json": 2048,
}
_LIVE_PREVIEW_TEXT_LIMITS = {
    "thread_id": 255,
    "turn_key": 512,
    "preview_text": 8_192,
    "provisional_cursor": 512,
    "event_origin": 32,
    "source": 128,
    "last_observation_id": 512,
}


def _row_dto(
    row,
    *,
    fields: frozenset[str] | None = None,
    text_limits: dict[str, int] | None = None,
) -> dict[str, Any] | None:
    if row is None:
        return None
    limits = text_limits or {}
    result: dict[str, Any] = {}
    for key, value in row.items():
        if fields is not None and key not in fields:
            continue
        if isinstance(value, datetime):
            result[key] = _encode_datetime(value)
        elif isinstance(value, UUID):
            result[key] = str(value)
        elif isinstance(value, str):
            result[key] = _truncate_utf8(value, limits.get(key, 255))
        else:
            result[key] = value
    return result


def _runtime_dto(row, *, compact: bool) -> dict[str, Any] | None:
    result = _row_dto(row, fields=_RUNTIME_FIELDS, text_limits=_RUNTIME_TEXT_LIMITS)
    if result is not None:
        result["pending_interaction_projection_json"] = _bounded_pause_projection(
            result.get("pending_interaction_projection_json"),
            compact=compact,
        )
    return result


def _bounded_pause_projection(value: object, *, compact: bool) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    string_limits = {
        "id": 128,
        "request_key": 255,
        "session_id": 64,
        "runtime_key": 255,
        "kind": 64,
        "status": 32,
        "provider": 64,
        "title": 96 if compact else 160,
        "summary": 128 if compact else 256,
        "tool_name": 128,
        "occurred_at": 64,
        "last_seen_at": 64,
        "resolved_at": 64,
        "expires_at": 64,
    }
    for key, maximum in string_limits.items():
        raw = value.get(key)
        result[key] = _truncate_utf8(str(raw), maximum) if raw is not None else None
    result["can_respond"] = bool(value.get("can_respond"))
    questions: list[dict[str, Any]] = []
    for raw_question in value.get("questions", [])[:3] if isinstance(value.get("questions"), list) else []:
        if not isinstance(raw_question, dict):
            continue
        options: list[dict[str, str | None]] = []
        for raw_option in raw_question.get("options", [])[:4] if isinstance(raw_question.get("options"), list) else []:
            if not isinstance(raw_option, dict):
                continue
            options.append(
                {
                    "label": _truncate_utf8(str(raw_option.get("label") or ""), 32 if compact else 48),
                    "description": (
                        _truncate_utf8(
                            str(raw_option["description"]),
                            32 if compact else 64,
                        )
                        if raw_option.get("description")
                        else None
                    ),
                    "value": (
                        _truncate_utf8(str(raw_option["value"]), 32 if compact else 48) if raw_option.get("value") is not None else None
                    ),
                }
            )
        questions.append(
            {
                "id": _truncate_utf8(str(raw_question.get("id") or ""), 128),
                "header": (_truncate_utf8(str(raw_question["header"]), 48 if compact else 64) if raw_question.get("header") else None),
                "question": _truncate_utf8(
                    str(raw_question.get("question") or "Answer required"),
                    128 if compact else 192,
                ),
                "multi_select": bool(raw_question.get("multi_select")),
                "options": options,
            }
        )
    result["questions"] = questions
    return result


def _bounded_connections(rows: list[Any], *, observed_at: datetime) -> list[Any]:
    state_priority = {"attached": 5, "degraded": 4, "detached": 3, "released": 2, "ended": 1}

    def key(row) -> tuple[Any, ...]:
        state = str(row["state"] or "")
        last_health = _as_aware_utc(row["last_health_at"])
        if state in {"attached", "degraded"} and (last_health is None or observed_at - last_health > _CONTROL_LEASE_TTL):
            state = "detached"
        capabilities = sum(bool(row[field]) for field in ("can_send_input", "can_interrupt", "can_terminate", "can_tail_output"))
        return (
            state_priority.get(state, 0),
            capabilities,
            last_health or datetime.min.replace(tzinfo=UTC),
            int(row["id"] or 0),
        )

    return sorted(rows, key=key, reverse=True)[:SESSION_CONNECTION_LIMIT]


def _truncate_utf8(value: str, maximum_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return value
    return encoded[:maximum_bytes].decode("utf-8", errors="ignore")


def _heartbeat_request_sha256(
    *,
    heartbeat: dict[str, Any],
    managed_leases: list[dict[str, Any]],
    managed_leases_present: bool,
    owner_id: int | None,
) -> str:
    payload = {
        "heartbeat": _jsonable_catalog_value(heartbeat),
        "managed_leases": _jsonable_catalog_value(managed_leases),
        "managed_leases_present": managed_leases_present,
        "owner_id": owner_id,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _jsonable_catalog_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _encode_datetime(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable_catalog_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable_catalog_value(item) for item in value]
    return value


def _decode_json_object(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        decoded = json.loads(str(value or "{}"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("catalog receipt JSON is invalid") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("catalog receipt JSON is not an object")
    return decoded


def _source_epoch_dto(row) -> dict[str, Any]:
    return {
        "source_epoch": str(row["source_epoch"]),
        "tenant_id": str(row["tenant_id"]),
        "machine_id": str(row["machine_id"]),
        "provider": str(row["provider"]),
        "opaque_source_id": str(row["opaque_source_id"]),
        "range_kind": str(row["range_kind"]),
        "state": str(row["state"]),
        "predecessor_source_epoch": row["predecessor_source_epoch"],
        "replaced_by_source_epoch": row["replaced_by_source_epoch"],
        "accepted_through": str(int(row["accepted_through"])),
        "object_count": int(row["object_count"]),
        "commit_seq": str(row["commit_seq"]),
        "closed_commit_seq": str(row["closed_commit_seq"]) if row["closed_commit_seq"] is not None else None,
        "opened_at": _encode_datetime(row["opened_at"]),
        "closed_at": _encode_datetime(row["closed_at"]),
        "close_reason": row["close_reason"],
    }


def _raw_object_receipt(row) -> dict[str, object]:
    missing = tuple(json.loads(str(row["missing_media_hashes_json"] or "[]")))
    return DurableReceipt(
        envelope_id=str(row["envelope_id"]),
        object_hash=str(row["object_hash"]),
        commit_seq=int(row["commit_seq"]),
        render_state=str(row["render_state"]),
        media_state=str(row["media_state"]),
        missing_media_hashes=missing,
    ).as_wire()


def _contiguous_range_prefix(rows) -> int:
    """Return the proven contiguous end, ignoring any legacy high-water mark."""
    if not rows:
        return 0
    through = int(rows[0][0])
    for row in rows:
        start = int(row[0])
        end = int(row[1])
        if start != through or end < start:
            break
        through = end
    return through


def _raw_object_manifest_dto(row) -> dict[str, Any]:
    return {
        "envelope_id": str(row["envelope_id"]),
        "tenant_id": str(row["tenant_id"]),
        "session_id": str(row["session_id"]),
        "machine_id": str(row["machine_id"]),
        "provider": str(row["provider"]),
        "opaque_source_id": str(row["opaque_source_id"]),
        "source_epoch": str(row["source_epoch"]),
        "range_kind": str(row["range_kind"]),
        "range_start": str(int(row["range_start"])),
        "range_end": str(int(row["range_end"])),
        "record_count": int(row["record_count"]),
        "object_hash": str(row["object_hash"]),
        "payload_hash": str(row["payload_hash"]),
        "object_path": str(row["object_path"]),
        "uncompressed_size": int(row["uncompressed_size"]),
        "compressed_size": int(row["compressed_size"]),
        "provenance_kind": str(row["provenance_kind"]),
        "commit_seq": str(row["commit_seq"]),
        "render_state": str(row["render_state"]),
        "media_state": str(row["media_state"]),
        "retired_at": _encode_datetime(row["retired_at"]),
        "retirement_revision": (str(row["retirement_revision"]) if row["retirement_revision"] is not None else None),
    }


def _recompute_render_generation_projection(
    connection,
    *,
    session_id: str,
    generation_id: str,
    commit_seq: int,
    commit_time: datetime,
) -> None:
    """Rebuild bounded heads after the rare source-epoch replacement path."""

    generation = RenderGeneration.__table__
    objects = RenderObject.__table__
    sessions = StorageSession.__table__
    rows = list(
        connection.execute(
            select(objects).where(
                objects.c.session_id == session_id,
                objects.c.generation_id == generation_id,
                objects.c.retired_at.is_(None),
            )
        )
        .mappings()
        .all()
    )
    first_order = None
    last_order = None
    first_preview_row = None
    last_preview_row = None
    for row in rows:
        first_order = _minimum_order_key(first_order, row["first_order_key"])
        last_order = _maximum_order_key(last_order, row["last_order_key"])
        if (
            row["first_user_message_preview"] is not None
            and row["first_order_key"] is not None
            and (
                first_preview_row is None
                or tuple(json.loads(str(row["first_order_key"]))) < tuple(json.loads(str(first_preview_row["first_order_key"])))
            )
        ):
            first_preview_row = row
        if (
            row["last_visible_text_preview"] is not None
            and row["last_order_key"] is not None
            and (
                last_preview_row is None
                or tuple(json.loads(str(row["last_order_key"]))) > tuple(json.loads(str(last_preview_row["last_order_key"])))
            )
        ):
            last_preview_row = row
    source_ids = sorted(str(row["source_envelope_id"]) for row in rows)
    source_chain_hash = hashlib.sha256(
        b"longhouse-render-source-set-v1\0" + b"".join(bytes.fromhex(value) for value in source_ids)
    ).hexdigest()
    generation_values = {
        "state": "current",
        "source_chain_hash": source_chain_hash,
        "object_count": len(rows),
        "event_count": sum(int(row["event_count"]) for row in rows),
        "first_order_key": first_order,
        "last_order_key": last_order,
        "commit_seq": commit_seq,
        "updated_at": commit_time,
    }
    connection.execute(update(generation).where(generation.c.generation_id == generation_id).values(**generation_values))
    connection.execute(
        update(sessions)
        .where(sessions.c.session_id == session_id)
        .values(
            current_render_generation=generation_id,
            user_messages=sum(int(row["user_messages"]) for row in rows),
            assistant_messages=sum(int(row["assistant_messages"]) for row in rows),
            tool_calls=sum(int(row["tool_calls"]) for row in rows),
            summary_title=func.coalesce(
                sessions.c.summary_title,
                sanitize_title(
                    str(first_preview_row["first_user_message_preview"]) if first_preview_row is not None else None,
                    max_words=6,
                ),
            ),
            first_user_message_preview=(str(first_preview_row["first_user_message_preview"]) if first_preview_row is not None else None),
            last_visible_text_preview=(str(last_preview_row["last_visible_text_preview"]) if last_preview_row is not None else None),
        )
    )


def _minimum_order_key(left: object, right: object) -> str | None:
    values = [value for value in (left, right) if value is not None]
    return min(values, key=lambda value: tuple(json.loads(str(value)))) if values else None


def _maximum_order_key(left: object, right: object) -> str | None:
    values = [value for value in (left, right) if value is not None]
    return max(values, key=lambda value: tuple(json.loads(str(value)))) if values else None


def _render_order_columns(first: object, last: object) -> dict[str, object | None]:
    values: dict[str, object | None] = {}
    fields = (
        "order_time_us",
        "machine_id",
        "provider",
        "opaque_source_id",
        "source_epoch",
        "source_position",
        "event_subordinal",
    )
    for prefix, raw in (("first", first), ("last", last)):
        decoded = json.loads(str(raw)) if raw is not None else [None] * len(fields)
        values.update({f"{prefix}_{field}": value for field, value in zip(fields, decoded, strict=True)})
    return values


def _storage_session_dto(row) -> dict[str, Any]:
    return {
        "session_id": str(row["session_id"]),
        "tenant_id": str(row["tenant_id"]),
        "owner_id": row["owner_id"],
        "provider": str(row["provider"]),
        "environment": str(row["environment"]),
        "machine_id": str(row["machine_id"]),
        "project": row["project"],
        "cwd": row["cwd"],
        "git_repo": row["git_repo"],
        "git_branch": row["git_branch"],
        "started_at": _encode_datetime(row["started_at"]),
        "last_activity_at": _encode_datetime(row["last_activity_at"]),
        "ended_at": _encode_datetime(row["ended_at"]),
        "user_messages": int(row["user_messages"]),
        "assistant_messages": int(row["assistant_messages"]),
        "tool_calls": int(row["tool_calls"]),
        "summary_title": row["summary_title"],
        "first_user_message_preview": row["first_user_message_preview"],
        "last_visible_text_preview": row["last_visible_text_preview"],
        "transcript_revision": str(row["transcript_revision"]),
        "current_render_generation": row["current_render_generation"],
        "raw_state": str(row["raw_state"]),
        "render_state": str(row["render_state"]),
        "media_state": str(row["media_state"]),
        "missing_media_hashes": list(json.loads(str(row["missing_media_hashes_json"] or "[]"))),
        "user_state": str(row["user_state"]),
        "loop_mode": str(row["loop_mode"]),
        "notification_muted": bool(row["notification_muted"]),
        "origin_kind": row["origin_kind"],
        "hidden_from_default_timeline": bool(row["hidden_from_default_timeline"]),
        "launch_actor": row["launch_actor"],
        "launch_surface": row["launch_surface"],
        "commit_seq": str(row["commit_seq"]),
        "created_at": _encode_datetime(row["created_at"]),
        "updated_at": _encode_datetime(row["updated_at"]),
    }


def _delete_bounded_live_session_state(connection, *, session_key: str, deleted_at: datetime) -> int:
    threads = LiveSessionThread.__table__
    aliases = LiveSessionThreadAlias.__table__
    runs = LiveSessionRun.__table__
    connections = LiveSessionConnection.__table__
    thread_ids = list(connection.execute(select(threads.c.id).where(threads.c.session_id == session_key)).scalars())
    run_ids = list(connection.execute(select(runs.c.id).where(runs.c.thread_id.in_(thread_ids))).scalars()) if thread_ids else []
    removed = 0
    if run_ids:
        removed += int(connection.execute(delete(connections).where(connections.c.run_id.in_(run_ids))).rowcount or 0)
        removed += int(connection.execute(delete(runs).where(runs.c.id.in_(run_ids))).rowcount or 0)
    if thread_ids:
        removed += int(connection.execute(delete(aliases).where(aliases.c.thread_id.in_(thread_ids))).rowcount or 0)
        removed += int(connection.execute(delete(threads).where(threads.c.id.in_(thread_ids))).rowcount or 0)
    for table in (
        LiveSessionCatalog.__table__,
        LiveTimelineCard.__table__,
        LiveSessionLaunchAttempt.__table__,
        LiveSession.__table__,
        LiveRuntimeState.__table__,
        LiveInteractionRequest.__table__,
        LiveControlLease.__table__,
        LiveLaunchReadiness.__table__,
        LiveSessionLivePreview.__table__,
        LiveMachineControlOperation.__table__,
        LiveSessionInputReceipt.__table__,
    ):
        removed += int(connection.execute(delete(table).where(table.c.session_id == session_key)).rowcount or 0)
    connection.execute(
        update(LiveNotificationClientPresence.__table__)
        .where(LiveNotificationClientPresence.session_id == session_key)
        .values(session_id=None, updated_at=deleted_at)
    )
    connection.execute(
        update(LiveAPNSLiveActivityRegistration.__table__)
        .where(
            LiveAPNSLiveActivityRegistration.session_id == session_key,
            LiveAPNSLiveActivityRegistration.ended_at.is_(None),
        )
        .values(ended_at=deleted_at, updated_at=deleted_at)
    )
    return removed


def _render_generation_dto(row) -> dict[str, Any]:
    return {
        "generation_id": str(row["generation_id"]),
        "session_id": str(row["session_id"]),
        "parser_revision": str(row["parser_revision"]),
        "ordering_revision": str(row["ordering_revision"]),
        "state": str(row["state"]),
        "source_chain_hash": str(row["source_chain_hash"]),
        "object_count": int(row["object_count"]),
        "event_count": int(row["event_count"]),
        "first_order_key": row["first_order_key"],
        "last_order_key": row["last_order_key"],
        "commit_seq": str(row["commit_seq"]),
        "created_at": _encode_datetime(row["created_at"]),
        "updated_at": _encode_datetime(row["updated_at"]),
        "superseded_at": _encode_datetime(row["superseded_at"]),
    }


def _render_object_manifest_dto(row) -> dict[str, Any]:
    return {
        "object_id": str(row["object_id"]),
        "generation_id": str(row["generation_id"]),
        "session_id": str(row["session_id"]),
        "source_envelope_id": str(row["source_envelope_id"]),
        "object_hash": str(row["object_hash"]),
        "payload_hash": str(row["payload_hash"]),
        "object_path": str(row["object_path"]),
        "uncompressed_size": int(row["uncompressed_size"]),
        "compressed_size": int(row["compressed_size"]),
        "event_count": int(row["event_count"]),
        "user_messages": int(row["user_messages"]),
        "assistant_messages": int(row["assistant_messages"]),
        "tool_calls": int(row["tool_calls"]),
        "first_user_message_preview": row["first_user_message_preview"],
        "last_visible_text_preview": row["last_visible_text_preview"],
        "first_order_key": row["first_order_key"],
        "last_order_key": row["last_order_key"],
        "commit_seq": str(row["commit_seq"]),
        "created_at": _encode_datetime(row["created_at"]),
        "retired_at": _encode_datetime(row["retired_at"]),
        "retirement_revision": str(row["retirement_revision"]) if row["retirement_revision"] is not None else None,
    }


def _media_object_dto(row) -> dict[str, Any]:
    return {
        "media_hash": str(row["media_hash"]),
        "state": str(row["state"]),
        "mime_type": row["mime_type"],
        "byte_size": int(row["byte_size"]) if row["byte_size"] is not None else None,
        "object_path": row["object_path"],
        "commit_seq": str(row["commit_seq"]),
        "observed_at": _encode_datetime(row["observed_at"]),
        "verified_at": _encode_datetime(row["verified_at"]),
        "deleted_at": _encode_datetime(row["deleted_at"]),
    }


def _media_ref_dto(row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "session_id": str(row["session_id"]),
        "media_hash": str(row["media_hash"]),
        "envelope_id": row["envelope_id"],
        "ref_key": str(row["ref_key"]),
        "state": str(row["state"]),
        "commit_seq": str(row["commit_seq"]),
        "created_at": _encode_datetime(row["created_at"]),
        "retired_at": _encode_datetime(row["retired_at"]),
        "deletion_revision": str(row["deletion_revision"]) if row["deletion_revision"] is not None else None,
    }


def _projector_state_dto(row) -> dict[str, Any]:
    return {
        "projector": str(row["projector"]),
        "session_id": str(row["session_id"]),
        "desired_revision": str(row["desired_revision"]),
        "completed_revision": str(row["completed_revision"]),
        "claimed_revision": str(row["claimed_revision"]) if row["claimed_revision"] is not None else None,
        "claim_token": row["claim_token"],
        "worker_id": row["worker_id"],
        "claim_expires_at": _encode_datetime(row["claim_expires_at"]),
        "status": str(row["status"]),
        "failure_count": int(row["failure_count"]),
        "last_error_code": row["last_error_code"],
        "last_error_message": row["last_error_message"],
        "retry_at": _encode_datetime(row["retry_at"]),
        "commit_seq": str(row["commit_seq"]),
        "updated_at": _encode_datetime(row["updated_at"]),
    }


def _raw_object_matches(row, immutable: dict[str, Any]) -> bool:
    for key, value in immutable.items():
        existing = row[key]
        if isinstance(value, datetime):
            if _as_aware_utc(existing) != value:
                return False
        elif existing != value:
            return False
    return True


def _u64_key(value: int) -> str:
    if not 0 <= value < 1 << 64:
        raise ValueError("value exceeds u64")
    return f"{value:020d}"


def _recency_weight(age_days: float) -> int:
    for threshold, weight in _RECENCY_BUCKETS:
        if age_days <= threshold:
            return weight
    return 10


def _workspace_label(path: str, git_repo: str | None, git_branch: str | None) -> str:
    if git_repo:
        name = str(git_repo).rstrip("/").rsplit("/", 1)[-1]
        if name.endswith(".git"):
            name = name[:-4]
        if name:
            return f"{name} ({git_branch})" if git_branch else name
    parts = path.split("/")
    if len(parts) >= 3 and parts[1] == "Users":
        return "~/" + "/".join(parts[3:]) if len(parts) > 3 else "~"
    return path


def _current_commit_seq(connection) -> int:
    value = connection.execute(select(catalog_meta.c.commit_seq).where(catalog_meta.c.singleton == 1)).scalar_one()
    if type(value) is not int or value < 0:
        raise RuntimeError("catalog commit_seq is invalid")
    return value


def _advance_commit_seq(connection, now: datetime) -> int:
    return connection.execute(
        update(catalog_meta)
        .where(catalog_meta.c.singleton == 1)
        .values(commit_seq=catalog_meta.c.commit_seq + 1, updated_at=now.isoformat())
        .returning(catalog_meta.c.commit_seq)
    ).scalar_one()


def _user_dto(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "email": row["email"],
        "provider": row["provider"],
        "provider_user_id": row["provider_user_id"],
        "cp_user_id": row["cp_user_id"],
        "email_verified": bool(row["email_verified"]),
        "is_active": bool(row["is_active"]),
        "role": str(row["role"]),
        "display_name": row["display_name"],
        "avatar_url": row["avatar_url"],
        "prefs": row["prefs"],
        "context": row["context"] or {},
        "last_login": _encode_datetime(row["last_login"]),
        "created_at": _encode_datetime(row["created_at"]),
        "updated_at": _encode_datetime(row["updated_at"]),
    }


@contextmanager
def _read_snapshot(engine: Engine):
    """Open a real SQLite read transaction under pysqlite legacy mode."""

    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN")
        try:
            yield connection
        finally:
            connection.rollback()


@contextmanager
def _write_transaction(engine: Engine):
    """Acquire SQLite's write reservation before mutation read-checks."""

    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


def _as_aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _encode_datetime(value: datetime | None) -> str | None:
    normalized = _as_aware_utc(value)
    return normalized.isoformat() if normalized is not None else None


__all__ = ["CatalogStore", "DEVICE_TOKEN_LIMIT_PER_OWNER"]
