"""CatalogStore: provider interactions (questions, approvals) and their decisions."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_
from sqlalchemy import or_
from sqlalchemy.orm import Session

from zerg.catalogd.fact_reducer import _advance_commit_seq
from zerg.catalogd.fact_reducer import _current_commit_seq

# store.py imports this module after defining these, at its end.
from zerg.catalogd.store import _as_aware_utc
from zerg.catalogd.store import _encode_datetime
from zerg.catalogd.store import _interaction_dto
from zerg.catalogd.store import _read_snapshot
from zerg.catalogd.store import _runtime_interaction_dto
from zerg.catalogd.store import _write_transaction
from zerg.models.live_store import LiveInteractionRequest
from zerg.models.live_store import LiveRuntimeState
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread


class InteractionsMixin:
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
                row = orm.query(LiveInteractionRequest).filter_by(request_key=interaction["request_key"]).one()
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
                    if status == "pending":
                        now = datetime.now(UTC)
                        query = query.filter((LiveInteractionRequest.expires_at.is_(None)) | (LiveInteractionRequest.expires_at > now))
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

    def expire_due_interactions(self, *, now: datetime, session_id: str | None = None, dry_run: bool = False) -> dict[str, Any]:
        """Repair elapsed or provably orphaned waits through catalog maintenance.

        No age-based guesses, archive writes, or startup data migration. Dry runs
        report the same transactionally evaluated evidence that apply consumes.
        """
        from zerg.services.session_pause_requests import EXECUTION_TERMINAL_STATES
        from zerg.services.session_pause_requests import clear_live_interaction_pointer
        from zerg.services.session_pause_requests import expire_live_interaction
        from zerg.services.session_pause_requests import live_interaction_terminal_reason
        from zerg.services.session_pause_requests import materialize_live_interaction
        from zerg.services.session_runtime import RuntimeEventIngest
        from zerg.services.session_runtime import _apply_run_terminal_event
        from zerg.services.session_runtime import _live_run_for_terminal

        repairs = []
        run_repairs = []
        with _write_transaction(self.engine) as connection:
            orm = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            legacy_interactions = []
            try:
                unended_run = (
                    orm.query(LiveSessionRun.id)
                    .join(LiveSessionThread, LiveSessionThread.id == LiveSessionRun.thread_id)
                    .filter(
                        LiveSessionThread.session_id == LiveRuntimeState.session_id,
                        LiveSessionRun.ended_at.is_(None),
                        LiveSessionRun.started_at <= LiveRuntimeState.terminal_at,
                    )
                    .correlate(LiveRuntimeState)
                    .exists()
                )
                runtime_query = orm.query(LiveRuntimeState).filter(
                    or_(
                        LiveRuntimeState.pending_interaction_id.is_not(None),
                        and_(LiveRuntimeState.terminal_state.in_(EXECUTION_TERMINAL_STATES - {"user_closed"}), unended_run),
                    )
                )
                if session_id is not None:
                    runtime_query = runtime_query.filter(LiveRuntimeState.session_id == UUID(session_id))
                for runtime in runtime_query.all():
                    # Legacy writers persisted only the pointer. Materialize it
                    # before terminalizing so the original question is retained.
                    legacy = materialize_live_interaction(orm, runtime, persist=False)
                    if legacy is not None and legacy not in orm:
                        legacy_interactions.append(legacy)
                    terminal_at = _as_aware_utc(runtime.terminal_at)
                    if (
                        runtime.terminal_state not in EXECUTION_TERMINAL_STATES - {"user_closed"}
                        or terminal_at is None
                        or runtime.session_id is None
                    ):
                        continue
                    event = RuntimeEventIngest(
                        runtime_key=runtime.runtime_key,
                        session_id=runtime.session_id,
                        run_id=runtime.run_id,
                        provider=runtime.provider,
                        source="interaction_maintenance",
                        kind="terminal_signal",
                        occurred_at=terminal_at,
                        dedupe_key=f"repair-terminal:{runtime.runtime_key}",
                        payload={"terminal_state": runtime.terminal_state, "terminal_reason": runtime.terminal_reason},
                    )
                    run = _live_run_for_terminal(orm, event=event, state=runtime, occurred_at=terminal_at)
                    if run is None:
                        continue
                    if _apply_run_terminal_event(orm, event=event, state=runtime, occurred_at=terminal_at):
                        run_repairs.append({"session_id": str(runtime.session_id), "run_id": run.id, "ended_at": terminal_at.isoformat()})
                pending_query = orm.query(LiveInteractionRequest).filter(LiveInteractionRequest.status == "pending")
                if session_id is not None:
                    pending_query = pending_query.filter(LiveInteractionRequest.session_id == session_id)
                for row in [*pending_query.all(), *legacy_interactions]:
                    reason = live_interaction_terminal_reason(orm, row)
                    if (
                        reason is None
                        and row.kind == "permission_prompt"
                        and row.expires_at is not None
                        and _as_aware_utc(row.expires_at) <= now
                    ):
                        reason = "Approval deadline expired"
                    if reason is None:
                        continue
                    repairs.append({"session_id": row.session_id, "interaction_id": row.id, "reason": reason})
                    expire_live_interaction(orm, row, occurred_at=now, reason=reason)
                # Repair pointers left behind by older terminalization paths.
                for runtime in runtime_query.filter(LiveRuntimeState.pending_interaction_id.is_not(None)).all():
                    row = orm.query(LiveInteractionRequest).filter_by(request_key=runtime.pending_interaction_id).one_or_none()
                    if row is not None and row.status != "pending":
                        if clear_live_interaction_pointer(runtime, request_key=row.request_key, occurred_at=now):
                            repairs.append(
                                {"session_id": row.session_id, "interaction_id": row.id, "reason": "Terminal interaction pointer"}
                            )
                if dry_run:
                    orm.rollback()
                else:
                    orm.commit()
            except BaseException:
                orm.rollback()
                raise
            finally:
                orm.close()
            commit_seq = (
                _advance_commit_seq(connection, now) if not dry_run and (repairs or run_repairs) else _current_commit_seq(connection)
            )
            return {
                "expired_count": 0 if dry_run else len(repairs),
                "candidate_count": len(repairs),
                "run_repair_count": 0 if dry_run else len(run_repairs),
                "dry_run": dry_run,
                "interactions": repairs,
                "runs": run_repairs,
                "commit_seq": str(commit_seq),
            }

    def repair_expire_interaction(
        self,
        *,
        session_id: str,
        interaction_id: str,
        expected_updated_at: datetime,
        expected_source: str,
        expected_reply_transport: str,
        now: datetime,
        dry_run: bool,
    ) -> dict[str, Any]:
        """CAS-protected operator repair for one known malformed interaction."""

        reason = "legacy_provider_gate_contract_invalid"
        with _write_transaction(self.engine) as connection:
            orm = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            try:
                row = (
                    orm.query(LiveInteractionRequest)
                    .filter(
                        LiveInteractionRequest.id == interaction_id,
                        LiveInteractionRequest.session_id == session_id,
                        LiveInteractionRequest.status == "pending",
                        LiveInteractionRequest.updated_at == expected_updated_at,
                        LiveInteractionRequest.source == expected_source,
                        LiveInteractionRequest.reply_transport == expected_reply_transport,
                    )
                    .one_or_none()
                )
                if row is None:
                    orm.rollback()
                    return {"repaired": False, "reason": "compare_and_set_failed", "commit_seq": str(_current_commit_seq(connection))}
                if dry_run:
                    receipt = _interaction_dto(row)
                    orm.rollback()
                    return {
                        "repaired": False,
                        "dry_run": True,
                        "reason": "would_expire_legacy_provider_gate_contract_invalid",
                        "interaction": receipt,
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                from zerg.services.session_pause_requests import expire_live_interaction

                expire_live_interaction(orm, row, occurred_at=now, reason=reason)
                repaired = _interaction_dto(row)
                orm.commit()
            except BaseException:
                orm.rollback()
                raise
            finally:
                orm.close()
            commit_seq = _advance_commit_seq(connection, now)
            return {"repaired": True, "reason": reason, "interaction": repaired, "commit_seq": str(commit_seq)}

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

        from zerg.services.session_pause_requests import expire_live_interaction
        from zerg.services.session_pause_requests import live_interaction_terminal_reason
        from zerg.services.session_pause_requests import materialize_live_interaction
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
                        row = materialize_live_interaction(orm, runtime)
                if row is None:
                    orm.rollback()
                    return {
                        "found": False,
                        "resolved": False,
                        "reason": "not_found",
                        "interaction": None,
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                terminal_reason = live_interaction_terminal_reason(orm, row) if row.status == "pending" else None
                if terminal_reason is not None:
                    expire_live_interaction(orm, row, occurred_at=resolved_at, reason=terminal_reason)
                    result = _interaction_dto(row)
                    orm.commit()
                    return {
                        "found": True,
                        "resolved": False,
                        "reason": "not_pending",
                        "interaction": result,
                        "commit_seq": str(_advance_commit_seq(connection, resolved_at)),
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
                expired_before_response = (
                    row.expires_at is not None and _as_aware_utc(row.expires_at) <= resolved_at and status != "expired"
                )
                event_status = "expired" if expired_before_response else status
                event_response_payload = (
                    {
                        "permissionDecision": "deny",
                        "permissionDecisionReason": "Approval deadline expired",
                    }
                    if expired_before_response
                    else response_payload
                )
                event_response_text = "Approval deadline expired" if expired_before_response else response_text
                event = RuntimeEventIngest(
                    runtime_key=row.runtime_key,
                    session_id=UUID(session_id),
                    provider=row.provider,
                    source="interaction_response",
                    kind="pause_resolution",
                    occurred_at=resolved_at,
                    dedupe_key=f"interaction-resolution:{interaction_id}:{event_status}",
                    payload={
                        "request_key": row.request_key,
                        "provider_request_id": row.provider_request_id,
                        "status": event_status,
                        "response_payload": event_response_payload,
                        "response_text": event_response_text,
                    },
                )
                ingest_live_runtime_events(orm, [event])
                resolved = row
                orm.commit()
            except BaseException:
                orm.rollback()
                raise
            finally:
                orm.close()
            commit_seq = _advance_commit_seq(connection, resolved_at)
            return {
                "found": True,
                "resolved": not expired_before_response,
                "reason": "not_pending" if expired_before_response else None,
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
                    LiveInteractionRequest.source.in_(("claude_permission_gate", "cursor_permission_gate")),
                )
                query = (
                    query.filter(LiveInteractionRequest.id == interaction_id)
                    if interaction_id is not None
                    else query.filter(LiveInteractionRequest.request_key == request_key)
                )
                row = query.one_or_none()
                expired = (
                    row is not None
                    and row.status == "pending"
                    and row.expires_at is not None
                    and _as_aware_utc(row.expires_at) <= datetime.now(UTC)
                )
                if expired:
                    result = {
                        "found": True,
                        "resolved": True,
                        "decision": "deny",
                        "reason": "Approval deadline expired",
                    }
                elif row is None or row.status == "pending":
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
