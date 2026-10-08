"""CatalogStore: console sessions and turns, local launches and resumes, and machine control operations."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.orm import Session

from zerg.catalogd.fact_reducer import _advance_commit_seq
from zerg.catalogd.fact_reducer import _current_commit_seq
from zerg.catalogd.fact_reducer import read_session_fact_heads
from zerg.catalogd.models import StorageSession

# store.py imports this module after defining these, at its end.
from zerg.catalogd.store import CONSOLE_TURN_TERMINAL_STATES
from zerg.catalogd.store import _as_aware_utc
from zerg.catalogd.store import _assemble_session_facts
from zerg.catalogd.store import _console_turn_attachments
from zerg.catalogd.store import _console_turns_with_reporting_run
from zerg.catalogd.store import _create_console_turn_rows
from zerg.catalogd.store import _decode_json_object
from zerg.catalogd.store import _json_launch_result
from zerg.catalogd.store import _live_console_turn_dto
from zerg.catalogd.store import _live_control_grant_payload
from zerg.catalogd.store import _live_thread_source_path
from zerg.catalogd.store import _open_run_holds_live_ownership
from zerg.catalogd.store import _receipt_error_code
from zerg.catalogd.store import _retire_orphaned_open_run
from zerg.catalogd.store import _settle_console_turn
from zerg.catalogd.store import _starting_console_turn_dto
from zerg.catalogd.store import _stored_live_control_grant_matches
from zerg.catalogd.store import _write_transaction
from zerg.models.live_store import LiveConsoleTurn
from zerg.models.live_store import LiveMachineControlOperation
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionInputReceipt
from zerg.models.live_store import LiveSessionLaunchAttempt
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.models.live_store import LiveSessionThreadAlias
from zerg.models.live_store import LiveTimelineCard
from zerg.services.codex_launch_visibility_repair import CodexLaunchVisibilityRepairFacts
from zerg.services.codex_launch_visibility_repair import codex_launch_visibility_repair_fingerprint
from zerg.services.codex_launch_visibility_repair import codex_launch_visibility_repair_refusals
from zerg.services.codex_launch_visibility_repair import plan_codex_launch_visibility_repair
from zerg.services.session_visibility_policy import SessionVisibilityFacts
from zerg.services.session_visibility_policy import evaluate_origin_visibility


class LaunchMixin:
    def repair_codex_launch_visibility(
        self,
        *,
        session_id: str,
        dry_run: bool,
        expected_fingerprint: str | None,
    ) -> dict[str, Any]:
        """Dry-run or CAS-apply one exact legacy Codex launch repair."""

        from zerg.services.session_state_facts_projector import project_shadow_session_state_facts

        observed_at = datetime.now(UTC)
        catalog_table = LiveSessionCatalog.__table__
        card_table = LiveTimelineCard.__table__
        thread_table = LiveSessionThread.__table__
        with _write_transaction(self.engine) as connection:
            catalog_row = connection.execute(select(catalog_table).where(catalog_table.c.session_id == session_id)).mappings().first()
            card_row = connection.execute(select(card_table).where(card_table.c.session_id == session_id)).mappings().first()
            if catalog_row is None or card_row is None:
                return {
                    "eligible": False,
                    "applied": False,
                    "dry_run": dry_run,
                    "refusals": ["session_not_found" if catalog_row is None else "timeline_card_missing"],
                    "commit_seq": str(_current_commit_seq(connection)),
                }

            snapshots = _assemble_session_facts(
                connection,
                session_ids=[session_id],
                observed_at=observed_at,
                compact=True,
            )
            if not snapshots:
                return {
                    "eligible": False,
                    "applied": False,
                    "dry_run": dry_run,
                    "refusals": ["canonical_snapshot_missing"],
                    "commit_seq": str(_current_commit_seq(connection)),
                }
            snapshot = snapshots[0]
            primary_thread = snapshot.get("primary_thread") or {}
            latest_run = snapshot.get("latest_run") or {}
            connections = snapshot.get("connections") or []
            current_run_id = str(latest_run.get("id") or "")
            owned_connection = any(
                str(item.get("run_id") or "") == current_run_id
                and item.get("released_at") is None
                and item.get("state") in {"attached", "degraded"}
                and item.get("acquisition_kind") in {"spawned_control", "adopted_control"}
                for item in connections
                if isinstance(item, dict)
            )
            managed_local = (
                bool(current_run_id)
                and latest_run.get("ended_at") is None
                and latest_run.get("launch_origin") in {"longhouse_spawned", "longhouse_continued"}
                and owned_connection
            )
            fact_commit_seq, heads = read_session_fact_heads(connection, session_id=session_id)
            projected = project_shadow_session_state_facts(
                session_id=session_id,
                commit_seq=fact_commit_seq,
                catalog_facts=snapshot,
                heads=heads,
                supported_operations=(),
                now=observed_at,
            )
            catalog_origin = str(catalog_row["origin_kind"] or "").strip() or None
            thread_origin = str(primary_thread.get("origin_kind") or "").strip() or None
            facts = CodexLaunchVisibilityRepairFacts(
                session_id=session_id,
                provider=str(catalog_row["provider"] or "").strip() or None,
                mode=projected.mode,
                execution_home="managed_local" if managed_local else None,
                control_ownership=(
                    "owned" if owned_connection and projected.control is not None and projected.control.ownership == "owned" else "unowned"
                ),
                fresh_exact_terminal_attached=bool(
                    projected.control is not None
                    and projected.control.terminal_attached is True
                    and projected.control_run_id == current_run_id
                ),
                fresh_exact_active_run=bool(
                    projected.run is not None
                    and projected.run.lifecycle == "running"
                    and projected.activity.state in {"thinking", "executing"}
                ),
                launch_actor=str(catalog_row["launch_actor"] or "").strip() or None,
                launch_surface=str(catalog_row["launch_surface"] or "").strip() or None,
                origin_kind=catalog_origin or thread_origin,
                is_sidechain=primary_thread.get("branch_kind") == "subagent",
                environment=str(catalog_row["environment"] or "").strip() or None,
                hidden_from_default_timeline=bool(catalog_row["hidden_from_default_timeline"]),
                primary_thread_hidden_from_default_timeline=bool(primary_thread.get("hidden_from_default_timeline")),
                user_hidden_from_timeline=bool(catalog_row["user_hidden_from_timeline"]),
            )
            refusals = list(codex_launch_visibility_repair_refusals(facts))
            if card_row["provider"] != catalog_row["provider"]:
                refusals.append("timeline_card_provider_mismatch")
            if card_row["environment"] != catalog_row["environment"]:
                refusals.append("timeline_card_environment_mismatch")
            for field in ("origin_kind", "launch_actor", "launch_surface"):
                if card_row[field] is not None:
                    refusals.append(f"timeline_card_{field}_already_set")
            if not bool(card_row["hidden_from_default_timeline"]):
                refusals.append("timeline_card_not_policy_hidden")
            if bool(card_row["user_hidden_from_timeline"]):
                refusals.append("timeline_card_user_hidden")
            plan = plan_codex_launch_visibility_repair(facts)
            if plan is None or refusals:
                return {
                    "eligible": False,
                    "applied": False,
                    "dry_run": dry_run,
                    "refusals": refusals,
                    "commit_seq": str(_current_commit_seq(connection)),
                }

            fingerprint = codex_launch_visibility_repair_fingerprint(plan)
            response = {
                "eligible": True,
                "applied": False,
                "dry_run": dry_run,
                "expected_fingerprint": fingerprint,
                "compare_and_set": plan.compare_and_set,
                "updates": plan.updates,
                "commit_seq": str(_current_commit_seq(connection)),
            }
            if dry_run:
                return response
            if expected_fingerprint != fingerprint:
                return {
                    **response,
                    "eligible": False,
                    "refusals": ["compare_and_set_failed"],
                }

            catalog_updated = connection.execute(
                update(catalog_table)
                .where(
                    catalog_table.c.session_id == session_id,
                    catalog_table.c.provider == "codex",
                    catalog_table.c.environment == facts.environment,
                    catalog_table.c.origin_kind.is_(None),
                    catalog_table.c.launch_actor.is_(None),
                    catalog_table.c.launch_surface.is_(None),
                    catalog_table.c.hidden_from_default_timeline == 1,
                    catalog_table.c.user_hidden_from_timeline == 0,
                )
                .values(
                    launch_actor="human_shell",
                    launch_surface="terminal",
                    hidden_from_default_timeline=0,
                    updated_at=observed_at,
                )
            ).rowcount
            card_updated = connection.execute(
                update(card_table)
                .where(
                    card_table.c.session_id == session_id,
                    card_table.c.provider == "codex",
                    card_table.c.environment == facts.environment,
                    card_table.c.origin_kind.is_(None),
                    card_table.c.launch_actor.is_(None),
                    card_table.c.launch_surface.is_(None),
                    card_table.c.hidden_from_default_timeline == 1,
                    card_table.c.user_hidden_from_timeline == 0,
                )
                .values(
                    launch_actor="human_shell",
                    launch_surface="terminal",
                    hidden_from_default_timeline=0,
                    updated_at=observed_at,
                )
            ).rowcount
            thread_updated = connection.execute(
                update(thread_table)
                .where(
                    thread_table.c.id == primary_thread.get("id"),
                    thread_table.c.session_id == session_id,
                    thread_table.c.branch_kind == "root",
                    thread_table.c.origin_kind.is_(None),
                    thread_table.c.hidden_from_default_timeline == int(facts.primary_thread_hidden_from_default_timeline),
                )
                .values(hidden_from_default_timeline=0, updated_at=observed_at)
            ).rowcount
            if (catalog_updated, card_updated, thread_updated) != (1, 1, 1):
                raise RuntimeError("Codex launch visibility CAS changed during its write transaction")
            commit_seq = _advance_commit_seq(connection, observed_at)
            return {
                **response,
                "applied": True,
                "dry_run": False,
                "commit_seq": str(commit_seq),
            }

    def apply_control_command_result(
        self,
        *,
        owner_id: int,
        device_id: str,
        message: dict[str, Any],
    ) -> dict[str, Any]:
        """Reconcile one unmatched control result without opening SQLite in the API."""

        from zerg.services.machine_control_operations import TERMINAL_OPERATION_STATUSES

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

        from zerg.services.live_control_catalog import get_canonical_live_control_grant
        from zerg.services.machine_control_operations import MACHINE_OPERATION_TIMEOUT_GRACE_SECS

        observed_at = datetime.now(UTC)
        with _write_transaction(self.engine) as connection:
            orm = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            try:

                def resolve_grant():
                    return get_canonical_live_control_grant(
                        orm,
                        session_id=session_id,
                        provider=provider,
                        device_id=device_id,
                        capability=capability,
                        now=observed_at,
                    )

                existing = orm.query(LiveMachineControlOperation).filter(LiveMachineControlOperation.command_id == command_id).one_or_none()
                if existing is not None:
                    stored_request = _decode_json_object(existing.request_json)
                    stored_grant = stored_request.pop("longhouse_control_grant", None)
                    request_matches = (
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
                    if not request_matches:
                        orm.rollback()
                        return {
                            "allowed": False,
                            "reason": "idempotency_conflict",
                            "operation_id": None,
                            "grant": None,
                            "exact_replay": False,
                            "commit_seq": str(_current_commit_seq(connection)),
                        }
                    if str(existing.status) != "running":
                        orm.rollback()
                        return {
                            "allowed": False,
                            "reason": "operation_finished",
                            "operation_id": None,
                            "grant": None,
                            "exact_replay": False,
                            "commit_seq": str(_current_commit_seq(connection)),
                        }
                    grant, denial_reason = resolve_grant()
                    current_grant = _live_control_grant_payload(grant) if grant is not None else None
                    exact_replay = bool(
                        current_grant is not None
                        and isinstance(stored_grant, dict)
                        and _stored_live_control_grant_matches(stored_grant, current_grant)
                    )
                    orm.rollback()
                    return {
                        "allowed": exact_replay,
                        "reason": None if exact_replay else denial_reason or "grant_revoked",
                        "operation_id": str(existing.id) if exact_replay else None,
                        "grant": current_grant if exact_replay else None,
                        "exact_replay": exact_replay,
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                grant, denial_reason = resolve_grant()
                if grant is None:
                    orm.rollback()
                    return {
                        "allowed": False,
                        "reason": denial_reason or "control_unavailable",
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                grant_payload = _live_control_grant_payload(grant)
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
                            "longhouse_control_grant": grant_payload,
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
                "grant": grant_payload,
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

    def reap_stale_control_operations(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Expire leased machine-control operations that never received a result.

        Catalog mode owns live writes; the API process must not open the live
        WriteSerializer for this. Opportunistic reaping on prepare/read still
        happens, and this periodic path covers abandoned operations nobody
        touches again.
        """

        from zerg.services.machine_control_operations import NONTERMINAL_OPERATION_STATUSES

        observed_at = now or datetime.now(UTC)
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
                orm.commit() if stale else orm.rollback()
            except BaseException:
                orm.rollback()
                raise
            finally:
                orm.close()
            commit_seq = _advance_commit_seq(connection, observed_at) if stale else _current_commit_seq(connection)
            return {"reaped_count": len(stale), "commit_seq": str(commit_seq)}

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

    def create_branch_session(self, *, data: dict[str, Any]) -> dict[str, Any]:
        """Create a branch, its lineage, and its first turn in one transaction.

        Creating the child and enqueuing its first turn cannot be two calls.
        Between them the parent can be resumed on its machine, which changes the
        thread the fork would be taken from, and a client that crashed in the
        gap would leave an empty session nobody asked for. They are separate
        catalogd RPCs today precisely because nothing needed them together.

        What this cannot make atomic is the dispatch that follows: the Machine
        Agent is contacted after commit. A branch whose send fails is therefore a
        durable branch with a failed first turn, which is the honest outcome --
        the child exists, the user can see it, and the turn says what went wrong.
        Hiding it behind an error would lose a session that was really created.
        """

        from zerg.services.live_archive_outbox import enqueue_console_session_create_outbox
        from zerg.services.live_catalog_launch import create_live_console_session_shell

        now = data["created_at"]
        parent_session_id = str(data["parent_session_id"])
        owner_id = int(data["owner_id"])
        client_request_id = str(data["client_request_id"])
        with _write_transaction(self.engine) as connection:
            orm = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            try:
                parent = orm.get(LiveSessionCatalog, parent_session_id)
                if parent is None or not parent.primary_thread_id:
                    orm.rollback()
                    return {"found": False}
                if not self._session_explicitly_belongs_to_owner(connection, session_id=parent_session_id, owner_id=owner_id):
                    orm.rollback()
                    return {"found": False}

                # Replay is keyed on the parent, not the child: on a retry the
                # child does not exist yet, so the receipt's own session id
                # cannot identify the request that would have created it.
                existing = (
                    orm.query(LiveSessionInputReceipt, LiveSessionThread)
                    .join(LiveSessionThread, LiveSessionThread.session_id == LiveSessionInputReceipt.session_id)
                    .filter(
                        LiveSessionInputReceipt.owner_id == owner_id,
                        LiveSessionInputReceipt.client_request_id == client_request_id,
                        LiveSessionThread.parent_session_id == parent_session_id,
                    )
                    .first()
                )
                if existing is not None:
                    receipt, child_thread = existing
                    turn = orm.query(LiveConsoleTurn).filter(LiveConsoleTurn.receipt_id == receipt.id).one()
                    exact = receipt.text == data["message"]
                    replay = _live_console_turn_dto(
                        turn,
                        message=receipt.text,
                        client_request_id=receipt.client_request_id,
                        provider_config=child_thread.provider_config_json,
                        model=turn.model,
                        reporting_turn_ids=_console_turns_with_reporting_run(orm, [turn], observed_at=datetime.now(UTC)),
                    )
                    orm.rollback()
                    return {
                        "found": True,
                        "created": False,
                        "idempotency_conflict": not exact,
                        "session_id": child_thread.session_id,
                        "thread_id": child_thread.id,
                        "turn": replay if exact else None,
                    }

                parent_thread = orm.get(LiveSessionThread, str(parent.primary_thread_id))
                if parent_thread is None or not parent_thread.device_id or not parent_thread.cwd:
                    orm.rollback()
                    return {"found": True, "unavailable": "execution_target_missing"}

                # The thread to fork from, read at execution time rather than
                # when the user tapped. There is no way to pin an earlier point:
                # ThreadForkParams.lastTurnId exists but no provider turn id is
                # available here, so the fork is taken at the tip.
                parent_alias = (
                    orm.query(LiveSessionThreadAlias)
                    .filter(
                        LiveSessionThreadAlias.thread_id == parent_thread.id,
                        LiveSessionThreadAlias.provider == parent.provider,
                        LiveSessionThreadAlias.alias_kind == "provider_session_id",
                    )
                    .order_by(
                        LiveSessionThreadAlias.last_seen_at.desc(),
                        LiveSessionThreadAlias.first_seen_at.desc(),
                        LiveSessionThreadAlias.id.desc(),
                    )
                    .first()
                )
                if parent_alias is None or not parent_alias.alias_value:
                    orm.rollback()
                    return {"found": True, "unavailable": "provider_identity_missing"}

                child_session_id = str(data["session_id"])
                child_thread_id = str(data["thread_id"])
                parent_provider_config = _decode_json_object(parent_thread.provider_config_json)
                shell = {
                    "session_id": child_session_id,
                    "thread_id": child_thread_id,
                    "owner_id": owner_id,
                    "provider": parent.provider,
                    "device_id": parent_thread.device_id,
                    "cwd": parent_thread.cwd,
                    "project": parent.project,
                    "display_name": data.get("display_name"),
                    "provider_config": {"permission_mode": "bypass"},
                    "launch_actor": "user",
                    "launch_surface": str(data.get("launch_surface") or "console"),
                    "started_at": now,
                    "parent_thread_id": parent_thread.id,
                    "parent_session_id": parent_session_id,
                    "branch_kind": "fork",
                }
                create_live_console_session_shell(orm, data=shell)
                enqueue_console_session_create_outbox(orm, session=shell)

                # Provider-tier lineage, on the non-routing alias kind. The
                # routing alias is uniquely indexed and belongs to whichever
                # thread actually owns that provider session; the child will
                # claim its own once the fork returns a new id.
                orm.add(
                    LiveSessionThreadAlias(
                        thread_id=child_thread_id,
                        provider=parent.provider,
                        alias_kind="forked_from_provider_session_id",
                        alias_value=parent_alias.alias_value,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )

                receipt_id = str(uuid4())
                turn_id = str(uuid4())
                run_id = str(uuid4())
                receipt = LiveSessionInputReceipt(
                    id=receipt_id,
                    owner_id=owner_id,
                    session_id=child_session_id,
                    thread_id=child_thread_id,
                    provider=parent.provider,
                    device_id=parent_thread.device_id,
                    client_request_id=client_request_id,
                    intent="auto",
                    status="delivering",
                    text=data["message"],
                    delivery_request_id=run_id,
                    created_at=now,
                    updated_at=now,
                )
                turn = LiveConsoleTurn(
                    id=turn_id,
                    session_id=child_session_id,
                    thread_id=child_thread_id,
                    receipt_id=receipt_id,
                    run_id=run_id,
                    state="starting",
                    provider=parent.provider,
                    device_id=parent_thread.device_id,
                    cwd=parent_thread.cwd,
                    model=parent_provider_config.get("model") if isinstance(parent_provider_config.get("model"), str) else None,
                    # A branch's first turn forks and never resumes. The child
                    # owns no thread yet, so a resume identity here would be the
                    # parent's and would continue it instead of branching it.
                    resume_provider_thread_id=None,
                    fork_from_provider_thread_id=parent_alias.alias_value,
                    created_at=now,
                    updated_at=now,
                )
                orm.add_all([receipt, turn])
                orm.add(
                    LiveSessionRun(
                        id=run_id,
                        thread_id=child_thread_id,
                        provider=parent.provider,
                        host_id=parent_thread.device_id,
                        cwd=parent_thread.cwd,
                        launch_origin="longhouse_spawned",
                        started_at=now,
                    )
                )
                orm.commit()
                result = {
                    "found": True,
                    "created": True,
                    "session_id": child_session_id,
                    "thread_id": child_thread_id,
                    "turn": _live_console_turn_dto(
                        turn,
                        message=receipt.text,
                        client_request_id=client_request_id,
                        provider_config=json.dumps(shell["provider_config"], sort_keys=True),
                        model=turn.model,
                    ),
                }
            except BaseException:
                orm.rollback()
                raise
            finally:
                orm.close()
        return result

    def enqueue_console_turn(self, *, data: dict[str, Any]) -> dict[str, Any]:
        """Accept one idempotent Console message and claim it when the thread is idle."""

        now = data["created_at"]
        report_id = data.get("report_id")
        if report_id is not None:
            try:
                report_id = str(UUID(str(report_id)))
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid report_id") from exc
        attachment_refs = data.get("attachments") or []
        if not isinstance(attachment_refs, list) or not all(isinstance(ref, dict) for ref in attachment_refs):
            raise ValueError("invalid attachments")
        attachments_digest = data.get("attachments_digest")
        attachments_digest = str(attachments_digest) if attachments_digest else None
        requested_receipt_id = data.get("receipt_id")
        if requested_receipt_id is not None:
            try:
                requested_receipt_id = str(UUID(str(requested_receipt_id)))
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid receipt_id") from exc
        attachments_json = (
            json.dumps({"digest": attachments_digest, "refs": attachment_refs}, sort_keys=True)
            if attachment_refs or attachments_digest
            else None
        )
        with _write_transaction(self.engine) as connection:
            orm = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            try:
                session = orm.get(LiveSessionCatalog, data["session_id"])
                if session is None or not session.primary_thread_id:
                    orm.rollback()
                    return {"found": False}
                if not self._session_explicitly_belongs_to_owner(
                    connection,
                    session_id=str(data["session_id"]),
                    owner_id=int(data["owner_id"]),
                ):
                    orm.rollback()
                    return {"found": False}
                if str(session.origin_kind or "").strip().lower() != "console":
                    orm.rollback()
                    return {"found": True, "unavailable": "not_console_session"}
                thread = orm.get(LiveSessionThread, str(session.primary_thread_id))
                if thread is None or not thread.device_id or not thread.cwd:
                    orm.rollback()
                    return {"found": True, "unavailable": "execution_target_missing"}
                if str(session.provider or "").strip().lower() == "pi":
                    from zerg.services.live_catalog_launch import normalize_console_provider_config

                    thread.provider_config_json = json.dumps(
                        normalize_console_provider_config("pi", _decode_json_object(thread.provider_config_json)),
                        sort_keys=True,
                    )
                    session.permission_mode = "provider_local"
                    session.permission_mode_source = "console_default"
                thread_provider_config = _decode_json_object(thread.provider_config_json)
                requested_model = data["model"] if "model" in data else thread_provider_config.get("model")
                turn_model = requested_model if isinstance(requested_model, str) else None
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
                    stored_digest = _console_turn_attachments(turn).get("digest") or None
                    digest_matches = stored_digest == attachments_digest
                    model_matches = "model" not in data or turn.model == turn_model
                    exact = existing_receipt.text == data["message"] and turn.report_id == report_id and digest_matches and model_matches
                    replay_turn = _live_console_turn_dto(
                        turn,
                        message=existing_receipt.text,
                        client_request_id=existing_receipt.client_request_id,
                        provider_config=thread.provider_config_json,
                        model=turn.model,
                        reporting_turn_ids=_console_turns_with_reporting_run(orm, [turn], observed_at=datetime.now(UTC)),
                        resume_session_file=_live_thread_source_path(
                            orm,
                            thread_id=thread.id,
                            provider=session.provider,
                        ),
                        error_code=_receipt_error_code(existing_receipt),
                    )
                    orm.rollback()
                    return {
                        "found": True,
                        "created": False,
                        "idempotency_conflict": not exact,
                        "turn": replay_turn if exact else None,
                    }
                if report_id is not None:
                    existing_report_turn = (
                        orm.query(LiveConsoleTurn)
                        .join(LiveSessionInputReceipt, LiveSessionInputReceipt.id == LiveConsoleTurn.receipt_id)
                        .filter(
                            LiveSessionInputReceipt.owner_id == data["owner_id"],
                            LiveConsoleTurn.report_id == report_id,
                            LiveConsoleTurn.state.in_(("queued", "starting", "active", "draining")),
                        )
                        .order_by(LiveConsoleTurn.created_at.asc())
                        .first()
                    )
                    if existing_report_turn is not None:
                        existing_receipt = orm.get(LiveSessionInputReceipt, existing_report_turn.receipt_id)
                        existing_thread = orm.get(LiveSessionThread, existing_report_turn.thread_id)
                        existing_dto = _live_console_turn_dto(
                            existing_report_turn,
                            reporting_turn_ids=_console_turns_with_reporting_run(
                                orm, [existing_report_turn], observed_at=datetime.now(UTC)
                            ),
                            message=existing_receipt.text if existing_receipt is not None else None,
                            client_request_id=existing_receipt.client_request_id if existing_receipt is not None else None,
                            provider_config=existing_thread.provider_config_json if existing_thread is not None else None,
                            model=existing_report_turn.model,
                            resume_session_file=(
                                _live_thread_source_path(
                                    orm,
                                    thread_id=existing_report_turn.thread_id,
                                    provider=existing_report_turn.provider,
                                )
                                if existing_thread is not None
                                else None
                            ),
                            error_code=_receipt_error_code(existing_receipt),
                        )
                        orm.rollback()
                        return {
                            "found": True,
                            "created": False,
                            "report_conflict": True,
                            "turn": existing_dto,
                        }
                execution_owner = (
                    orm.query(LiveSessionRun.id)
                    .join(LiveSessionConnection, LiveSessionConnection.run_id == LiveSessionRun.id)
                    .filter(
                        LiveSessionRun.thread_id == thread.id,
                        LiveSessionRun.ended_at.is_(None),
                        LiveSessionConnection.acquisition_kind.in_(("spawned_control", "adopted_control")),
                        LiveSessionConnection.released_at.is_(None),
                    )
                    .first()
                )
                if execution_owner is not None:
                    orm.rollback()
                    return {"found": True, "unavailable": "execution_owner_conflict"}
                turn, receipt, source_path = _create_console_turn_rows(
                    orm,
                    session=session,
                    thread=thread,
                    owner_id=int(data["owner_id"]),
                    message=data["message"],
                    client_request_id=data["client_request_id"],
                    created_at=now,
                    report_id=report_id,
                    attachments_json=attachments_json,
                    model=turn_model,
                    receipt_id=requested_receipt_id,
                )
                orm.commit()
                result = _live_console_turn_dto(
                    turn,
                    message=receipt.text,
                    client_request_id=receipt.client_request_id,
                    provider_config=thread.provider_config_json,
                    model=turn.model,
                    resume_session_file=source_path,
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
                turn = (
                    orm.query(LiveConsoleTurn)
                    .join(LiveSessionInputReceipt, LiveSessionInputReceipt.id == LiveConsoleTurn.receipt_id)
                    .filter(
                        LiveConsoleTurn.run_id == data["run_id"],
                        LiveConsoleTurn.session_id == data["session_id"],
                        LiveConsoleTurn.thread_id == data["thread_id"],
                        LiveConsoleTurn.provider == data["provider"],
                        LiveConsoleTurn.device_id == data["device_id"],
                        LiveSessionInputReceipt.owner_id == data["owner_id"],
                    )
                    .one_or_none()
                )
                if turn is None or (data.get("turn_id") and turn.id != data["turn_id"]):
                    orm.rollback()
                    return {"found": False}
                receipt = orm.get(LiveSessionInputReceipt, turn.receipt_id)
                expected_state = data.get("expected_state")
                if expected_state is not None and turn.state != expected_state:
                    thread = orm.get(LiveSessionThread, turn.thread_id)
                    result = _live_console_turn_dto(
                        turn,
                        message=receipt.text if receipt is not None else None,
                        client_request_id=receipt.client_request_id if receipt is not None else None,
                        provider_config=thread.provider_config_json if thread is not None else None,
                        model=turn.model,
                        error_code=_receipt_error_code(receipt),
                        resume_session_file=(
                            _live_thread_source_path(orm, thread_id=thread.id, provider=turn.provider) if thread is not None else None
                        ),
                    )
                    orm.rollback()
                    return {
                        "found": True,
                        "applied": False,
                        "stale": True,
                        "turn": result,
                        "next_turn": None,
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                next_state = data["state"]
                if turn.state in CONSOLE_TURN_TERMINAL_STATES:
                    if next_state != turn.state:
                        thread = orm.get(LiveSessionThread, turn.thread_id)
                        result = _live_console_turn_dto(
                            turn,
                            client_request_id=receipt.client_request_id if receipt is not None else None,
                            provider_config=thread.provider_config_json if thread is not None else None,
                            model=turn.model,
                            error_code=_receipt_error_code(receipt),
                            resume_session_file=(
                                _live_thread_source_path(orm, thread_id=thread.id, provider=turn.provider) if thread is not None else None
                            ),
                        )
                        orm.rollback()
                        return {
                            "found": True,
                            "applied": False,
                            "stale": True,
                            "exact_replay": False,
                            "turn": result,
                            "next_turn": None,
                            "commit_seq": str(_current_commit_seq(connection)),
                        }
                    next_turn_result = _starting_console_turn_dto(orm, thread_id=turn.thread_id)
                    thread = orm.get(LiveSessionThread, turn.thread_id)
                    result = _live_console_turn_dto(
                        turn,
                        client_request_id=receipt.client_request_id if receipt is not None else None,
                        provider_config=thread.provider_config_json if thread is not None else None,
                        model=turn.model,
                        error_code=_receipt_error_code(receipt),
                        resume_session_file=(
                            _live_thread_source_path(orm, thread_id=thread.id, provider=turn.provider) if thread is not None else None
                        ),
                    )
                    orm.rollback()
                    return {
                        "found": True,
                        "applied": False,
                        "stale": False,
                        "exact_replay": True,
                        "turn": result,
                        "next_turn": next_turn_result,
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                next_turn_result = _settle_console_turn(
                    orm,
                    turn,
                    receipt,
                    next_state=next_state,
                    error=data.get("error"),
                    error_code=data.get("error_code"),
                    now=now,
                )
                orm.commit()
                thread = orm.get(LiveSessionThread, turn.thread_id)
                result = _live_console_turn_dto(
                    turn,
                    client_request_id=receipt.client_request_id if receipt is not None else None,
                    provider_config=thread.provider_config_json if thread is not None else None,
                    model=turn.model,
                    error_code=_receipt_error_code(receipt),
                    resume_session_file=(
                        _live_thread_source_path(orm, thread_id=thread.id, provider=turn.provider) if thread is not None else None
                    ),
                )
            except BaseException:
                orm.rollback()
                raise
            finally:
                orm.close()
            commit_seq = _advance_commit_seq(connection, now)
            return {
                "found": True,
                "applied": True,
                "stale": False,
                "exact_replay": False,
                "turn": result,
                "next_turn": next_turn_result,
                "commit_seq": str(commit_seq),
            }

    def list_starting_console_turns_for_device(self, *, owner_id: int, device_id: str) -> dict[str, Any]:
        """Return durable ambiguous dispatches that can be replayed by stable run_id."""

        with self.engine.connect() as connection:
            orm = Session(bind=connection)
            try:
                rows = (
                    orm.query(LiveConsoleTurn, LiveSessionInputReceipt, LiveSessionThread)
                    .join(LiveSessionInputReceipt, LiveSessionInputReceipt.id == LiveConsoleTurn.receipt_id)
                    .join(LiveSessionThread, LiveSessionThread.id == LiveConsoleTurn.thread_id)
                    .filter(
                        LiveConsoleTurn.state == "starting",
                        LiveConsoleTurn.device_id == device_id,
                        LiveSessionInputReceipt.owner_id == owner_id,
                    )
                    .order_by(LiveConsoleTurn.created_at.asc(), LiveConsoleTurn.id.asc())
                    .limit(100)
                    .all()
                )
                reporting_turn_ids = _console_turns_with_reporting_run(
                    orm, [turn for turn, _receipt, _thread in rows], observed_at=datetime.now(UTC)
                )
                return {
                    "turns": [
                        _live_console_turn_dto(
                            turn,
                            message=receipt.text,
                            client_request_id=receipt.client_request_id,
                            provider_config=thread.provider_config_json,
                            model=turn.model,
                            reporting_turn_ids=reporting_turn_ids,
                            resume_session_file=_live_thread_source_path(
                                orm,
                                thread_id=thread.id,
                                provider=turn.provider,
                            ),
                        )
                        for turn, receipt, thread in rows
                    ],
                    "commit_seq": str(_current_commit_seq(connection)),
                }
            finally:
                orm.close()

    def read_current_console_turn(self, *, session_id: str, owner_id: int) -> dict[str, Any]:
        with self.engine.connect() as connection:
            if not self._session_explicitly_belongs_to_owner(
                connection,
                session_id=session_id,
                owner_id=owner_id,
            ):
                return {"found": False}
            orm = Session(bind=connection)
            try:
                turn = (
                    orm.query(LiveConsoleTurn)
                    .filter(
                        LiveConsoleTurn.session_id == session_id,
                        LiveConsoleTurn.state.in_(("starting", "active", "draining")),
                    )
                    .order_by(LiveConsoleTurn.created_at.asc(), LiveConsoleTurn.id.asc())
                    .first()
                )
                if turn is None:
                    observed_at = datetime.now(UTC)
                    snapshots = _assemble_session_facts(
                        connection,
                        session_ids=[session_id],
                        observed_at=observed_at,
                        compact=True,
                    )
                    if snapshots:
                        snapshot = snapshots[0]
                        commit_seq, heads = read_session_fact_heads(connection, session_id=session_id)
                        from zerg.services.session_state_facts_projector import project_shadow_session_state_facts

                        projected = project_shadow_session_state_facts(
                            session_id=session_id,
                            commit_seq=commit_seq,
                            catalog_facts=snapshot,
                            heads=heads,
                            now=observed_at,
                        )
                        primary_thread = snapshot.get("primary_thread") or {}
                        latest_run = snapshot.get("latest_run") or {}
                        run_id = str(latest_run.get("id") or "")
                        latest_turn = (
                            orm.query(LiveConsoleTurn)
                            .filter(
                                LiveConsoleTurn.session_id == session_id,
                                LiveConsoleTurn.thread_id == str(primary_thread.get("id") or ""),
                                LiveConsoleTurn.run_id == run_id,
                            )
                            .order_by(LiveConsoleTurn.created_at.desc(), LiveConsoleTurn.id.desc())
                            .first()
                        )
                        if (
                            projected.mode == "console"
                            and projected.disposition.state == "open"
                            and projected.run is not None
                            and projected.run.lifecycle == "ended"
                            and projected.delegation.state == "pending"
                            and projected.delegation.count > 0
                            and latest_run.get("ended_at") is not None
                            and latest_turn is not None
                            and latest_turn.state in CONSOLE_TURN_TERMINAL_STATES
                        ):
                            return {
                                "found": True,
                                "turn": None,
                                "parked_invocation": {
                                    "turn_id": str(latest_turn.id),
                                    "run_id": run_id,
                                    "thread_id": str(primary_thread["id"]),
                                    "provider": str((snapshot.get("catalog") or {}).get("provider") or ""),
                                    "device_id": str(primary_thread.get("device_id") or ""),
                                },
                            }
                    return {"found": True, "turn": None}
                receipt = orm.get(LiveSessionInputReceipt, turn.receipt_id)
                thread = orm.get(LiveSessionThread, turn.thread_id)
                return {
                    "found": True,
                    "turn": _live_console_turn_dto(
                        turn,
                        message=receipt.text if receipt is not None else None,
                        client_request_id=receipt.client_request_id if receipt is not None else None,
                        provider_config=thread.provider_config_json if thread is not None else None,
                        model=turn.model,
                        error_code=_receipt_error_code(receipt),
                        reporting_turn_ids=_console_turns_with_reporting_run(orm, [turn], observed_at=datetime.now(UTC)),
                        resume_session_file=(
                            _live_thread_source_path(orm, thread_id=thread.id, provider=turn.provider) if thread is not None else None
                        ),
                    ),
                }
            finally:
                orm.close()

    def create_local_launch(self, *, launch: dict[str, Any]) -> dict[str, Any]:
        """Atomically create a Helm launch shell, control attachment, and outbox row."""

        from zerg.services.agents.session_graph_writes import primary_thread_id_for_session
        from zerg.services.live_archive_outbox import enqueue_managed_local_launch_outbox
        from zerg.services.live_catalog_launch import attach_live_catalog_control
        from zerg.services.live_catalog_launch import create_live_launch_catalog_shell
        from zerg.services.live_catalog_launch import live_launch_result
        from zerg.services.live_launch_readiness import upsert_live_launch_readiness
        from zerg.services.managed_local_launcher import managed_local_run_id_for_session
        from zerg.services.managed_local_launcher import managed_provider_has_lease_observer
        from zerg.services.managed_local_launcher import managed_provider_requires_readiness_proof

        plan_payload = dict(launch["plan"])
        session_id = UUID(plan_payload["session_id"])
        plan = SimpleNamespace(**{**plan_payload, "session_id": session_id})
        expected_provider_session_id = str(plan.provider_session_id or "").strip() or None
        command_id = f"managed-local-{session_id}"
        observed_at = launch["started_at"]
        thread_id = primary_thread_id_for_session(session_id)
        run_id = managed_local_run_id_for_session(session_id)
        replay_contract = {
            "owner_id": launch["owner_id"],
            "git_repo": launch.get("git_repo"),
            "git_branch": launch.get("git_branch"),
            "plan": {**plan_payload, "session_id": str(session_id)},
        }
        launch_fingerprint = hashlib.sha256(json.dumps(replay_contract, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        with _write_transaction(self.engine) as connection:
            orm = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            try:
                existing = orm.query(LiveSessionLaunchAttempt).filter(LiveSessionLaunchAttempt.command_id == command_id).one_or_none()
                if existing is not None:
                    # The durable outbox is consumable transport state, not an
                    # idempotency requirement. Once archive drains it, comparing
                    # its vanished payload turns a genuine retry into a false
                    # 409. Keep the established persisted launch-attribute
                    # checks, but do not require the outbox row to survive.
                    catalog = orm.get(LiveSessionCatalog, str(session_id))
                    provider_session_id = expected_provider_session_id or ""
                    provider_alias = None
                    if provider_session_id:
                        provider_alias = (
                            orm.query(LiveSessionThreadAlias)
                            .filter(
                                LiveSessionThreadAlias.thread_id == str(existing.thread_id or ""),
                                LiveSessionThreadAlias.provider == plan.provider,
                                LiveSessionThreadAlias.alias_kind == "provider_session_id",
                                LiveSessionThreadAlias.alias_value == provider_session_id,
                            )
                            .one_or_none()
                        )
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
                        and str(catalog.permission_mode or "") == plan.permission_mode
                        and (not provider_session_id or provider_alias is not None)
                        # Rows created before launch_fingerprint existed retain
                        # the persisted-field fallback above. New rows compare
                        # the complete request contract without depending on a
                        # consumable archive outbox record.
                        and (existing.launch_fingerprint is None or hmac.compare_digest(existing.launch_fingerprint, launch_fingerprint))
                    )
                    result = live_launch_result(existing) if exact_replay else None
                    orm.rollback()
                    return {
                        "created": False,
                        "exact_replay": exact_replay,
                        "idempotency_conflict": not exact_replay,
                        "run_id": str(run_id),
                        "provider_session_id": provider_session_id or None,
                        "launch": _json_launch_result(result) if result is not None else None,
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                if expected_provider_session_id:
                    existing_provider_alias = (
                        orm.query(LiveSessionThreadAlias)
                        .filter(
                            LiveSessionThreadAlias.provider == plan.provider,
                            LiveSessionThreadAlias.alias_kind == "provider_session_id",
                            LiveSessionThreadAlias.alias_value == expected_provider_session_id,
                        )
                        .first()
                    )
                    if existing_provider_alias is not None and str(existing_provider_alias.thread_id) != str(thread_id):
                        orm.rollback()
                        return {
                            "created": False,
                            "exact_replay": False,
                            "idempotency_conflict": False,
                            "conflict": "provider session identity is already bound to another live thread",
                            "run_id": str(run_id),
                            "provider_session_id": expected_provider_session_id,
                            "launch": None,
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
                    launch_fingerprint=launch_fingerprint,
                    started_at=observed_at,
                    expires_at=launch["expires_at"],
                    launch_actor=plan.launch_actor,
                    launch_surface=plan.launch_surface,
                    # The replay comparison below already reads
                    # plan.permission_mode; not passing it here meant every Helm
                    # session persisted as bypass, and an idempotent relaunch of
                    # a provider_local session could never match its own row.
                    permission_mode=plan.permission_mode,
                    provider_config=plan.provider_config,
                    environment=getattr(plan, "environment", "development"),
                    origin_kind=getattr(plan, "origin_kind", None),
                    hidden_from_default_timeline=int(
                        getattr(
                            plan,
                            "hidden_from_default_timeline",
                            int(
                                getattr(plan, "origin_kind", None) in {"hatch_automation", "test_or_canary"}
                                or getattr(plan, "launch_actor", None) == "automation"
                            ),
                        )
                    ),
                )
                requires_readiness_proof = managed_provider_requires_readiness_proof(plan.provider)
                attach_live_catalog_control(
                    orm,
                    session_id=session_id,
                    provider=plan.provider,
                    device_id=plan.source_name,
                    state=("detached" if managed_provider_has_lease_observer(plan.provider) or requires_readiness_proof else "attached"),
                    external_name=plan.managed_session_name,
                    run_id=run_id,
                    provider_session_id=plan.provider_session_id,
                    observed_at=observed_at,
                    can_send_input=0 if requires_readiness_proof else None,
                )
                persisted_provider_alias = (
                    orm.query(LiveSessionThreadAlias)
                    .filter(
                        LiveSessionThreadAlias.thread_id == str(thread_id),
                        LiveSessionThreadAlias.provider == plan.provider,
                        LiveSessionThreadAlias.alias_kind == "provider_session_id",
                    )
                    .one_or_none()
                )
                persisted_provider_session_id = (
                    str(persisted_provider_alias.alias_value).strip() if persisted_provider_alias is not None else None
                )
                if persisted_provider_session_id != expected_provider_session_id:
                    orm.rollback()
                    return {
                        "created": False,
                        "exact_replay": False,
                        "idempotency_conflict": False,
                        "conflict": "provider session identity could not be bound to the launch thread",
                        "run_id": str(run_id),
                        "provider_session_id": expected_provider_session_id,
                        "launch": None,
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
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
                "run_id": str(run_id),
                "provider_session_id": persisted_provider_session_id,
                "launch": _json_launch_result(result),
                "commit_seq": str(commit_seq),
            }

    def resume_local_launch(self, *, resume: dict[str, Any]) -> dict[str, Any]:
        """Atomically claim one new run for an ended managed provider thread."""

        from zerg.services.live_catalog_launch import attach_live_catalog_control
        from zerg.services.live_catalog_launch import live_launch_result
        from zerg.services.live_launch_readiness import upsert_live_launch_readiness
        from zerg.services.managed_local_launcher import managed_local_resume_run_id

        session_id = str(resume["session_id"])
        attempt_id = str(resume["resume_attempt_id"])
        run_id = str(managed_local_resume_run_id(session_id, attempt_id))
        command_id = f"managed-resume-{attempt_id}"
        observed_at = resume["started_at"]
        with _write_transaction(self.engine) as connection:
            orm = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            try:
                existing = (
                    orm.query(LiveSessionLaunchAttempt)
                    .filter(
                        LiveSessionLaunchAttempt.session_id == session_id,
                        LiveSessionLaunchAttempt.client_request_id == attempt_id,
                    )
                    .one_or_none()
                )
                if existing is not None:
                    existing_run = orm.get(LiveSessionRun, run_id)
                    existing_alias = (
                        orm.query(LiveSessionThreadAlias)
                        .filter(
                            LiveSessionThreadAlias.thread_id == str(existing.thread_id or ""),
                            LiveSessionThreadAlias.provider == str(resume["provider"]),
                            LiveSessionThreadAlias.alias_kind == "provider_session_id",
                            LiveSessionThreadAlias.alias_value == str(resume["provider_thread_id"]),
                        )
                        .first()
                    )
                    exact = (
                        str(existing.run_id or "") == run_id
                        and str(existing.provider) == str(resume["provider"])
                        and str(existing.host_id or "") == str(resume["device_id"])
                        and int(existing.owner_id or 0) == int(resume["owner_id"])
                        and existing_run is not None
                        and str(existing_run.cwd or "") == str(resume["cwd"])
                        and existing_alias is not None
                    )
                    result = live_launch_result(existing) if exact else None
                    orm.rollback()
                    return {
                        "created": False,
                        "exact_replay": exact,
                        "conflict": None if exact else "resume attempt identity was reused with different attributes",
                        "run_id": run_id,
                        "provider_session_id": str(resume["provider_thread_id"]),
                        "launch": _json_launch_result(result) if result is not None else None,
                        "commit_seq": str(_current_commit_seq(connection)),
                    }

                catalog = orm.get(LiveSessionCatalog, session_id)
                original_attempt = (
                    orm.query(LiveSessionLaunchAttempt)
                    .filter(LiveSessionLaunchAttempt.session_id == session_id)
                    .order_by(LiveSessionLaunchAttempt.id.asc())
                    .first()
                )
                if catalog is None or original_attempt is None or not catalog.primary_thread_id:
                    orm.rollback()
                    return {"conflict": "managed session is not present in the live catalog"}
                thread_id = str(catalog.primary_thread_id)
                incoming_actor = str(resume.get("launch_actor") or "").strip() or None
                incoming_surface = str(resume.get("launch_surface") or "").strip() or None
                if incoming_actor is not None and catalog.launch_actor not in (None, incoming_actor):
                    orm.rollback()
                    return {"conflict": "managed resume launch actor conflicts with retained provenance"}
                if incoming_surface is not None and catalog.launch_surface not in (None, incoming_surface):
                    orm.rollback()
                    return {"conflict": "managed resume launch surface conflicts with retained provenance"}
                identity_matches = (
                    str(catalog.provider) == str(resume["provider"])
                    and str(catalog.device_id or "") == str(resume["device_id"])
                    and str(catalog.cwd or "") == str(resume["cwd"])
                    and int(original_attempt.owner_id or 0) == int(resume["owner_id"])
                )
                if not identity_matches:
                    orm.rollback()
                    return {"conflict": "managed resume contract does not match the retained session"}
                provider_alias = (
                    orm.query(LiveSessionThreadAlias)
                    .filter(
                        LiveSessionThreadAlias.thread_id == thread_id,
                        LiveSessionThreadAlias.provider == str(resume["provider"]),
                        LiveSessionThreadAlias.alias_kind == "provider_session_id",
                        LiveSessionThreadAlias.alias_value == str(resume["provider_thread_id"]),
                    )
                    .first()
                )
                if provider_alias is None:
                    orm.rollback()
                    return {"conflict": "provider thread does not match the session primary thread"}

                # Resume may be the first upgraded wrapper observation for a
                # legacy Codex row. Fill only missing provenance; never rewrite
                # a retained classification. Policy hiding remains independent
                # from the empty/content gate used by timeline reads.
                catalog.launch_actor = catalog.launch_actor or incoming_actor
                catalog.launch_surface = catalog.launch_surface or incoming_surface
                catalog.origin_kind = catalog.origin_kind or str(resume.get("origin_kind") or "").strip() or None
                storage_session = orm.get(StorageSession, session_id)
                primary_thread = orm.get(LiveSessionThread, thread_id)
                policy_hidden = evaluate_origin_visibility(
                    SessionVisibilityFacts(
                        provider=catalog.provider,
                        project=catalog.project,
                        environment=catalog.environment,
                        origin_kind=catalog.origin_kind,
                        launch_actor=catalog.launch_actor,
                        launch_surface=catalog.launch_surface,
                        cwd=storage_session.cwd if storage_session is not None else catalog.cwd,
                        machine_id=storage_session.machine_id if storage_session is not None else catalog.device_id,
                        primary_thread_is_worker_only=bool(primary_thread and primary_thread.branch_kind == "subagent"),
                        is_subagent=bool(storage_session is not None and storage_session.is_subagent),
                    )
                ).system_hidden
                catalog.hidden_from_default_timeline = int(policy_hidden)
                card = orm.get(LiveTimelineCard, session_id)
                if card is not None:
                    card.launch_actor = card.launch_actor or catalog.launch_actor
                    card.launch_surface = card.launch_surface or catalog.launch_surface
                    card.hidden_from_default_timeline = int(policy_hidden)
                    card.updated_at = observed_at
                if primary_thread is not None:
                    primary_thread.hidden_from_default_timeline = int(policy_hidden)
                    primary_thread.updated_at = observed_at
                open_run = (
                    orm.query(LiveSessionRun).filter(LiveSessionRun.thread_id == thread_id, LiveSessionRun.ended_at.is_(None)).first()
                )
                if open_run is not None:
                    if _open_run_holds_live_ownership(orm, run=open_run, observed_at=observed_at):
                        orm.rollback()
                        return {"conflict": "managed session already has a current run"}
                    # The run outlived its owner: the provider exited without a
                    # wrapper to report it, so no terminal fact ever retired the
                    # row and `ended_at IS NULL` misread as "still executing".
                    # Resume is the recovery path for exactly this session, so
                    # retire the orphan here rather than refusing it forever.
                    _retire_orphaned_open_run(orm, run=open_run, observed_at=observed_at)

                # A receipt claimed by the ended run may have reached the
                # provider even when its acknowledgement did not. Settle that
                # uncertainty explicitly. Unclaimed queued input remains
                # queued, but Resume itself does not claim or deliver it.
                stale_receipts = (
                    orm.query(LiveSessionInputReceipt)
                    .filter(
                        LiveSessionInputReceipt.session_id == session_id,
                        LiveSessionInputReceipt.status == "delivering",
                    )
                    .all()
                )
                for receipt in stale_receipts:
                    receipt.status = "failed"
                    receipt.error_json = json.dumps(
                        {
                            "code": "delivery_unknown",
                            "message": "Input was not carried across the managed-session recovery boundary; retry explicitly.",
                        },
                        sort_keys=True,
                    )
                    receipt.updated_at = observed_at

                run = LiveSessionRun(
                    id=run_id,
                    thread_id=thread_id,
                    provider=str(resume["provider"]),
                    host_id=str(resume["device_id"]),
                    cwd=str(resume["cwd"]),
                    launch_origin="longhouse_continued",
                    started_at=observed_at,
                )
                orm.add(run)
                attempt = LiveSessionLaunchAttempt(
                    session_id=session_id,
                    thread_id=thread_id,
                    run_id=run_id,
                    provider=str(resume["provider"]),
                    host_id=str(resume["device_id"]),
                    owner_id=int(resume["owner_id"]),
                    execution_lifetime="live_control",
                    client_request_id=attempt_id,
                    command_id=command_id,
                    state="pending",
                    expires_at=resume["expires_at"],
                    created_at=observed_at,
                    updated_at=observed_at,
                )
                orm.add(attempt)
                orm.flush()
                attach_live_catalog_control(
                    orm,
                    session_id=session_id,
                    provider=str(resume["provider"]),
                    device_id=str(resume["device_id"]),
                    state="detached",
                    run_id=run_id,
                    provider_session_id=str(resume["provider_thread_id"]),
                    launch_origin="longhouse_continued",
                    observed_at=observed_at,
                )
                upsert_live_launch_readiness(
                    orm,
                    session_id=UUID(session_id),
                    owner_id=int(resume["owner_id"]),
                    device_id=str(resume["device_id"]),
                    provider=str(resume["provider"]),
                    execution_lifetime="live_control",
                    state="pending",
                    command_id=command_id,
                    client_request_id=attempt_id,
                    machine_id=str(resume["device_id"]),
                    project=str(catalog.project or ""),
                    expires_at=resume["expires_at"],
                    now=observed_at,
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
                "conflict": None,
                "run_id": run_id,
                "provider_session_id": str(resume["provider_thread_id"]),
                "launch": _json_launch_result(result),
                "commit_seq": str(commit_seq),
            }

    def finish_local_launch(self, *, outcome: dict[str, Any]) -> dict[str, Any]:
        """Commit the provider-observed result of one registered Helm launch."""

        from zerg.services.live_catalog_launch import live_launch_result
        from zerg.services.live_catalog_launch import update_live_launch_catalog_outcome
        from zerg.services.live_launch_readiness import update_live_launch_readiness_state

        session_id = str(outcome["session_id"])
        run_id = str(outcome["run_id"])
        observed_at = outcome["observed_at"]
        requested_state = str(outcome["state"])
        with _write_transaction(self.engine) as connection:
            orm = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            try:
                attempt = (
                    orm.query(LiveSessionLaunchAttempt)
                    .filter(
                        LiveSessionLaunchAttempt.session_id == session_id,
                        LiveSessionLaunchAttempt.run_id == run_id,
                    )
                    .one_or_none()
                )
                if attempt is None:
                    # The original local-launch attempt predates assignment of
                    # its deterministic run id. Resume attempts are born with
                    # run_id and must never fall through to this legacy key.
                    attempt = (
                        orm.query(LiveSessionLaunchAttempt)
                        .filter(
                            LiveSessionLaunchAttempt.session_id == session_id,
                            LiveSessionLaunchAttempt.command_id == f"managed-local-{session_id}",
                            LiveSessionLaunchAttempt.run_id.is_(None),
                        )
                        .one_or_none()
                    )
                if attempt is None:
                    orm.rollback()
                    return {"found": False, "idempotency_conflict": False}
                run = orm.get(LiveSessionRun, run_id)
                identity_matches = (
                    str(attempt.session_id) == session_id
                    and int(attempt.owner_id or 0) == int(outcome["owner_id"])
                    and str(attempt.host_id or "") == str(outcome["device_id"])
                    and run is not None
                    and str(run.thread_id) == str(attempt.thread_id)
                    and str(run.host_id or "") == str(outcome["device_id"])
                )
                if not identity_matches:
                    orm.rollback()
                    return {"found": True, "idempotency_conflict": True}

                latest_run_id = (
                    orm.query(LiveSessionRun.id)
                    .filter(LiveSessionRun.thread_id == run.thread_id)
                    .order_by(LiveSessionRun.started_at.desc(), LiveSessionRun.id.desc())
                    .limit(1)
                    .scalar()
                )
                if str(latest_run_id or "") != run_id:
                    # A safe-retried outcome from an older launch generation
                    # must not overwrite session-keyed readiness or catalog
                    # state owned by a newer resumed run.
                    orm.rollback()
                    return {"found": True, "idempotency_conflict": True}

                current_state = str(attempt.state or "pending")
                if current_state in {"adopted", "failed", "abandoned"}:
                    exact_replay = (
                        current_state == requested_state
                        and str(attempt.run_id or run_id) == run_id
                        and (attempt.error_code or None) == outcome["error_code"]
                        and (attempt.error_message or None) == outcome["error_message"]
                    )
                    result = live_launch_result(attempt)
                    orm.rollback()
                    return {
                        "found": True,
                        "exact_replay": exact_replay,
                        "idempotency_conflict": not exact_replay,
                        "launch": _json_launch_result(result),
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                if current_state not in {"pending", "dispatched"}:
                    orm.rollback()
                    return {"found": True, "idempotency_conflict": True}

                attempt.run_id = run_id
                command_id = str(attempt.command_id or "")
                update_live_launch_catalog_outcome(
                    orm,
                    session_id=UUID(session_id),
                    command_id=command_id,
                    state=requested_state,
                    error_code=outcome["error_code"],
                    error_message=outcome["error_message"],
                    now=observed_at,
                )
                update_live_launch_readiness_state(
                    orm,
                    session_id=UUID(session_id),
                    state=requested_state,
                    error_code=outcome["error_code"],
                    error_message=outcome["error_message"],
                    clear_expires=True,
                    now=observed_at,
                )
                if requested_state == "failed":
                    run.ended_at = observed_at
                    for connection_row in (
                        orm.query(LiveSessionConnection)
                        .filter(
                            LiveSessionConnection.run_id == run_id,
                            LiveSessionConnection.released_at.is_(None),
                        )
                        .all()
                    ):
                        connection_row.state = "released"
                        connection_row.released_at = observed_at
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
                "exact_replay": False,
                "idempotency_conflict": False,
                "launch": _json_launch_result(result),
                "commit_seq": str(commit_seq),
            }
