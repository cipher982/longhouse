"""CatalogStore: machine heartbeats, session runtime reports, and machine directory reads."""

from __future__ import annotations

import json
import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from sqlalchemy import and_
from sqlalchemy import func
from sqlalchemy import insert
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.orm import Session

from zerg.catalogd.fact_reducer import MAX_REDUCER_FACTS
from zerg.catalogd.fact_reducer import ReducerResult
from zerg.catalogd.fact_reducer import _advance_commit_seq
from zerg.catalogd.fact_reducer import _current_commit_seq
from zerg.catalogd.fact_reducer import reduce_fact_batch_setwise
from zerg.catalogd.models import SessionProviderFact
from zerg.catalogd.models import StorageSession

# store.py imports this module after defining these, at its end.
from zerg.catalogd.store import _MACHINE_HEALTH_QUERY_FIELDS
from zerg.catalogd.store import MACHINE_ENROLLMENT_LIMIT
from zerg.catalogd.store import MACHINE_HEALTH_LIMIT
from zerg.catalogd.store import _apply_shadow_parity
from zerg.catalogd.store import _apply_shadow_reducer
from zerg.catalogd.store import _as_aware_utc
from zerg.catalogd.store import _decode_json_object
from zerg.catalogd.store import _encode_datetime
from zerg.catalogd.store import _heartbeat_request_sha256
from zerg.catalogd.store import _machine_health_heartbeat_dto
from zerg.catalogd.store import _read_snapshot
from zerg.catalogd.store import _runtime_activity_facts
from zerg.catalogd.store import _runtime_delegation_facts
from zerg.catalogd.store import _settle_console_turns_from_runtime
from zerg.catalogd.store import _StageTimer
from zerg.catalogd.store import _write_transaction
from zerg.models.live_store import LiveDeviceToken
from zerg.models.live_store import LiveHeartbeatStamp
from zerg.models.live_store import LiveSession
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionThread
from zerg.models.live_store import LiveSessionThreadAlias
from zerg.models.live_store import LiveTimelineCard
from zerg.services.workspace_suggestion_projection import WORKSPACE_CANDIDATE_MAX_PAGES
from zerg.services.workspace_suggestion_projection import WORKSPACE_CANDIDATE_PAGE_SIZE
from zerg.services.workspace_suggestion_projection import WorkspaceSessionFacts
from zerg.services.workspace_suggestion_projection import rank_human_workspace_candidates


class MachinesMixin:
    def apply_machine_heartbeat(
        self,
        *,
        heartbeat: dict[str, Any],
        managed_leases: list[dict[str, Any]],
        managed_leases_present: bool,
        owner_id: int | None,
        machine_evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically persist and reconcile one hosted Machine Agent heartbeat.

        Stage-timed because knowing this call is slow was not enough to act on.
        catalogd's writer instrumentation showed it holding the single writer for
        ~900ms with zero queue wait -- the cost is inside here, not in waiting to
        get here -- but "inside here" spans a replay lookup, a retention delete,
        two shadow projections and a commit. The breakdown is what says which.
        """

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
        timer = _StageTimer("apply_machine_heartbeat")
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
            timer.mark("replay_lookup")
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

            timer.mark("previous_digest")
            cutoff = received_at - timedelta(days=30)
            connection.execute(stamp.delete().where(stamp.c.device_id == device_id, stamp.c.received_at < cutoff))
            timer.mark("retention_delete")
            stamp_id = connection.execute(
                insert(stamp).values(**heartbeat, request_sha256=request_sha256).returning(stamp.c.id)
            ).scalar_one()
            timer.mark("stamp_insert")

            lease_objects = [SimpleNamespace(**lease) for lease in managed_leases]
            touched: set[UUID] = set()
            orm = Session(bind=connection, join_transaction_mode="rollback_only", expire_on_commit=False)
            try:
                if lease_objects:
                    accepted_session_ids = upsert_live_control_leases(
                        orm,
                        lease_objects,
                        device_id=device_id,
                        received_at=received_at,
                    )
                    touched.update(accepted_session_ids)
                    touched.update(
                        upsert_live_sessions_from_managed_leases(
                            orm,
                            [lease for lease in lease_objects if lease.session_id in accepted_session_ids],
                            device_id=device_id,
                            owner_id=owner_id,
                            received_at=received_at,
                        )
                    )
                if managed_leases_present:
                    missing_session_ids = mark_missing_live_control_leases(
                        orm,
                        lease_objects,
                        device_id=device_id,
                        received_at=received_at,
                    )
                    touched.update(missing_session_ids)
                    touched.update(
                        mark_missing_live_sessions(
                            orm,
                            missing_session_ids,
                            device_id=device_id,
                            received_at=received_at,
                        )
                    )
                # The catalog connection owns the heartbeat transaction. Flush
                # the ORM unit without releasing a nested savepoint so lease
                # reconciliation, state projection, and the final stamp update
                # commit together.
                orm.flush()
            except BaseException:
                orm.rollback()
                raise
            finally:
                orm.close()
            timer.mark("lease_reconcile")

            commit_seq = _advance_commit_seq(connection, received_at)
            timer.mark("advance_commit_seq")
            shadow_reducer = _apply_shadow_reducer(
                connection,
                heartbeat=heartbeat,
                machine_evidence=machine_evidence,
                received_at=received_at,
                commit_seq=commit_seq,
            )
            timer.mark("shadow_reducer")
            shadow_parity, next_shadow_parity_delta_count = _apply_shadow_parity(
                connection,
                heartbeat=heartbeat,
                machine_evidence=machine_evidence,
                managed_leases_present=managed_leases_present,
                received_at=received_at,
                commit_seq=commit_seq,
                known_delta_count=self._shadow_parity_delta_count,
            )
            timer.mark("shadow_parity")
            result = {
                "previous_sessions_digest": previous_sessions_digest,
                "commit_seq": str(commit_seq),
                "touched_session_ids": sorted(str(session_id) for session_id in touched),
                "exact_replay": False,
                "shadow_reducer": shadow_reducer,
                "shadow_parity": shadow_parity,
            }
            connection.execute(
                update(stamp)
                .where(stamp.c.id == stamp_id)
                .values(catalog_result_json=json.dumps(result, sort_keys=True, separators=(",", ":")))
            )
            timer.mark("result_update")
        # The gap between the marks and the total is the transaction commit.
        timer.log_if_slow()
        self._shadow_parity_delta_count = next_shadow_parity_delta_count
        return result

    def apply_session_runtime(self, *, events: list[Any]) -> dict[str, Any]:
        """Atomically reduce one ordered runtime batch with bounded fact folds."""

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
                    # Binding aliases are an idempotent graph side effect, not a
                    # runtime-state mutation. A valid binding can leave the
                    # reducer snapshot unchanged and still must be persisted so
                    # the next Console turn can resume the provider thread.
                    if event.kind == "binding_signal":
                        provider_session_id = str((event.payload or {}).get("provider_session_id") or "").strip()
                        source_path = str((event.payload or {}).get("source_path") or "").strip()
                        if provider_session_id and event.session_id is not None:
                            catalog = orm.get(LiveSessionCatalog, str(event.session_id))
                            thread_id = str(event.thread_id or (catalog.primary_thread_id if catalog is not None else ""))
                            if not thread_id:
                                continue
                            # Query without thread_id: the routing index makes
                            # (provider, alias_value) unique per native id, so a
                            # row on another thread is a conflict, not an insert.
                            alias = (
                                orm.query(LiveSessionThreadAlias)
                                .filter(
                                    LiveSessionThreadAlias.provider == event.provider,
                                    LiveSessionThreadAlias.alias_kind == "provider_session_id",
                                    LiveSessionThreadAlias.alias_value == provider_session_id,
                                )
                                .order_by(LiveSessionThreadAlias.id.asc())
                                .first()
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
                            elif alias.thread_id == thread_id:
                                alias.last_seen_at = event.occurred_at or observed_at
                            else:
                                # Existing thread keeps the native id; crashing
                                # the runtime batch would retry forever.
                                logging.getLogger(__name__).warning(
                                    "Provider session binding conflict in live catalog: "
                                    "provider=%s provider_session_id=%s existing_thread_id=%s requested_thread_id=%s",
                                    event.provider,
                                    provider_session_id,
                                    alias.thread_id,
                                    thread_id,
                                )
                            if source_path:
                                source_alias = (
                                    orm.query(LiveSessionThreadAlias)
                                    .filter(
                                        LiveSessionThreadAlias.thread_id == thread_id,
                                        LiveSessionThreadAlias.provider == event.provider,
                                        LiveSessionThreadAlias.alias_kind == "source_path",
                                        LiveSessionThreadAlias.alias_value == source_path,
                                    )
                                    .first()
                                )
                                if source_alias is None:
                                    orm.add(
                                        LiveSessionThreadAlias(
                                            thread_id=thread_id,
                                            provider=event.provider,
                                            alias_kind="source_path",
                                            alias_value=source_path,
                                            first_seen_at=event.occurred_at or observed_at,
                                            last_seen_at=event.occurred_at or observed_at,
                                        )
                                    )
                                else:
                                    source_alias.last_seen_at = event.occurred_at or observed_at
                console_next_turns = _settle_console_turns_from_runtime(orm, events, observed_at=observed_at)
                orm.commit()
            except BaseException:
                orm.rollback()
                raise
            finally:
                orm.close()
            commit_seq = _advance_commit_seq(connection, observed_at)
            updated_runtime_keys = set(result.updated_runtime_keys)
            activity_facts = _runtime_activity_facts(
                connection,
                events=events,
                updated_runtime_keys=updated_runtime_keys,
            )
            delegation_facts = _runtime_delegation_facts(
                connection,
                events=events,
            )
            all_facts = [*activity_facts, *delegation_facts]
            reduced_parts = [
                reduce_fact_batch_setwise(
                    connection,
                    all_facts[start : start + MAX_REDUCER_FACTS],
                    received_at=observed_at,
                    commit_seq_override=commit_seq,
                )
                for start in range(0, len(all_facts), MAX_REDUCER_FACTS)
            ]
            reduced = ReducerResult(
                commit_seq=commit_seq,
                changed_heads=sum(part.changed_heads for part in reduced_parts),
                duplicates=sum(part.duplicates for part in reduced_parts),
                stale=sum(part.stale for part in reduced_parts),
                conflicts=sum(part.conflicts for part in reduced_parts),
            )
            return {
                **result.model_dump(mode="json"),
                "commit_seq": str(commit_seq),
                "console_next_turns": console_next_turns,
                "activity_facts": {
                    "changed_heads": reduced.changed_heads,
                    "duplicates": reduced.duplicates,
                    "stale": reduced.stale,
                    "conflicts": reduced.conflicts,
                },
                # Only the delegation-specific number: the reduction counters
                # above already describe this batch as a whole, and copying
                # them here would read as if they were delegation's own.
                "delegation_facts": {"promoted": len(delegation_facts)},
            }

    def set_device_automation(
        self,
        *,
        owner_id: int,
        device_id: str,
        automation: bool,
        observed_at: datetime,
    ) -> dict[str, Any]:
        """Mark one owner's machine credentials as automation, and backfill.

        docs/specs/automation-machine-credentials.md: the credential fills an
        absent ``launch_actor`` and never overwrites recorded provenance, at
        ingest and here alike. Turning the flag off clears only the flag; no
        history is guessed back to visible.
        """

        token_table = LiveDeviceToken.__table__
        storage = StorageSession.__table__
        catalog = LiveSessionCatalog.__table__
        card = LiveTimelineCard.__table__
        thread = LiveSessionThread.__table__
        with _write_transaction(self.engine) as connection:
            tokens = connection.execute(
                update(token_table)
                .where(
                    token_table.c.owner_id == owner_id,
                    token_table.c.device_id == device_id,
                    token_table.c.revoked_at.is_(None),
                )
                .values(automation=automation)
            ).rowcount
            if not tokens:
                return {
                    "found": False,
                    "tokens_updated": 0,
                    "reclassified": [],
                    "sessions": [],
                    "commit_seq": str(_current_commit_seq(connection)),
                }
            if not automation:
                return {
                    "found": True,
                    "tokens_updated": int(tokens),
                    "reclassified": [],
                    "sessions": [],
                    "commit_seq": str(_advance_commit_seq(connection, observed_at)),
                }
            # Candidates: this owner's sessions from this machine with no launch
            # provenance, not Console, in storage and (when present) the live
            # catalog alike. A live row that recorded provenance wins.
            live_declared = select(catalog.c.session_id).where(
                or_(
                    catalog.c.launch_actor.isnot(None),
                    func.coalesce(catalog.c.origin_kind, "") == "console",
                )
            )
            session_ids = [
                str(value)
                for value in connection.execute(
                    select(storage.c.session_id).where(
                        storage.c.machine_id == device_id,
                        storage.c.owner_id == str(owner_id),
                        storage.c.launch_actor.is_(None),
                        func.coalesce(storage.c.origin_kind, "") != "console",
                        storage.c.session_id.notin_(live_declared),
                    )
                ).scalars()
            ]
            # Sessions known only to the live catalog (no archived row yet)
            # belong to the same machine history.
            live_only = LiveSession.__table__
            session_ids += [
                str(value)
                for value in connection.execute(
                    select(catalog.c.session_id).where(
                        catalog.c.device_id == device_id,
                        catalog.c.launch_actor.is_(None),
                        func.coalesce(catalog.c.origin_kind, "") != "console",
                        catalog.c.session_id.in_(select(live_only.c.session_id).where(live_only.c.owner_id == str(owner_id))),
                        catalog.c.session_id.notin_(select(storage.c.session_id)),
                    )
                ).scalars()
            ]
            commit_seq = _advance_commit_seq(connection, observed_at)
            for start in range(0, len(session_ids), 500):
                chunk = session_ids[start : start + 500]
                connection.execute(
                    update(storage)
                    .where(storage.c.session_id.in_(chunk))
                    .values(launch_actor="automation", hidden_from_default_timeline=1, commit_seq=commit_seq, updated_at=observed_at)
                )
                for table in (catalog, card):
                    connection.execute(
                        update(table)
                        .where(table.c.session_id.in_(chunk), table.c.launch_actor.is_(None))
                        .values(launch_actor="automation", hidden_from_default_timeline=1, updated_at=observed_at)
                    )
                connection.execute(
                    update(thread)
                    .where(thread.c.session_id.in_(chunk), thread.c.is_primary == 1)
                    .values(hidden_from_default_timeline=1, updated_at=observed_at)
                )
            # Every automation row from this machine, not only this call's
            # fills, so a rerun re-mirrors searchd after a partial failure.
            mirror = (
                connection.execute(
                    select(storage.c.session_id, storage.c.user_hidden_from_timeline, storage.c.user_state).where(
                        storage.c.machine_id == device_id,
                        storage.c.owner_id == str(owner_id),
                        storage.c.launch_actor == "automation",
                    )
                )
                .mappings()
                .all()
            )
        return {
            "found": True,
            "tokens_updated": int(tokens),
            "reclassified": session_ids,
            "sessions": [
                {
                    "session_id": str(row["session_id"]),
                    "user_hidden_from_timeline": bool(row["user_hidden_from_timeline"]),
                    "user_state": str(row["user_state"] or "active"),
                }
                for row in mirror
            ],
            "commit_seq": str(commit_seq),
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

    def list_machine_heartbeats(
        self,
        *,
        owner_id: int,
        device_id: str | None,
        recent_after: datetime | None,
        limit: int,
    ) -> dict[str, Any]:
        observed_at = datetime.now(UTC)
        token = LiveDeviceToken.__table__
        heartbeat = LiveHeartbeatStamp.__table__
        # Resolve latest ids per authorized device rather than grouping the
        # entire heartbeat history.  The device index can narrow each
        # correlated max(id) lookup before the final bounded projection.
        authorized_devices = select(token.c.device_id).where(
            token.c.owner_id == owner_id,
            token.c.revoked_at.is_(None),
        )
        if device_id is not None:
            authorized_devices = authorized_devices.where(token.c.device_id == device_id)
        authorized_devices = authorized_devices.distinct().subquery("authorized_machine_devices")
        latest_heartbeat = heartbeat.alias("latest_machine_heartbeat")
        latest_heartbeat_id = (
            select(func.max(latest_heartbeat.c.id))
            .where(latest_heartbeat.c.device_id == authorized_devices.c.device_id)
            .correlate(authorized_devices)
        )
        if recent_after is not None:
            latest_heartbeat_id = latest_heartbeat_id.where(latest_heartbeat.c.received_at >= recent_after)
        latest_heartbeat_id = latest_heartbeat_id.scalar_subquery()
        with _read_snapshot(self.engine) as connection:
            rows = connection.execute(
                select(*(heartbeat.c[field] for field in _MACHINE_HEALTH_QUERY_FIELDS))
                .select_from(heartbeat.join(authorized_devices, heartbeat.c.device_id == authorized_devices.c.device_id))
                .where(heartbeat.c.id == latest_heartbeat_id)
                .order_by(heartbeat.c.received_at.desc(), heartbeat.c.device_id.asc())
                .limit(min(limit, MACHINE_HEALTH_LIMIT))
            ).mappings()
            heartbeats = [_machine_health_heartbeat_dto(row) for row in rows]
            return {
                "commit_seq": str(_current_commit_seq(connection)),
                "observed_at": observed_at.isoformat(),
                "heartbeats": heartbeats,
            }

    def list_machine_models(
        self,
        *,
        owner_id: int,
        device_id: str,
        provider: str,
        limit: int,
        days_back: int,
    ) -> dict[str, Any]:
        """Return recent provider-reported models for one enrolled machine."""

        observed_at = datetime.now(UTC)
        since = observed_at - timedelta(days=days_back)
        token = LiveDeviceToken.__table__
        catalog = LiveSessionCatalog.__table__
        facts = SessionProviderFact.__table__
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
            models: list[dict[str, Any]] = []
            if enrolled is not None:
                session_rows = connection.execute(
                    select(catalog.c.session_id)
                    .where(
                        catalog.c.device_id == device_id,
                        catalog.c.provider == provider,
                        or_(
                            catalog.c.environment.is_(None),
                            func.lower(catalog.c.environment).notin_(("test", "e2e")),
                        ),
                        func.coalesce(catalog.c.last_activity_at, catalog.c.started_at) >= since,
                    )
                    .order_by(
                        func.coalesce(catalog.c.last_activity_at, catalog.c.started_at).desc(),
                        catalog.c.session_id.desc(),
                    )
                    .limit(min(max(limit * 4, limit), 200))
                ).all()
                session_ids = [str(row.session_id) for row in session_rows]
                if session_ids:
                    seen: set[str] = set()
                    fact_rows = connection.execute(
                        select(
                            facts.c.at,
                            facts.c.payload_json,
                        )
                        .where(
                            facts.c.session_id.in_(session_ids),
                            facts.c.kind == "turn.usage",
                        )
                        .order_by(facts.c.at.desc(), facts.c.source_position.desc(), facts.c.id.desc())
                    ).all()
                    for row in fact_rows:
                        try:
                            payload = json.loads(row.payload_json)
                        except (TypeError, ValueError):
                            continue
                        model = payload.get("model") if isinstance(payload, dict) else None
                        if not isinstance(model, str) or not model.strip():
                            continue
                        key = model.casefold()
                        if key in seen:
                            continue
                        seen.add(key)
                        models.append(
                            {
                                "model": model,
                                "last_used_at": _encode_datetime(_as_aware_utc(row.at)),
                            }
                        )
                        if len(models) >= limit:
                            break
            return {
                "commit_seq": str(_current_commit_seq(connection)),
                "observed_at": observed_at.isoformat(),
                "device_id": device_id,
                "provider": provider,
                "days_back": days_back,
                "models": models,
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
        thread = LiveSessionThread.__table__
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
            candidates: list[WorkspaceSessionFacts] = []
            limit_exceeded = False
            if enrolled is not None:
                stmt = (
                    select(
                        catalog.c.device_id,
                        catalog.c.provider,
                        catalog.c.environment,
                        catalog.c.project,
                        catalog.c.cwd,
                        catalog.c.git_repo,
                        catalog.c.git_branch,
                        catalog.c.last_activity_at,
                        catalog.c.started_at,
                        catalog.c.origin_kind,
                        catalog.c.hidden_from_default_timeline,
                        catalog.c.user_hidden_from_timeline,
                        catalog.c.launch_actor,
                        thread.c.branch_kind,
                    )
                    .select_from(
                        catalog.outerjoin(
                            thread,
                            and_(thread.c.session_id == catalog.c.session_id, thread.c.is_primary == 1),
                        )
                    )
                    .where(
                        catalog.c.device_id == device_id,
                        func.coalesce(catalog.c.last_activity_at, catalog.c.started_at) >= since,
                    )
                    .order_by(
                        func.coalesce(catalog.c.last_activity_at, catalog.c.started_at).desc(),
                        catalog.c.session_id.desc(),
                    )
                )
                for page in range(WORKSPACE_CANDIDATE_MAX_PAGES):
                    rows = connection.execute(
                        stmt.offset(page * WORKSPACE_CANDIDATE_PAGE_SIZE).limit(WORKSPACE_CANDIDATE_PAGE_SIZE + 1)
                    ).all()
                    has_more = len(rows) > WORKSPACE_CANDIDATE_PAGE_SIZE
                    page_facts = [
                        WorkspaceSessionFacts(
                            device_id=row.device_id,
                            provider=row.provider,
                            environment=row.environment,
                            project=row.project,
                            cwd=row.cwd,
                            git_repo=row.git_repo,
                            git_branch=row.git_branch,
                            last_activity_at=_as_aware_utc(row.last_activity_at),
                            started_at=_as_aware_utc(row.started_at),
                            origin_kind=row.origin_kind,
                            hidden_from_default_timeline=bool(row.hidden_from_default_timeline),
                            user_hidden_from_timeline=bool(row.user_hidden_from_timeline),
                            launch_actor=row.launch_actor,
                            is_sidechain=row.branch_kind == "subagent",
                        )
                        for row in rows[:WORKSPACE_CANDIDATE_PAGE_SIZE]
                    ]
                    candidates.extend(page_facts)
                    limit_exceeded = has_more
                    if not has_more:
                        break
            workspaces = rank_human_workspace_candidates(
                candidates,
                device_id=device_id,
                now=observed_at,
                days_back=days_back,
                limit=limit,
            )
            return {
                "commit_seq": str(_current_commit_seq(connection)),
                "observed_at": observed_at.isoformat(),
                "device_id": device_id,
                "workspaces": [entry.to_response() for entry in workspaces],
                "limit_exceeded": limit_exceeded,
            }
