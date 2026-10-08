"""CatalogStore: the one-time legacy corpus to storage-v2 conversion ledger."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_
from sqlalchemy import delete
from sqlalchemy import func
from sqlalchemy import insert
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import update

from zerg.catalogd.fact_reducer import _advance_commit_seq
from zerg.catalogd.fact_reducer import _current_commit_seq
from zerg.catalogd.models import LegacyMigrationRun
from zerg.catalogd.models import LegacyMigrationSession
from zerg.catalogd.models import ProjectorState
from zerg.catalogd.models import RawObject as LiveRawObject
from zerg.catalogd.models import RenderGeneration
from zerg.catalogd.models import RenderObject
from zerg.catalogd.models import StorageSession

# store.py imports this module after defining these, at its end.
from zerg.catalogd.store import KNOWN_PROJECTORS
from zerg.catalogd.store import _as_aware_utc
from zerg.catalogd.store import _legacy_migration_run_dto
from zerg.catalogd.store import _legacy_migration_session_dto
from zerg.catalogd.store import _legacy_migration_summary
from zerg.catalogd.store import _read_snapshot
from zerg.catalogd.store import _recompute_render_generation_projection
from zerg.catalogd.store import _refresh_legacy_migration_run
from zerg.catalogd.store import _write_transaction


class LegacyMigrationMixin:
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
                render_objects = RenderObject.__table__
                raw_objects = LiveRawObject.__table__
                invalid_source = connection.execute(
                    select(render_objects.c.object_id)
                    .select_from(
                        render_objects.outerjoin(
                            raw_objects,
                            raw_objects.c.envelope_id == render_objects.c.source_envelope_id,
                        )
                    )
                    .where(
                        render_objects.c.generation_id == generation_key,
                        render_objects.c.session_id == session_key,
                        or_(
                            render_objects.c.retired_at.is_not(None),
                            raw_objects.c.envelope_id.is_(None),
                            raw_objects.c.retired_at.is_not(None),
                        ),
                    )
                    .limit(1)
                ).first()
                if invalid_source is not None:
                    return {"render_generation_retired": True, "commit_seq": str(_current_commit_seq(connection))}
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
                for projector_name in KNOWN_PROJECTORS:
                    projector_row = (
                        connection.execute(
                            select(projector).where(
                                projector.c.projector == projector_name,
                                projector.c.session_id == session_key,
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if projector_row is None:
                        connection.execute(
                            insert(projector).values(
                                projector=projector_name,
                                session_id=session_key,
                                desired_revision=commit_seq,
                                desired_at=completed_at,
                                completed_revision=0,
                                status="idle",
                                failure_count=0,
                                commit_seq=commit_seq,
                                created_at=completed_at,
                                updated_at=completed_at,
                            )
                        )
                    else:
                        values = {
                            "desired_revision": commit_seq,
                            "desired_at": completed_at,
                            "commit_seq": commit_seq,
                            "updated_at": completed_at,
                        }
                        if projector_row["status"] == "quarantined":
                            values.update(
                                {
                                    "status": "idle",
                                    "failure_count": 0,
                                    "last_error_code": None,
                                    "last_error_message": None,
                                    "retry_at": None,
                                }
                            )
                        connection.execute(
                            update(projector)
                            .where(projector.c.projector == projector_name, projector.c.session_id == session_key)
                            .values(**values)
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
                for projector_name in KNOWN_PROJECTORS:
                    projector_row = connection.execute(
                        select(projectors).where(
                            projectors.c.projector == projector_name,
                            projectors.c.session_id == session_key,
                        )
                    ).first()
                    if projector_row is None:
                        connection.execute(
                            insert(projectors).values(
                                projector=projector_name,
                                session_id=session_key,
                                desired_revision=commit_seq,
                                desired_at=observed_at,
                                completed_revision=0,
                                status="idle",
                                failure_count=0,
                                commit_seq=commit_seq,
                                created_at=observed_at,
                                updated_at=observed_at,
                            )
                        )
                    else:
                        # Repair replaces the render generation the previous
                        # failure was reached against, so clearing the failure
                        # is part of republishing -- not a side effect someone
                        # else has to remember. This used to be implicit: the
                        # row was quarantined and a quarantine-keyed reset
                        # elsewhere happened to catch it. Permanent failures
                        # are now ordinary `failed` rows, so the site that
                        # invalidates the evidence states it directly.
                        connection.execute(
                            update(projectors)
                            .where(projectors.c.projector == projector_name, projectors.c.session_id == session_key)
                            .values(
                                desired_revision=commit_seq,
                                desired_at=observed_at,
                                status="idle",
                                failure_count=0,
                                last_error_code=None,
                                last_error_message=None,
                                retry_at=None,
                                commit_seq=commit_seq,
                                updated_at=observed_at,
                            )
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
