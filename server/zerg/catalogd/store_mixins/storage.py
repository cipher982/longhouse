"""CatalogStore: storage-v2 session reads, deletion, relinking, render generations, titles and health."""

from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
from uuid import UUID
from uuid import uuid4

from sqlalchemy import and_
from sqlalchemy import case
from sqlalchemy import func
from sqlalchemy import insert
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import tuple_
from sqlalchemy import update

from zerg.catalogd.fact_reducer import _advance_commit_seq
from zerg.catalogd.fact_reducer import _current_commit_seq
from zerg.catalogd.models import MediaObject
from zerg.catalogd.models import ProjectorState
from zerg.catalogd.models import RawObject as LiveRawObject
from zerg.catalogd.models import RenderGeneration
from zerg.catalogd.models import RenderObject
from zerg.catalogd.models import RuntimeDependencyState
from zerg.catalogd.models import SessionMediaRef
from zerg.catalogd.models import SessionTombstone as LiveSessionTombstone
from zerg.catalogd.models import StorageSession
from zerg.catalogd.schema import storage_telemetry_counters

# store.py imports this module after defining these, at its end.
from zerg.catalogd.store import ACTIVE_PROJECTORS
from zerg.catalogd.store import KNOWN_PROJECTORS
from zerg.catalogd.store import MAX_TITLE_ATTEMPTS
from zerg.catalogd.store import RETRYABLE_TITLE_ROW_ERRORS
from zerg.catalogd.store import TITLE_BACKLOG_DEGRADED_AFTER
from zerg.catalogd.store import TITLE_DEPENDENCY_AVAILABILITY_PROBE_DELAY
from zerg.catalogd.store import TITLE_DEPENDENCY_LEGACY_REPAIR_VERSION
from zerg.catalogd.store import TITLE_DEPENDENCY_PROBE_DELAY
from zerg.catalogd.store import TITLE_DEPENDENCY_USE_CASE
from zerg.catalogd.store import TITLE_ROW_TRANSIENT_RETRY_DELAY
from zerg.catalogd.store import _as_aware_utc
from zerg.catalogd.store import _delegation_fact_rows
from zerg.catalogd.store import _delete_bounded_live_session_state
from zerg.catalogd.store import _encode_datetime
from zerg.catalogd.store import _input_receipt_rows
from zerg.catalogd.store import _legacy_title_dependency_failure_clause
from zerg.catalogd.store import _raw_object_manifest_dto
from zerg.catalogd.store import _read_snapshot
from zerg.catalogd.store import _recompute_render_generation_projection
from zerg.catalogd.store import _render_generation_dto
from zerg.catalogd.store import _render_object_manifest_dto
from zerg.catalogd.store import _retryable_title_row_failure_clause
from zerg.catalogd.store import _runtime_dependency_dto
from zerg.catalogd.store import _session_read_media_refs
from zerg.catalogd.store import _session_read_provider_facts
from zerg.catalogd.store import _storage_session_dto
from zerg.catalogd.store import _storage_title_candidate_clause
from zerg.catalogd.store import _storage_title_obligation_clause
from zerg.catalogd.store import _title_anchor_replaceable
from zerg.catalogd.store import _title_anchor_replaceable_clause
from zerg.catalogd.store import _title_auth_failure_clause
from zerg.catalogd.store import _write_transaction
from zerg.models.live_store import LiveDeviceToken
from zerg.models.live_store import LiveHeartbeatStamp
from zerg.models.live_store import LiveSession
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionThread
from zerg.models.live_store import LiveTimelineCard
from zerg.services.internal_sessions import factory_title_assurance_session_clause
from zerg.services.session_title import is_path_like_title
from zerg.services.session_title import is_resume_seed_marker
from zerg.services.session_visibility_policy import effective_system_hidden_clause
from zerg.services.session_visibility_policy import primary_worker_only_clause
from zerg.services.session_visibility_policy import title_origin_eligible_clause


class StorageMixin:
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
            for projector_name in KNOWN_PROJECTORS:
                search_state = (
                    connection.execute(
                        select(projector_state).where(
                            projector_state.c.projector == projector_name, projector_state.c.session_id == session_key
                        )
                    )
                    .mappings()
                    .first()
                )
                if search_state is None:
                    connection.execute(
                        insert(projector_state).values(
                            projector=projector_name,
                            session_id=session_key,
                            desired_revision=commit_seq,
                            desired_at=deleted_at,
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
                            projector_state.c.projector == projector_name,
                            projector_state.c.session_id == session_key,
                        )
                        .values(
                            desired_revision=commit_seq,
                            desired_at=deleted_at,
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

    def reconcile_relinked_legacy_session(
        self,
        *,
        session_id: UUID,
        observed_at: datetime,
    ) -> dict[str, Any]:
        """Retire one proven duplicate left behind by legacy/source relinking."""

        session_key = str(session_id)
        sessions = StorageSession.__table__
        raw = LiveRawObject.__table__
        render_objects = RenderObject.__table__
        tombstones = LiveSessionTombstone.__table__
        with _write_transaction(self.engine) as connection:
            session = connection.execute(select(sessions).where(sessions.c.session_id == session_key)).mappings().first()
            if session is None:
                return {"session_missing": True, "commit_seq": str(_current_commit_seq(connection))}
            if connection.execute(select(tombstones.c.session_id).where(tombstones.c.session_id == session_key)).first() is not None:
                return {"proof_conflict": "session_tombstoned", "commit_seq": str(_current_commit_seq(connection))}
            generation_id = session["current_render_generation"]
            if generation_id is None:
                return {"proof_conflict": "current_generation_missing", "commit_seq": str(_current_commit_seq(connection))}

            current_objects = list(
                connection.execute(
                    select(
                        render_objects.c.object_id,
                        render_objects.c.retired_at.label("render_retired_at"),
                        raw.c.envelope_id,
                        raw.c.tenant_id,
                        raw.c.machine_id,
                        raw.c.provider,
                        raw.c.opaque_source_id,
                        raw.c.range_kind,
                        raw.c.range_start,
                        raw.c.range_end,
                        raw.c.record_hashes_hash,
                        raw.c.session_id.label("raw_session_id"),
                        raw.c.retired_at.label("raw_retired_at"),
                    )
                    .select_from(render_objects.outerjoin(raw, raw.c.envelope_id == render_objects.c.source_envelope_id))
                    .where(
                        render_objects.c.session_id == session_key,
                        render_objects.c.generation_id == generation_id,
                    )
                    .order_by(render_objects.c.object_id)
                ).mappings()
            )
            if not current_objects:
                return {"proof_conflict": "current_generation_empty", "commit_seq": str(_current_commit_seq(connection))}
            if any(
                row["render_retired_at"] is None or row["envelope_id"] is None or row["raw_retired_at"] is None for row in current_objects
            ):
                return {"proof_conflict": "current_generation_not_retired", "commit_seq": str(_current_commit_seq(connection))}
            replacement_proofs = []
            for row in current_objects:
                replacement = connection.execute(
                    select(raw.c.envelope_id, raw.c.session_id).where(
                        raw.c.tenant_id == row["tenant_id"],
                        raw.c.machine_id == row["machine_id"],
                        raw.c.provider == row["provider"],
                        raw.c.opaque_source_id == row["opaque_source_id"],
                        raw.c.range_kind == row["range_kind"],
                        raw.c.range_start == row["range_start"],
                        raw.c.range_end == row["range_end"],
                        raw.c.record_hashes_hash == row["record_hashes_hash"],
                        raw.c.session_id != row["raw_session_id"],
                        raw.c.retired_at.is_(None),
                    )
                ).first()
                if replacement is None:
                    return {"proof_conflict": "exact_active_replacement_missing", "commit_seq": str(_current_commit_seq(connection))}
                replacement_proofs.append(
                    {
                        "retired_envelope_id": str(row["envelope_id"]),
                        "replacement_envelope_id": str(replacement.envelope_id),
                        "replacement_session_id": str(replacement.session_id),
                    }
                )

            active_owned = list(
                connection.execute(
                    select(raw.c.envelope_id, raw.c.provenance_kind).where(
                        raw.c.session_id == session_key,
                        raw.c.retired_at.is_(None),
                    )
                ).mappings()
            )
            if not active_owned:
                return {"proof_conflict": "active_legacy_source_missing", "commit_seq": str(_current_commit_seq(connection))}
            if any(not str(row["provenance_kind"]).startswith("legacy_") for row in active_owned):
                return {"proof_conflict": "active_nonlegacy_source_present", "commit_seq": str(_current_commit_seq(connection))}

            if session["render_state"] == "retired":
                return {
                    "changed": False,
                    "already_retired": True,
                    "session_id": session_key,
                    "preserved_raw_objects": len(active_owned),
                    "replacement_proofs": replacement_proofs,
                    "commit_seq": str(session["commit_seq"]),
                }

            commit_seq, retired_render, retired_generations = _retire_session_render(connection, session_key, observed_at)
            return {
                "changed": True,
                "session_id": session_key,
                "preserved_raw_objects": len(active_owned),
                "retired_render_objects": int(retired_render or 0),
                "retired_render_generations": int(retired_generations or 0),
                "replacement_proofs": replacement_proofs,
                "commit_seq": str(commit_seq),
            }

    def retire_legacy_twin_session(
        self,
        *,
        session_id: UUID,
        twin_session_id: UUID,
        window_seconds: int,
        observed_at: datetime,
    ) -> dict[str, Any]:
        """Retire a legacy-converted session whose conversation a native session already holds.

        The caller supplies the content evidence (``storage-migrate reconcile-legacy-twins``);
        this re-checks owner, provider, machine, identity or start window and
        provenance under the writer lock. Nothing is deleted:
        raw objects stay active and the render is retired, so the copy leaves the
        timeline and search but stays on disk.
        """

        session_key = str(session_id)
        twin_key = str(twin_session_id)
        sessions = StorageSession.__table__
        raw = LiveRawObject.__table__
        tombstones = LiveSessionTombstone.__table__
        with _write_transaction(self.engine) as connection:

            def conflict(code: str) -> dict[str, Any]:
                return {"proof_conflict": code, "commit_seq": str(_current_commit_seq(connection))}

            if session_key == twin_key:
                return conflict("twin_is_session")
            session = connection.execute(select(sessions).where(sessions.c.session_id == session_key)).mappings().first()
            twin = connection.execute(select(sessions).where(sessions.c.session_id == twin_key)).mappings().first()
            if session is None:
                return {"session_missing": True, "commit_seq": str(_current_commit_seq(connection))}
            if twin is None:
                return conflict("twin_missing")
            if connection.execute(select(tombstones.c.session_id).where(tombstones.c.session_id.in_([session_key, twin_key]))).first():
                return conflict("session_tombstoned")
            if (
                twin["owner_id"] != session["owner_id"]
                or twin["provider"] != session["provider"]
                or twin["machine_id"] != session["machine_id"]
            ):
                return conflict("twin_identity_mismatch")
            if session["provider_session_id"] and twin["provider_session_id"]:
                if twin["provider_session_id"] != session["provider_session_id"]:
                    return conflict("twin_identity_mismatch")
            elif abs((_as_aware_utc(twin["started_at"]) - _as_aware_utc(session["started_at"])).total_seconds()) > window_seconds:
                return conflict("twin_outside_window")
            if twin["render_state"] != "ready":
                return conflict("twin_not_ready")

            def provenance(key: str) -> list[str]:
                return [
                    str(row[0])
                    for row in connection.execute(select(raw.c.provenance_kind).where(raw.c.session_id == key, raw.c.retired_at.is_(None)))
                ]

            owned = provenance(session_key)
            if not owned:
                return conflict("active_legacy_source_missing")
            if any(not kind.startswith("legacy_") for kind in owned):
                return conflict("active_nonlegacy_source_present")
            twin_owned = provenance(twin_key)
            if "native" not in twin_owned or any(kind.startswith("legacy_") for kind in twin_owned):
                return conflict("twin_not_native")
            if session["render_state"] == "retired":
                return {
                    "changed": False,
                    "already_retired": True,
                    "session_id": session_key,
                    "twin_session_id": twin_key,
                    "preserved_raw_objects": len(owned),
                    "commit_seq": str(session["commit_seq"]),
                }
            commit_seq, retired_render, retired_generations = _retire_session_render(connection, session_key, observed_at)
            return {
                "changed": True,
                "session_id": session_key,
                "twin_session_id": twin_key,
                "preserved_raw_objects": len(owned),
                "retired_render_objects": retired_render,
                "retired_render_generations": retired_generations,
                "commit_seq": str(commit_seq),
            }

    def restore_storage_render_generation(
        self,
        *,
        session_id: UUID,
        generation_id: UUID,
        observed_at: datetime,
    ) -> dict[str, Any]:
        """Restore one complete generation when the selected generation lost its raw source."""

        session_key = str(session_id)
        generation_key = str(generation_id)
        sessions = StorageSession.__table__
        raw = LiveRawObject.__table__
        render_objects = RenderObject.__table__
        generations = RenderGeneration.__table__
        projector_state = ProjectorState.__table__
        tombstones = LiveSessionTombstone.__table__
        with _write_transaction(self.engine) as connection:
            session = connection.execute(select(sessions).where(sessions.c.session_id == session_key)).mappings().first()
            if session is None:
                return {"session_missing": True, "commit_seq": str(_current_commit_seq(connection))}
            if connection.execute(select(tombstones.c.session_id).where(tombstones.c.session_id == session_key)).first() is not None:
                return {"proof_conflict": "session_tombstoned", "commit_seq": str(_current_commit_seq(connection))}
            if session["current_render_generation"] == generation_key and session["render_state"] == "ready":
                return {
                    "changed": False,
                    "already_current": True,
                    "session_id": session_key,
                    "generation_id": generation_key,
                    "commit_seq": str(session["commit_seq"]),
                }
            target = (
                connection.execute(
                    select(generations).where(
                        generations.c.generation_id == generation_key,
                        generations.c.session_id == session_key,
                    )
                )
                .mappings()
                .first()
            )
            if target is None:
                return {"generation_missing": True, "commit_seq": str(_current_commit_seq(connection))}
            if target["state"] != "superseded":
                return {"proof_conflict": "target_generation_not_superseded", "commit_seq": str(_current_commit_seq(connection))}
            current_generation = session["current_render_generation"]
            if current_generation is None:
                return {"proof_conflict": "current_generation_missing", "commit_seq": str(_current_commit_seq(connection))}
            current_rows = list(
                connection.execute(
                    select(render_objects.c.retired_at, raw.c.envelope_id, raw.c.retired_at.label("raw_retired_at"))
                    .select_from(render_objects.outerjoin(raw, raw.c.envelope_id == render_objects.c.source_envelope_id))
                    .where(
                        render_objects.c.session_id == session_key,
                        render_objects.c.generation_id == current_generation,
                    )
                ).mappings()
            )
            if not current_rows or any(
                row["retired_at"] is None or row["envelope_id"] is None or row["raw_retired_at"] is None for row in current_rows
            ):
                return {"proof_conflict": "current_generation_still_durable", "commit_seq": str(_current_commit_seq(connection))}
            target_rows = list(
                connection.execute(
                    select(
                        render_objects.c.object_id,
                        render_objects.c.event_count,
                        render_objects.c.retired_at,
                        raw.c.envelope_id,
                        raw.c.session_id.label("raw_session_id"),
                        raw.c.retired_at.label("raw_retired_at"),
                    )
                    .select_from(render_objects.outerjoin(raw, raw.c.envelope_id == render_objects.c.source_envelope_id))
                    .where(
                        render_objects.c.session_id == session_key,
                        render_objects.c.generation_id == generation_key,
                    )
                    .order_by(render_objects.c.object_id)
                ).mappings()
            )
            if not target_rows:
                return {"proof_conflict": "target_generation_empty", "commit_seq": str(_current_commit_seq(connection))}
            if any(
                row["retired_at"] is not None
                or row["envelope_id"] is None
                or row["raw_retired_at"] is not None
                or row["raw_session_id"] != session_key
                for row in target_rows
            ):
                return {"proof_conflict": "target_generation_not_durable", "commit_seq": str(_current_commit_seq(connection))}
            if len(target_rows) != int(target["object_count"]) or sum(int(row["event_count"]) for row in target_rows) != int(
                target["event_count"]
            ):
                return {"proof_conflict": "target_generation_projection_mismatch", "commit_seq": str(_current_commit_seq(connection))}

            commit_seq = _advance_commit_seq(connection, observed_at)
            connection.execute(
                update(generations)
                .where(
                    generations.c.session_id == session_key,
                    generations.c.generation_id != generation_key,
                    generations.c.state == "current",
                )
                .values(state="superseded", superseded_at=observed_at, commit_seq=commit_seq, updated_at=observed_at)
            )
            _recompute_render_generation_projection(
                connection,
                session_id=session_key,
                generation_id=generation_key,
                commit_seq=commit_seq,
                commit_time=observed_at,
            )
            connection.execute(
                update(sessions)
                .where(sessions.c.session_id == session_key)
                .values(render_state="ready", commit_seq=commit_seq, updated_at=observed_at)
            )
            for projector_name in KNOWN_PROJECTORS:
                state = (
                    connection.execute(
                        select(projector_state).where(
                            projector_state.c.projector == projector_name,
                            projector_state.c.session_id == session_key,
                        )
                    )
                    .mappings()
                    .first()
                )
                values = {
                    "desired_revision": commit_seq,
                    "desired_at": observed_at,
                    "claimed_revision": None,
                    "claim_token": None,
                    "worker_id": None,
                    "claim_expires_at": None,
                    "status": "idle",
                    "failure_count": 0,
                    "last_error_code": None,
                    "last_error_message": None,
                    "retry_at": None,
                    "commit_seq": commit_seq,
                    "updated_at": observed_at,
                }
                if state is None:
                    connection.execute(
                        insert(projector_state).values(
                            projector=projector_name,
                            session_id=session_key,
                            completed_revision=0,
                            created_at=observed_at,
                            **values,
                        )
                    )
                else:
                    connection.execute(
                        update(projector_state)
                        .where(
                            projector_state.c.projector == projector_name,
                            projector_state.c.session_id == session_key,
                        )
                        .values(**values)
                    )
            return {
                "changed": True,
                "session_id": session_key,
                "generation_id": generation_key,
                "object_count": len(target_rows),
                "event_count": sum(int(row["event_count"]) for row in target_rows),
                "source_envelope_ids": [str(row["envelope_id"]) for row in target_rows],
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
            found = row is not None and deleted is None
            return {
                "found": found,
                "deleted": deleted is not None,
                "deletion_revision": str(deleted) if deleted is not None else None,
                "session": _storage_session_dto(row) if found else None,
                # Provenance rides the coalesced, lane-exempt session read: the
                # workspace needs both on every page and a busy catalog's read
                # lane rejected them as separate calls.
                "provider_facts": _session_read_provider_facts(connection, session_id=session_key) if found else [],
                "delegation_observations": _delegation_fact_rows(connection, session_id=session_key) if found else [],
                "input_receipts": _input_receipt_rows(connection, session_id=session_key) if found else [],
                # Same rule for media: the workspace joins these onto events by
                # (envelope_id, source_position), so they must arrive with the page.
                "media_refs": _session_read_media_refs(connection, session_id=session_key) if found else [],
                "commit_seq": str(_current_commit_seq(connection)),
                "observed_at": observed_at.isoformat(),
            }

    def lookup_storage_canary_session(self, *, observed_at: datetime, max_age_seconds: int) -> dict[str, Any]:
        """Return the freshest live canary StorageSession joined to LiveSession liveness.

        Durable authority comes from StorageSession (provider canary/cnary). Runtime
        freshness uses the server-observed LiveSession.updated_at timestamp; the
        provider-stamped last_seen_at remains diagnostic metadata. Missing/ended
        runtime rows are excluded so abandoned stress sessions do not win.
        """

        storage = StorageSession.__table__
        live = LiveSession.__table__
        tombstone = LiveSessionTombstone.__table__
        cutoff = observed_at - timedelta(seconds=max_age_seconds)
        with _read_snapshot(self.engine) as connection:
            row = (
                connection.execute(
                    select(
                        storage.c.session_id,
                        storage.c.provider,
                        storage.c.project,
                        storage.c.machine_id,
                        storage.c.environment,
                        storage.c.last_activity_at,
                        storage.c.origin_kind,
                        storage.c.hidden_from_default_timeline,
                        live.c.last_seen_at,
                        live.c.updated_at.label("runtime_updated_at"),
                        live.c.state,
                    )
                    .select_from(
                        storage.join(live, live.c.session_id == storage.c.session_id).outerjoin(
                            tombstone, tombstone.c.session_id == storage.c.session_id
                        )
                    )
                    .where(
                        tombstone.c.session_id.is_(None),
                        storage.c.provider.in_(("canary", "cnary")),
                        live.c.provider.in_(("canary", "cnary")),
                        live.c.state.notin_(("missing", "ended")),
                        live.c.updated_at >= cutoff,
                    )
                    .order_by(live.c.updated_at.desc(), live.c.last_seen_at.desc(), live.c.session_id.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
            commit_seq = str(_current_commit_seq(connection))
            if row is None:
                return {
                    "session_id": None,
                    "provider": None,
                    "project": None,
                    "machine_id": None,
                    "environment": None,
                    "origin_kind": None,
                    "hidden_from_default_timeline": None,
                    "last_seen_at": None,
                    "runtime_updated_at": None,
                    "last_activity_at": None,
                    "runtime_state": None,
                    "commit_seq": commit_seq,
                    "observed_at": _encode_datetime(observed_at),
                    "max_age_seconds": max_age_seconds,
                }
            return {
                "session_id": str(row["session_id"]),
                "provider": str(row["provider"]),
                "project": row["project"],
                "machine_id": str(row["machine_id"]),
                "environment": str(row["environment"]),
                "origin_kind": row["origin_kind"],
                "hidden_from_default_timeline": bool(row["hidden_from_default_timeline"]),
                "last_seen_at": _encode_datetime(row["last_seen_at"]),
                "runtime_updated_at": _encode_datetime(row["runtime_updated_at"]),
                "last_activity_at": _encode_datetime(row["last_activity_at"]),
                "runtime_state": str(row["state"]),
                "commit_seq": commit_seq,
                "observed_at": _encode_datetime(observed_at),
                "max_age_seconds": max_age_seconds,
            }

    def list_storage_title_candidates(self, *, limit: int) -> dict[str, Any]:
        table = StorageSession.__table__
        observed_at = datetime.now(UTC)
        with _read_snapshot(self.engine) as connection:
            rows = (
                connection.execute(
                    select(table)
                    .where(_storage_title_candidate_clause(table, observed_at=observed_at))
                    # Durable debt drains oldest-first. New activity must not
                    # continually push an older eligible obligation beyond a
                    # bounded worker pool.
                    .order_by(
                        func.coalesce(table.c.title_retry_at, table.c.created_at).asc(),
                        table.c.last_activity_at.asc(),
                        table.c.session_id,
                    )
                    .limit(limit)
                )
                .mappings()
                .all()
            )
        return {
            "sessions": [
                {
                    "session_id": str(row["session_id"]),
                    "first_user_message": row["first_user_message_preview"],
                    "provider": str(row["provider"]),
                    "project": row["project"],
                    "git_branch": row["git_branch"],
                    "machine_id": row["machine_id"],
                    "attempt_count": int(row["title_attempt_count"] or 0),
                    "canonical_title_eligible": True,
                }
                for row in rows
                if str(row["first_user_message_preview"] or "").strip() and not is_resume_seed_marker(row["first_user_message_preview"])
            ],
            "observed_at": observed_at.isoformat(),
        }

    def reconcile_storage_title_dependency(
        self,
        *,
        provider: str,
        model: str,
        credential_binding: str,
        credential_generation: str,
        observed_at: datetime,
    ) -> dict[str, Any]:
        """Create dependency state and adopt pre-circuit shared-provider debt."""

        dependency = RuntimeDependencyState.__table__
        sessions = StorageSession.__table__
        catalog = LiveSessionCatalog.__table__
        identity = (
            dependency.c.use_case == TITLE_DEPENDENCY_USE_CASE,
            dependency.c.provider == provider,
            dependency.c.model == model,
            dependency.c.credential_binding == credential_binding,
        )
        untitled = or_(sessions.c.anchor_title.is_(None), sessions.c.anchor_title == "")
        legacy_clause = and_(untitled, _legacy_title_dependency_failure_clause(sessions))
        with _write_transaction(self.engine) as connection:
            row = connection.execute(select(dependency).where(*identity)).mappings().first()
            needs_legacy_repair = row is None or int(row["legacy_repair_version"] or 0) < TITLE_DEPENDENCY_LEGACY_REPAIR_VERSION
            legacy_count = (
                int(connection.execute(select(func.count()).select_from(sessions).where(legacy_clause)).scalar_one())
                if needs_legacy_repair
                else 0
            )
            has_legacy_auth = bool(
                legacy_count
                and connection.execute(
                    select(sessions.c.session_id)
                    .where(
                        untitled,
                        sessions.c.title_attempt_count >= MAX_TITLE_ATTEMPTS,
                        _title_auth_failure_clause(sessions),
                    )
                    .limit(1)
                ).first()
            )
            selected_failure_class = (
                str(row["failure_class"] or "availability")
                if row is not None and str(row["state"]) != "healthy"
                else "authentication"
                if has_legacy_auth
                else "availability"
                if legacy_count
                else None
            )
            commit_seq = _current_commit_seq(connection)
            incident_id: str | None = str(row["incident_id"]) if row is not None and row["incident_id"] else None
            changed = False
            if row is None:
                incident_id = str(uuid4()) if legacy_count else None
                state = "open" if incident_id else "healthy"
                commit_seq = _advance_commit_seq(connection, observed_at)
                connection.execute(
                    insert(dependency).values(
                        use_case=TITLE_DEPENDENCY_USE_CASE,
                        provider=provider,
                        model=model,
                        credential_binding=credential_binding,
                        state=state,
                        incident_id=incident_id,
                        failure_class=selected_failure_class if incident_id else None,
                        first_failure_at=observed_at if incident_id else None,
                        last_failure_at=observed_at if incident_id else None,
                        next_probe_at=observed_at if incident_id else None,
                        credential_generation=credential_generation,
                        last_error=(f"adopted_legacy_{selected_failure_class}_debt" if incident_id else None),
                        legacy_repair_version=TITLE_DEPENDENCY_LEGACY_REPAIR_VERSION,
                        commit_seq=commit_seq,
                        created_at=observed_at,
                        updated_at=observed_at,
                    )
                )
                changed = True
            elif str(row["state"]) == "healthy" and legacy_count:
                incident_id = str(uuid4())
                commit_seq = _advance_commit_seq(connection, observed_at)
                connection.execute(
                    update(dependency)
                    .where(*identity)
                    .values(
                        state="open",
                        incident_id=incident_id,
                        failure_class=selected_failure_class,
                        first_failure_at=observed_at,
                        last_failure_at=observed_at,
                        next_probe_at=observed_at,
                        credential_generation=credential_generation,
                        last_error=f"adopted_legacy_{selected_failure_class}_debt",
                        legacy_repair_version=TITLE_DEPENDENCY_LEGACY_REPAIR_VERSION,
                        commit_seq=commit_seq,
                        updated_at=observed_at,
                    )
                )
                changed = True
            elif str(row["state"]) != "healthy" and str(row["credential_generation"]) != credential_generation:
                commit_seq = _advance_commit_seq(connection, observed_at)
                connection.execute(
                    update(dependency)
                    .where(*identity)
                    .values(
                        state="open",
                        credential_generation=credential_generation,
                        next_probe_at=observed_at,
                        probe_token=None,
                        probe_expires_at=None,
                        commit_seq=commit_seq,
                        updated_at=observed_at,
                    )
                )
                # A fresh credential generation is the explicit signal to
                # probe now. Keep the dependency's incident identity, but do
                # not leave its bound obligations parked behind the failed
                # generation's stale backoff timestamp.
                if incident_id is not None:
                    connection.execute(
                        update(sessions)
                        .where(sessions.c.title_dependency_incident_id == incident_id)
                        .values(title_retry_at=observed_at, commit_seq=commit_seq, updated_at=observed_at)
                    )
                    connection.execute(
                        update(catalog)
                        .where(
                            catalog.c.session_id.in_(
                                select(sessions.c.session_id).where(sessions.c.title_dependency_incident_id == incident_id)
                            )
                        )
                        .values(title_retry_at=observed_at, updated_at=observed_at)
                    )
                changed = True

            if row is not None and needs_legacy_repair:
                if not changed:
                    commit_seq = _advance_commit_seq(connection, observed_at)
                connection.execute(
                    update(dependency)
                    .where(*identity)
                    .values(
                        legacy_repair_version=TITLE_DEPENDENCY_LEGACY_REPAIR_VERSION,
                        commit_seq=commit_seq,
                        updated_at=observed_at,
                    )
                )
                changed = True

            bound = 0
            unbound_legacy_count = (
                int(
                    connection.execute(
                        select(func.count()).select_from(sessions).where(legacy_clause, sessions.c.title_dependency_incident_id.is_(None))
                    ).scalar_one()
                )
                if needs_legacy_repair
                else 0
            )
            if incident_id is not None and unbound_legacy_count:
                dep_row = connection.execute(select(dependency).where(*identity)).mappings().one()
                retry_at = dep_row["next_probe_at"] or observed_at
                if not changed:
                    commit_seq = _advance_commit_seq(connection, observed_at)
                bound = int(
                    connection.execute(
                        update(sessions)
                        .where(legacy_clause, sessions.c.title_dependency_incident_id.is_(None))
                        .values(
                            title_dependency_incident_id=incident_id,
                            title_retry_at=retry_at,
                            commit_seq=commit_seq,
                            updated_at=observed_at,
                        )
                    ).rowcount
                    or 0
                )
                changed = changed or bound > 0
            current = connection.execute(select(dependency).where(*identity)).mappings().one()
            return {
                "changed": changed,
                "adopted_sessions": bound,
                "dependency": _runtime_dependency_dto(current),
                "commit_seq": str(commit_seq),
            }

    def acquire_storage_title_dependency(
        self,
        *,
        session_id: UUID,
        provider: str,
        model: str,
        credential_binding: str,
        credential_generation: str,
        probe_token: UUID,
        observed_at: datetime,
        lease_seconds: int,
    ) -> dict[str, Any]:
        """Fence a shared outage and admit at most one recovery probe."""

        dependency = RuntimeDependencyState.__table__
        sessions = StorageSession.__table__
        session_key = str(session_id)
        token = str(probe_token)
        identity = (
            dependency.c.use_case == TITLE_DEPENDENCY_USE_CASE,
            dependency.c.provider == provider,
            dependency.c.model == model,
            dependency.c.credential_binding == credential_binding,
        )
        with _write_transaction(self.engine) as connection:
            dep = connection.execute(select(dependency).where(*identity)).mappings().first()
            if dep is None:
                return {"dependency_missing": True, "allowed": False, "commit_seq": str(_current_commit_seq(connection))}
            session = connection.execute(
                select(sessions.c.session_id, sessions.c.anchor_title, sessions.c.anchor_title_source).where(
                    sessions.c.session_id == session_key
                )
            ).first()
            if session is None:
                return {"session_missing": True, "allowed": False, "commit_seq": str(_current_commit_seq(connection))}
            if not _title_anchor_replaceable(session.anchor_title, session.anchor_title_source):
                return {"allowed": False, "already_complete": True, "commit_seq": str(_current_commit_seq(connection))}
            if str(dep["state"]) == "healthy":
                return {
                    "allowed": True,
                    "probe": False,
                    "incident_id": None,
                    "commit_seq": str(_current_commit_seq(connection)),
                }

            incident_id = str(dep["incident_id"])
            if dep["probe_token"] == token:
                return {
                    "allowed": True,
                    "probe": True,
                    "incident_id": incident_id,
                    "commit_seq": str(_current_commit_seq(connection)),
                }
            generation_changed = str(dep["credential_generation"]) != credential_generation
            lease_expired = dep["probe_expires_at"] is None or _as_aware_utc(dep["probe_expires_at"]) <= observed_at
            probe_due = dep["next_probe_at"] is None or _as_aware_utc(dep["next_probe_at"]) <= observed_at
            allowed = (
                generation_changed or (str(dep["state"]) == "open" and probe_due) or (str(dep["state"]) == "probing" and lease_expired)
            )
            commit_seq = _advance_commit_seq(connection, observed_at)
            if allowed:
                probe_expires_at = observed_at + timedelta(seconds=lease_seconds)
                connection.execute(
                    update(dependency)
                    .where(*identity)
                    .values(
                        state="probing",
                        credential_generation=credential_generation,
                        probe_token=token,
                        probe_expires_at=probe_expires_at,
                        commit_seq=commit_seq,
                        updated_at=observed_at,
                    )
                )
                retry_at = probe_expires_at
            else:
                retry_at = (
                    dep["probe_expires_at"]
                    if str(dep["state"]) == "probing" and dep["probe_expires_at"] is not None
                    else dep["next_probe_at"] or (observed_at + TITLE_DEPENDENCY_PROBE_DELAY)
                )
            connection.execute(
                update(sessions)
                .where(sessions.c.session_id == session_key)
                .values(
                    title_dependency_incident_id=incident_id,
                    title_retry_at=retry_at,
                    commit_seq=commit_seq,
                    updated_at=observed_at,
                )
            )
            return {
                "allowed": allowed,
                "probe": allowed,
                "incident_id": incident_id,
                "retry_at": _encode_datetime(retry_at),
                "commit_seq": str(commit_seq),
            }

    def fail_storage_title_dependency(
        self,
        *,
        session_id: UUID,
        provider: str,
        model: str,
        credential_binding: str,
        credential_generation: str,
        probe_token: UUID,
        failure_class: str,
        reason: str,
        failed_at: datetime,
    ) -> dict[str, Any]:
        """Open or coalesce one shared dependency incident without spending row retries."""

        if failure_class not in {"authentication", "availability"}:
            raise ValueError(f"unsupported title dependency failure class: {failure_class}")

        dependency = RuntimeDependencyState.__table__
        sessions = StorageSession.__table__
        catalog = LiveSessionCatalog.__table__
        identity = (
            dependency.c.use_case == TITLE_DEPENDENCY_USE_CASE,
            dependency.c.provider == provider,
            dependency.c.model == model,
            dependency.c.credential_binding == credential_binding,
        )
        with _write_transaction(self.engine) as connection:
            dep = connection.execute(select(dependency).where(*identity)).mappings().first()
            if dep is None:
                return {"dependency_missing": True, "commit_seq": str(_current_commit_seq(connection))}
            if dep["last_failure_token"] == str(probe_token) and dep["incident_id"]:
                return {
                    "changed": False,
                    "new_incident": False,
                    "incident_id": str(dep["incident_id"]),
                    "affected_sessions": 0,
                    "attempt_consumed": False,
                    "next_probe_at": _encode_datetime(dep["next_probe_at"]),
                    "commit_seq": str(_current_commit_seq(connection)),
                }
            recovered_at = _as_aware_utc(dep["recovered_at"])
            if str(dep["state"]) == "healthy" and recovered_at is not None and failed_at <= recovered_at:
                return {
                    "changed": False,
                    "stale_failure": True,
                    "attempt_consumed": False,
                    "commit_seq": str(_current_commit_seq(connection)),
                }
            newly_opened = str(dep["state"]) == "healthy" or not dep["incident_id"]
            incident_id = str(uuid4()) if newly_opened else str(dep["incident_id"])
            effective_failure_class = failure_class if newly_opened else str(dep["failure_class"] or failure_class)
            probe_delay = (
                TITLE_DEPENDENCY_AVAILABILITY_PROBE_DELAY if effective_failure_class == "availability" else TITLE_DEPENDENCY_PROBE_DELAY
            )
            next_probe_at = failed_at + probe_delay
            commit_seq = _advance_commit_seq(connection, failed_at)
            connection.execute(
                update(dependency)
                .where(*identity)
                .values(
                    state="open",
                    incident_id=incident_id,
                    failure_class=effective_failure_class,
                    first_failure_at=failed_at if newly_opened else dep["first_failure_at"],
                    last_failure_at=failed_at,
                    next_probe_at=next_probe_at,
                    credential_generation=credential_generation,
                    probe_token=None,
                    probe_expires_at=None,
                    last_failure_token=str(probe_token),
                    last_error=reason[:255],
                    recovered_at=None,
                    commit_seq=commit_seq,
                    updated_at=failed_at,
                )
            )
            affected_ids = [
                str(value)
                for value in connection.execute(
                    select(sessions.c.session_id).where(
                        sessions.c.session_id == str(session_id),
                        or_(sessions.c.anchor_title.is_(None), sessions.c.anchor_title == ""),
                    )
                ).scalars()
            ]
            if affected_ids:
                connection.execute(
                    update(sessions)
                    .where(sessions.c.session_id.in_(affected_ids))
                    .values(
                        title_dependency_incident_id=incident_id,
                        title_last_attempt_at=failed_at,
                        title_retry_at=next_probe_at,
                        title_last_error=reason[:128],
                        commit_seq=commit_seq,
                        updated_at=failed_at,
                    )
                )
                connection.execute(
                    update(catalog)
                    .where(catalog.c.session_id.in_(affected_ids))
                    .values(title_retry_at=next_probe_at, title_last_error=reason[:128], updated_at=failed_at)
                )
            return {
                "changed": True,
                "new_incident": newly_opened,
                "incident_id": incident_id,
                "affected_sessions": len(affected_ids),
                "attempt_consumed": False,
                "next_probe_at": next_probe_at.isoformat(),
                "commit_seq": str(commit_seq),
            }

    def recover_storage_title_dependency(
        self,
        *,
        provider: str,
        model: str,
        credential_binding: str,
        credential_generation: str,
        incident_id: UUID,
        probe_token: UUID,
        recovered_at: datetime,
    ) -> dict[str, Any]:
        """Close one incident and re-arm exactly the title debt it blocked."""

        dependency = RuntimeDependencyState.__table__
        sessions = StorageSession.__table__
        catalog = LiveSessionCatalog.__table__
        identity = (
            dependency.c.use_case == TITLE_DEPENDENCY_USE_CASE,
            dependency.c.provider == provider,
            dependency.c.model == model,
            dependency.c.credential_binding == credential_binding,
        )
        incident_key = str(incident_id)
        token = str(probe_token)
        with _write_transaction(self.engine) as connection:
            dep = connection.execute(select(dependency).where(*identity)).mappings().first()
            if dep is None:
                return {"dependency_missing": True, "commit_seq": str(_current_commit_seq(connection))}
            if str(dep["state"]) == "healthy" and dep["incident_id"] is None:
                return {"changed": False, "rearmed_sessions": 0, "commit_seq": str(_current_commit_seq(connection))}
            if str(dep["incident_id"] or "") != incident_key or str(dep["probe_token"] or "") != token:
                return {"claim_conflict": True, "commit_seq": str(_current_commit_seq(connection))}
            debt_ids = [
                str(value)
                for value in connection.execute(
                    select(sessions.c.session_id).where(sessions.c.title_dependency_incident_id == incident_key)
                ).scalars()
            ]
            retryable_debt_ids = [
                str(value)
                for value in connection.execute(
                    select(sessions.c.session_id).where(
                        sessions.c.title_dependency_incident_id == incident_key,
                        _retryable_title_row_failure_clause(sessions),
                    )
                ).scalars()
            ]
            shared_debt_ids = sorted(set(debt_ids) - set(retryable_debt_ids))
            commit_seq = _advance_commit_seq(connection, recovered_at)
            connection.execute(
                update(dependency)
                .where(*identity)
                .values(
                    state="healthy",
                    incident_id=None,
                    failure_class=None,
                    first_failure_at=None,
                    last_failure_at=None,
                    next_probe_at=None,
                    credential_generation=credential_generation,
                    probe_token=None,
                    probe_expires_at=None,
                    last_failure_token=None,
                    last_error=None,
                    recovered_at=recovered_at,
                    commit_seq=commit_seq,
                    updated_at=recovered_at,
                )
            )
            if shared_debt_ids:
                connection.execute(
                    update(sessions)
                    .where(sessions.c.session_id.in_(shared_debt_ids), sessions.c.title_dependency_incident_id == incident_key)
                    .values(
                        title_attempt_count=0,
                        title_retry_at=recovered_at,
                        title_last_error=None,
                        title_dependency_incident_id=None,
                        commit_seq=commit_seq,
                        updated_at=recovered_at,
                    )
                )
                connection.execute(
                    update(catalog)
                    .where(catalog.c.session_id.in_(shared_debt_ids))
                    .values(title_retry_at=recovered_at, title_last_error=None, updated_at=recovered_at)
                )
            if retryable_debt_ids:
                connection.execute(
                    update(sessions)
                    .where(
                        sessions.c.session_id.in_(retryable_debt_ids),
                        sessions.c.title_dependency_incident_id == incident_key,
                    )
                    .values(
                        title_retry_at=recovered_at,
                        title_dependency_incident_id=None,
                        commit_seq=commit_seq,
                        updated_at=recovered_at,
                    )
                )
                connection.execute(
                    update(catalog)
                    .where(catalog.c.session_id.in_(retryable_debt_ids))
                    .values(title_retry_at=recovered_at, updated_at=recovered_at)
                )
            return {
                "changed": True,
                "rearmed_sessions": len(debt_ids),
                "incident_id": incident_key,
                "commit_seq": str(commit_seq),
            }

    def read_storage_title_dependency_health(self) -> dict[str, Any]:
        dependency = RuntimeDependencyState.__table__
        sessions = StorageSession.__table__
        observed_at = datetime.now(UTC)
        with _read_snapshot(self.engine) as connection:
            rows = (
                connection.execute(
                    select(dependency)
                    .where(dependency.c.use_case == TITLE_DEPENDENCY_USE_CASE)
                    .order_by(dependency.c.provider, dependency.c.model, dependency.c.credential_binding)
                )
                .mappings()
                .all()
            )
            blocked = dict(
                connection.execute(
                    select(sessions.c.title_dependency_incident_id, func.count())
                    .where(sessions.c.title_dependency_incident_id.is_not(None))
                    .group_by(sessions.c.title_dependency_incident_id)
                ).all()
            )
            dependencies = []
            for row in rows:
                item = _runtime_dependency_dto(row)
                item["blocked_sessions"] = int(blocked.get(row["incident_id"], 0)) if row["incident_id"] else 0
                dependencies.append(item)
            open_count = sum(1 for row in rows if str(row["state"]) != "healthy")
            obligation = _storage_title_obligation_clause(sessions)
            pending = and_(
                obligation,
                or_(
                    sessions.c.title_attempt_count < MAX_TITLE_ATTEMPTS,
                    sessions.c.title_dependency_incident_id.is_not(None),
                    _retryable_title_row_failure_clause(sessions),
                ),
            )
            overdue = and_(
                pending,
                or_(sessions.c.title_retry_at.is_(None), sessions.c.title_retry_at <= observed_at),
            )
            terminal = and_(
                obligation,
                sessions.c.title_attempt_count >= MAX_TITLE_ATTEMPTS,
                sessions.c.title_dependency_incident_id.is_(None),
                ~_retryable_title_row_failure_clause(sessions),
            )
            counts = connection.execute(
                select(
                    func.count().filter(pending).label("pending"),
                    func.count().filter(overdue).label("overdue"),
                    func.count().filter(terminal).label("terminal"),
                    func.count().filter(and_(terminal, _legacy_title_dependency_failure_clause(sessions))).label("terminal_shared"),
                    func.min(sessions.c.last_activity_at).filter(pending).label("oldest_pending_at"),
                    func.min(sessions.c.last_activity_at).filter(overdue).label("oldest_overdue_at"),
                ).select_from(sessions)
            ).one()
            oldest_pending_at = _as_aware_utc(counts.oldest_pending_at)
            oldest_overdue_at = _as_aware_utc(counts.oldest_overdue_at)
            oldest_pending_age_seconds = (
                max(0, int((observed_at - oldest_pending_at).total_seconds())) if oldest_pending_at is not None else None
            )
            oldest_overdue_age_seconds = (
                max(0, int((observed_at - oldest_overdue_at).total_seconds())) if oldest_overdue_at is not None else None
            )
            backlog_degraded = bool(
                int(counts.terminal or 0)
                or (
                    oldest_pending_age_seconds is not None
                    and oldest_pending_age_seconds >= int(TITLE_BACKLOG_DEGRADED_AFTER.total_seconds())
                )
            )
            return {
                "status": "degraded" if open_count or backlog_degraded else "healthy",
                "open_dependencies": open_count,
                "blocked_sessions": sum(item["blocked_sessions"] for item in dependencies),
                "pending_sessions": int(counts.pending or 0),
                "overdue_sessions": int(counts.overdue or 0),
                "terminal_sessions": int(counts.terminal or 0),
                "terminal_shared_failure_sessions": int(counts.terminal_shared or 0),
                "oldest_pending_at": _encode_datetime(oldest_pending_at),
                "oldest_pending_age_seconds": oldest_pending_age_seconds,
                "oldest_overdue_at": _encode_datetime(oldest_overdue_at),
                "oldest_overdue_age_seconds": oldest_overdue_age_seconds,
                "backlog_degraded_after_seconds": int(TITLE_BACKLOG_DEGRADED_AFTER.total_seconds()),
                "dependencies": dependencies,
                "observed_at": observed_at.isoformat(),
                "commit_seq": str(_current_commit_seq(connection)),
            }

    def complete_storage_title(self, *, session_id: UUID, title: str, completed_at: datetime, source: str = "ai") -> dict[str, Any]:
        if is_path_like_title(title):
            raise ValueError("path_like_title")
        table = StorageSession.__table__
        catalog = LiveSessionCatalog.__table__
        card = LiveTimelineCard.__table__
        session_key = str(session_id)
        with _write_transaction(self.engine) as connection:
            worker_only = (
                select(LiveSessionThread.__table__.c.id)
                .where(
                    LiveSessionThread.__table__.c.session_id == session_key,
                    LiveSessionThread.__table__.c.is_primary == 1,
                    LiveSessionThread.__table__.c.branch_kind == "subagent",
                )
                .exists()
            )
            existing = (
                connection.execute(
                    select(
                        table.c.anchor_title,
                        table.c.anchor_title_source,
                        or_(
                            factory_title_assurance_session_clause(table),
                            title_origin_eligible_clause(table),
                        ).label("origin_eligible"),
                        worker_only.label("worker_only"),
                    ).where(table.c.session_id == session_key)
                )
                .mappings()
                .first()
            )
            if existing is None:
                return {"changed": False, "title": title, "missing": True, "commit_seq": str(_current_commit_seq(connection))}
            # An anchor is frozen once, with one exception: the Longhouse AI
            # title promotes a provider-native name that won the race to it.
            # The AI title is the single title authority -- once it lands it
            # is never rewritten, by the provider or a later AI guess. A
            # provider name is only ever a fallback for the anchor slot.
            anchored = bool(existing["anchor_title"])
            promotion = anchored and source == "ai" and _title_anchor_replaceable(existing["anchor_title"], existing["anchor_title_source"])
            if anchored and not promotion:
                return {
                    "changed": False,
                    "title": str(existing["anchor_title"]),
                    "commit_seq": str(_current_commit_seq(connection)),
                }
            if not bool(existing["origin_eligible"]) or bool(existing["worker_only"]):
                return {"changed": False, "title": title, "ineligible": True, "commit_seq": str(_current_commit_seq(connection))}
            commit_seq = _advance_commit_seq(connection, completed_at)
            anchor_guard = (
                _title_anchor_replaceable_clause(table) if promotion else or_(table.c.anchor_title.is_(None), table.c.anchor_title == "")
            )
            changed = connection.execute(
                update(table)
                .where(
                    table.c.session_id == session_key,
                    anchor_guard,
                    or_(
                        factory_title_assurance_session_clause(table),
                        title_origin_eligible_clause(table),
                    ),
                    ~worker_only,
                )
                .values(
                    anchor_title=title,
                    anchor_title_source=source,
                    summary_title=title,
                    title_last_attempt_at=completed_at,
                    title_retry_at=None,
                    title_last_error=None,
                    title_dependency_incident_id=None,
                    commit_seq=commit_seq,
                    updated_at=completed_at,
                )
            ).rowcount
            if changed:
                connection.execute(
                    update(catalog)
                    .where(catalog.c.session_id == session_key)
                    .values(
                        anchor_title=title,
                        anchor_title_source=source,
                        summary_title=title,
                        title_retry_at=None,
                        title_last_error=None,
                        updated_at=completed_at,
                    )
                )
                connection.execute(update(card).where(card.c.session_id == session_key).values(summary_title=title))
            return {"changed": bool(changed), "title": title, "commit_seq": str(commit_seq)}

    def fail_storage_title(self, *, session_id: UUID, reason: str, failed_at: datetime) -> dict[str, Any]:
        table = StorageSession.__table__
        session_key = str(session_id)
        with _write_transaction(self.engine) as connection:
            row = connection.execute(
                select(
                    table.c.anchor_title,
                    table.c.title_attempt_count,
                    table.c.first_user_message_preview,
                    table.c.project,
                    table.c.git_branch,
                    table.c.provider,
                    table.c.title_last_error,
                ).where(table.c.session_id == session_key)
            ).first()
            existing_error = str(row.title_last_error or "").lower() if row is not None else ""
            existing_retryable = existing_error in RETRYABLE_TITLE_ROW_ERRORS
            if row is None or row.anchor_title or (int(row.title_attempt_count or 0) >= MAX_TITLE_ATTEMPTS and not existing_retryable):
                return {"changed": False, "commit_seq": str(_current_commit_seq(connection))}
            attempts = min(int(row.title_attempt_count or 0) + 1, MAX_TITLE_ATTEMPTS)
            retryable = reason.lower() in RETRYABLE_TITLE_ROW_ERRORS
            if attempts >= MAX_TITLE_ATTEMPTS and not retryable:
                # Terminal: stop retrying durably, but do not freeze the
                # deterministic prompt/project fallback as an AI anchor. The
                # timeline can still render a clearly degraded fallback while
                # a later explicit repair is free to install a real title.
                commit_seq = _advance_commit_seq(connection, failed_at)
                connection.execute(
                    update(table)
                    .where(table.c.session_id == session_key)
                    .values(
                        anchor_title=None,
                        summary_title=None,
                        title_attempt_count=attempts,
                        title_last_attempt_at=failed_at,
                        title_retry_at=failed_at,
                        title_last_error=reason[:128],
                        commit_seq=commit_seq,
                        updated_at=failed_at,
                    )
                )
                connection.execute(
                    update(LiveSessionCatalog.__table__)
                    .where(LiveSessionCatalog.__table__.c.session_id == session_key)
                    .values(
                        anchor_title=None,
                        summary_title=None,
                        title_retry_at=failed_at,
                        title_last_error=reason[:128],
                        updated_at=failed_at,
                    )
                )
                connection.execute(
                    update(LiveTimelineCard.__table__)
                    .where(LiveTimelineCard.__table__.c.session_id == session_key)
                    .values(summary_title=None)
                )
                return {
                    "changed": True,
                    "terminal": True,
                    "title": None,
                    "attempt_count": attempts,
                    "retry_at": failed_at.isoformat(),
                    "commit_seq": str(commit_seq),
                }
            if reason == "no_meaningful_user_text":
                retry_at = None
            elif retryable:
                # Empty provider output is row-local but usually transient.
                # Retain it as a durable obligation with a capped slow retry;
                # malformed payloads still terminate at the ordinary budget.
                retry_at = (
                    failed_at + TITLE_ROW_TRANSIENT_RETRY_DELAY
                    if attempts >= MAX_TITLE_ATTEMPTS
                    else failed_at + timedelta(seconds=2**attempts)
                )
            else:
                retry_at = failed_at + timedelta(seconds=min(30, 2 ** min(attempts, 5)))
            commit_seq = _advance_commit_seq(connection, failed_at)
            connection.execute(
                update(table)
                .where(table.c.session_id == session_key)
                .values(
                    title_attempt_count=attempts,
                    title_last_attempt_at=failed_at,
                    title_retry_at=retry_at,
                    title_last_error=reason[:128],
                    commit_seq=commit_seq,
                    updated_at=failed_at,
                )
            )
            connection.execute(
                update(LiveSessionCatalog.__table__)
                .where(LiveSessionCatalog.__table__.c.session_id == session_key)
                .values(title_retry_at=retry_at, title_last_error=reason[:128], updated_at=failed_at)
            )
            return {
                "changed": True,
                "terminal": False,
                "title": None,
                "attempt_count": attempts,
                "retry_at": retry_at.isoformat() if retry_at is not None else None,
                "commit_seq": str(commit_seq),
            }

    def summarize_provider_versions(self, *, owner_id: int, days_back: int) -> dict[str, Any]:
        """Which provider CLI releases the owner ran, grouped by provider and version.

        Counts storage sessions started inside the window that carry a version.
        ``first_seen`` is the earliest ``started_at`` and ``last_seen`` the latest
        ``last_activity_at`` among those sessions. Test and e2e environments are
        excluded, as on the timeline. No verdict is made here.
        """
        table = StorageSession.__table__
        observed_at = datetime.now(UTC)
        window_start = observed_at - timedelta(days=days_back)
        statement = (
            select(
                table.c.provider,
                table.c.provider_version,
                func.count().label("sessions"),
                func.count(func.distinct(table.c.machine_id)).label("devices"),
                func.min(table.c.started_at).label("first_seen"),
                func.max(table.c.last_activity_at).label("last_seen"),
            )
            .where(
                table.c.owner_id == str(owner_id),
                table.c.provider_version.is_not(None),
                table.c.started_at >= window_start,
                table.c.environment.notin_(("test", "e2e")),
            )
            .group_by(table.c.provider, table.c.provider_version)
            .order_by(table.c.provider.asc(), func.max(table.c.last_activity_at).desc(), table.c.provider_version.asc())
        )
        with _read_snapshot(self.engine) as connection:
            rows = connection.execute(statement).fetchall()
            commit_seq = _current_commit_seq(connection)
        return {
            "observed_at": observed_at.isoformat(),
            "window_days": days_back,
            "versions": [
                {
                    "provider": str(row.provider),
                    "provider_version": str(row.provider_version),
                    "sessions": int(row.sessions),
                    "devices": int(row.devices),
                    "first_seen": _as_aware_utc(row.first_seen).isoformat(),
                    "last_seen": _as_aware_utc(row.last_seen).isoformat(),
                }
                for row in rows
            ],
            "commit_seq": str(commit_seq),
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
            table.c.user_hidden_from_timeline == 0,
            table.c.user_state.notin_(("archived", "snoozed", "deleted")),
            ~select(tombstone.c.session_id).where(tombstone.c.session_id == table.c.session_id).exists(),
        )
        if project is not None:
            statement = statement.where(table.c.project == project)
        if provider is not None:
            statement = statement.where(table.c.provider == provider)
        if not include_test:
            statement = statement.where(table.c.environment.notin_(("test", "e2e")))
        statement = statement.where(
            ~effective_system_hidden_clause(
                table,
                include_test=include_test,
                worker_only_evidence=primary_worker_only_clause(table, LiveSessionThread.__table__),
            )
        )
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

    def read_tenant_funnel_facts(self, *, owner_id: str) -> dict[str, Any]:
        """Return the catalog half of a tester's funnel: machines and sessions per provider.

        Counts and first-arrival times only.
        "Shipped" is the catalog's own ingest time (`created_at`), not the
        provider's transcript start, so an imported year of history reads as
        arriving when it was imported. System-hidden work (automation, test,
        subagents) is excluded the way the timeline excludes it; sessions a user has
        since hidden or archived still count, because they were shipped.
        """

        sessions = StorageSession.__table__
        tombstones = LiveSessionTombstone.__table__
        tokens = LiveDeviceToken.__table__
        visible = and_(
            sessions.c.owner_id == owner_id,
            sessions.c.user_state != "deleted",
            ~select(tombstones.c.session_id).where(tombstones.c.session_id == sessions.c.session_id).exists(),
            ~effective_system_hidden_clause(
                sessions,
                include_test=False,
                worker_only_evidence=primary_worker_only_clause(sessions, LiveSessionThread.__table__),
            ),
        )
        with _read_snapshot(self.engine) as connection:
            provider_rows = connection.execute(
                select(sessions.c.provider, func.count(sessions.c.session_id), func.min(sessions.c.created_at))
                .where(visible)
                .group_by(sessions.c.provider)
                .order_by(sessions.c.provider)
            ).all()
            # History, not state: a machine the tester has since disconnected still connected.
            first_created_at = connection.execute(
                select(func.min(tokens.c.created_at)).where(tokens.c.owner_id == int(owner_id))
            ).scalar_one_or_none()
            return {
                "providers": [
                    {
                        "provider": str(provider),
                        "sessions": int(count or 0),
                        "first_shipped_at": _encode_datetime(first_shipped_at),
                    }
                    for provider, count, first_shipped_at in provider_rows
                ],
                "devices": {"first_created_at": _encode_datetime(first_created_at)},
                "observed_at": datetime.now(UTC).isoformat(),
            }

    def read_storage_telemetry_summary(self) -> dict[str, Any]:
        """Return O(1) transactional storage and projector counters."""

        observed_at = datetime.now(UTC)
        with _read_snapshot(self.engine) as connection:
            row = connection.execute(select(storage_telemetry_counters).where(storage_telemetry_counters.c.singleton == 1)).mappings().one()
            projectors = []
            if int(row["search_projector_rows"]) > 0:
                projectors.append(
                    {
                        "projector": "search-v2",
                        "lagging": int(row["search_projector_lagging"]),
                        "failed": int(row["search_projector_failed"]),
                        "claimed": int(row["search_projector_claimed"]),
                        "oldest_lag_updated_at": None,
                    }
                )
            # The trigger-maintained counters above only cover search-v2, so a
            # failing embedding row was not merely unalerted -- it was
            # unreportable. Two of them sat failed for sixteen days without
            # appearing in any surface. status is indexed and almost every row
            # is 'idle', so this equality scan stays cheap while making a stuck
            # row on ANY projector, including a retired generation, visible.
            projector_state = ProjectorState.__table__
            stuck = [
                {
                    "projector": str(stuck_row.projector),
                    "status": str(stuck_row.status),
                    "rows": int(stuck_row.rows),
                    "oldest_updated_at": _encode_datetime(stuck_row.oldest_updated_at),
                    "retired": str(stuck_row.projector) not in ACTIVE_PROJECTORS,
                }
                for stuck_row in connection.execute(
                    select(
                        projector_state.c.projector,
                        projector_state.c.status,
                        func.count().label("rows"),
                        func.min(projector_state.c.updated_at).label("oldest_updated_at"),
                    )
                    .where(projector_state.c.status.in_(("failed", "quarantined")))
                    .group_by(projector_state.c.projector, projector_state.c.status)
                    .order_by(projector_state.c.projector.asc())
                ).all()
            ]
            return {
                "stuck_projectors": stuck,
                "objects": {
                    "raw": {"count": int(row["raw_count"]), "bytes": int(row["raw_bytes"])},
                    "render": {"count": int(row["render_count"]), "bytes": int(row["render_bytes"])},
                    "media": {"count": int(row["media_count"]), "bytes": int(row["media_bytes"])},
                },
                "projectors": projectors,
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

    def read_storage_session_raw_neighborhood(
        self,
        *,
        session_id: UUID,
        owner_id: str,
        envelope_id: str,
    ) -> dict[str, Any]:
        """Return one raw companion and at most two source neighbors per side."""

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
            current = None
            if deleted is None and session_row is not None:
                current = (
                    connection.execute(
                        select(raw).where(
                            raw.c.session_id == session_key,
                            raw.c.envelope_id == envelope_id,
                            raw.c.retired_at.is_(None),
                        )
                    )
                    .mappings()
                    .first()
                )
            rows: list[Any] = []
            if current is not None:
                identity = (
                    raw.c.session_id == session_key,
                    raw.c.machine_id == current["machine_id"],
                    raw.c.provider == current["provider"],
                    raw.c.opaque_source_id == current["opaque_source_id"],
                    raw.c.source_epoch == current["source_epoch"],
                    raw.c.retired_at.is_(None),
                )
                position = tuple_(raw.c.range_start, raw.c.envelope_id)
                current_position = (current["range_start"], current["envelope_id"])
                before = list(
                    connection.execute(
                        select(raw)
                        .where(*identity, position < current_position)
                        .order_by(raw.c.range_start.desc(), raw.c.envelope_id.desc())
                        .limit(2)
                    )
                    .mappings()
                    .all()
                )
                after = list(
                    connection.execute(
                        select(raw)
                        .where(*identity, position > current_position)
                        .order_by(raw.c.range_start.asc(), raw.c.envelope_id.asc())
                        .limit(2)
                    )
                    .mappings()
                    .all()
                )
                rows = [*reversed(before), current, *after]
            return {
                "found": session_row is not None and deleted is None,
                "deleted": deleted is not None,
                "deletion_revision": str(deleted) if deleted is not None else None,
                "companion_found": current is not None,
                "objects": [_raw_object_manifest_dto(row) for row in rows],
                "commit_seq": str(_current_commit_seq(connection)),
                "observed_at": observed_at.isoformat(),
            }

    def read_storage_session_render_manifest(
        self,
        *,
        session_id: UUID,
        owner_id: str,
        generation_id: UUID | None,
        anchor: str,
        after_order_key: str | None,
        before_order_key: str | None,
        limit: int,
        object_cursor: str | None = None,
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
            abandoned_events: int | None = None
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
                branch_counts = connection.execute(
                    select(
                        func.count(),
                        func.count(object_table.c.abandoned_events),
                        func.coalesce(func.sum(object_table.c.abandoned_events), 0),
                    ).where(
                        object_table.c.generation_id == requested_generation,
                        object_table.c.retired_at.is_(None),
                        object_table.c.event_count > 0,
                    )
                ).one()
                if branch_counts[0] == branch_counts[1]:
                    abandoned_events = int(branch_counts[2])
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
                if object_cursor is not None:
                    # Continue a manifest page exactly where the previous one
                    # stopped: the cursor is the last object's full ordering
                    # key, object_id included, so pages concatenate to what
                    # one larger page would have returned.
                    edge = "last" if anchor == "tail" else "first"
                    object_key = tuple_(
                        *(
                            object_table.c[f"{edge}_{name}"]
                            for name in (
                                "order_time_us",
                                "machine_id",
                                "provider",
                                "opaque_source_id",
                                "source_epoch",
                                "source_position",
                                "event_subordinal",
                            )
                        ),
                        object_table.c.object_id,
                    )
                    cursor_values = tuple(json.loads(object_cursor))
                    statement = statement.where(object_key < cursor_values if anchor == "tail" else object_key > cursor_values)
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
                    if anchor == "tail"
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
                "abandoned_events": abandoned_events,
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
        catalog_table = LiveSessionCatalog.__table__
        thread_table = LiveSessionThread.__table__
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
            canonical_device_id = connection.execute(
                select(catalog_table.c.device_id).where(catalog_table.c.session_id == session_key)
            ).scalar_one_or_none()
            primary_branch_kind = connection.execute(
                select(thread_table.c.branch_kind).where(
                    thread_table.c.session_id == session_key,
                    thread_table.c.is_primary == 1,
                )
            ).scalar_one_or_none()
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
            session_dto = _storage_session_dto(session_row) if session_row is not None and deleted is None else None
            if session_dto is not None:
                # Timeline scope uses the canonical live identity when it is
                # present, retaining the immutable storage source identity for
                # legacy/archive sessions without a live catalog row.
                storage_device_id = session_row["machine_id"]
                session_dto["device_id"] = (
                    str(canonical_device_id)
                    if canonical_device_id is not None
                    else (str(storage_device_id) if storage_device_id is not None else None)
                )
                # Searchd cannot infer worker lineage from the storage session
                # row. Carry the canonical primary-thread fact in this frozen
                # projection snapshot so test scope never promotes a worker.
                session_dto["primary_thread_is_worker_only"] = primary_branch_kind == "subagent"
            return {
                "found": session_row is not None and deleted is None,
                "deleted": deleted is not None,
                "retired": session_row is not None and session_row["render_state"] == "retired",
                "deletion_revision": str(deleted) if deleted is not None else None,
                "snapshot_revision": str(snapshot_revision),
                "current_generation_id": current_generation,
                "generation_id": selected_generation,
                "generation": _render_generation_dto(generation_row) if generation_row is not None else None,
                "session": session_dto,
                "snapshot_object_count": snapshot_object_count,
                "snapshot_event_count": snapshot_event_count,
                "objects": [_render_object_manifest_dto(row) for row in rows],
                "has_more": has_more,
                "commit_seq": str(_current_commit_seq(connection)),
                "observed_at": observed_at.isoformat(),
            }

    def read_session_purge_manifest(
        self,
        *,
        session_id: UUID,
        after_kind: str | None,
        after_key: str | None,
        limit: int,
    ) -> dict[str, Any]:
        """Page every object a session owns, for deletion. No filters at all.

        Deletion is the one reader that must see objects every other read hides:
        retired raw and render rows, superseded render generations, and the
        media a session referenced. A filter here would leave bytes on disk with
        nothing left able to name them.

        Ownership comes off the durable ``sessions`` row when present. A
        live-only registration has no such row, so it uses the catalog's normal
        live/launch ownership resolver instead. The page is ordered by
        ``(kind, key)`` and the cursor is that pair, so a walk visits each
        object exactly once.
        """

        sessions = StorageSession.__table__
        tombstones = LiveSessionTombstone.__table__
        raw = LiveRawObject.__table__
        render = RenderObject.__table__
        media_refs = SessionMediaRef.__table__
        media = MediaObject.__table__
        session_key = str(session_id)
        with _read_snapshot(self.engine) as connection:
            session_row = (
                connection.execute(select(sessions.c.tenant_id, sessions.c.owner_id).where(sessions.c.session_id == session_key))
                .mappings()
                .first()
            )
            if session_row is not None:
                owner_id = str(session_row["owner_id"]) if session_row["owner_id"] is not None else None
            else:
                owner_id = self._resolve_session_owner_id(connection, session_id=session_key)
            deleted = connection.execute(
                select(tombstones.c.deletion_revision).where(tombstones.c.session_id == session_key)
            ).scalar_one_or_none()
            rows: list[dict[str, Any]] = []
            if session_row is not None:
                # "media" < "raw" < "render" lexicographically, which is the
                # order the cursor walks and the order these branches produce.
                remaining = limit + 1
                for kind in ("media", "raw", "render"):
                    if remaining <= 0:
                        break
                    if after_kind is not None and kind < after_kind:
                        continue
                    cursor = after_key if (after_kind is not None and kind == after_kind) else None
                    if kind == "media":
                        statement = (
                            select(
                                media_refs.c.media_hash,
                                media.c.object_path,
                                media.c.byte_size,
                                media.c.state,
                            )
                            .select_from(media_refs.outerjoin(media, media.c.media_hash == media_refs.c.media_hash))
                            .where(media_refs.c.session_id == session_key)
                            .group_by(media_refs.c.media_hash)
                        )
                        if cursor is not None:
                            statement = statement.where(media_refs.c.media_hash > cursor)
                        found = connection.execute(statement.order_by(media_refs.c.media_hash.asc()).limit(remaining)).mappings().all()
                        for row in found:
                            media_hash = str(row["media_hash"])
                            # Another session still holding an active reference
                            # owns these bytes as much as this one does. The
                            # fence retired this session's refs, so anything
                            # still active belongs to somebody else.
                            shared = (
                                connection.execute(
                                    select(media_refs.c.id).where(
                                        media_refs.c.media_hash == media_hash,
                                        media_refs.c.session_id != session_key,
                                        media_refs.c.state == "active",
                                    )
                                ).first()
                                is not None
                            )
                            rows.append(
                                {
                                    "kind": "media",
                                    "key": media_hash,
                                    "object_path": str(row["object_path"]) if row["object_path"] else None,
                                    "object_hash": media_hash,
                                    "byte_size": int(row["byte_size"] or 0),
                                    "shared": shared,
                                    "state": str(row["state"]) if row["state"] else None,
                                }
                            )
                    elif kind == "raw":
                        statement = select(
                            raw.c.envelope_id,
                            raw.c.object_path,
                            raw.c.object_hash,
                            raw.c.compressed_size,
                        ).where(raw.c.session_id == session_key)
                        if cursor is not None:
                            statement = statement.where(raw.c.envelope_id > cursor)
                        found = connection.execute(statement.order_by(raw.c.envelope_id.asc()).limit(remaining)).mappings().all()
                        rows.extend(
                            {
                                "kind": "raw",
                                "key": str(row["envelope_id"]),
                                "object_path": str(row["object_path"]),
                                "object_hash": str(row["object_hash"]),
                                "byte_size": int(row["compressed_size"] or 0),
                                "shared": False,
                                "state": None,
                            }
                            for row in found
                        )
                    else:
                        statement = select(
                            render.c.object_id,
                            render.c.object_path,
                            render.c.object_hash,
                            render.c.compressed_size,
                        ).where(render.c.session_id == session_key)
                        if cursor is not None:
                            statement = statement.where(render.c.object_id > cursor)
                        found = connection.execute(statement.order_by(render.c.object_id.asc()).limit(remaining)).mappings().all()
                        rows.extend(
                            {
                                "kind": "render",
                                "key": str(row["object_id"]),
                                "object_path": str(row["object_path"]),
                                "object_hash": str(row["object_hash"]),
                                "byte_size": int(row["compressed_size"] or 0),
                                "shared": False,
                                "state": None,
                            }
                            for row in found
                        )
                    remaining = limit + 1 - len(rows)
            has_more = len(rows) > limit
            rows = rows[:limit]
            return {
                "session_found": session_row is not None or owner_id is not None,
                "owner_id": owner_id,
                "tenant_id": str(session_row["tenant_id"]) if session_row is not None else None,
                "deleted": deleted is not None,
                "objects": rows,
                "has_more": has_more,
                "commit_seq": str(_current_commit_seq(connection)),
            }

    def repair_cursor_activity(
        self,
        *,
        session_id: str,
        expected_started_at: datetime,
        source_started_at: datetime,
        expected_last_activity_at: datetime,
        source_last_activity_at: datetime,
        now: datetime,
        dry_run: bool,
    ) -> dict[str, Any]:
        """Replace a verified Cursor import clock without rewriting raw history."""
        table = StorageSession.__table__
        with _write_transaction(self.engine) as connection:
            row = connection.execute(select(table).where(table.c.session_id == session_id)).mappings().first()
            commit_seq = _current_commit_seq(connection)
            if row is None or row["provider"] != "cursor":
                return {"repaired": False, "reason": "cursor_session_not_found", "commit_seq": str(commit_seq)}
            current = _as_aware_utc(row["last_activity_at"])
            current_start = _as_aware_utc(row["started_at"])
            source_start = _as_aware_utc(source_started_at)
            source = _as_aware_utc(source_last_activity_at)
            if current != _as_aware_utc(expected_last_activity_at) or current_start != _as_aware_utc(expected_started_at):
                return {"repaired": False, "reason": "compare_and_set_failed", "commit_seq": str(commit_seq)}
            if source is None or source_start is None or not source_start <= current_start or not source_start <= source <= current:
                return {"repaired": False, "reason": "source_clock_out_of_bounds", "commit_seq": str(commit_seq)}
            if source == current and source_start == current_start:
                return {"repaired": False, "reason": "already_current", "commit_seq": str(commit_seq)}
            result = {
                "session_id": session_id,
                "previous_started_at": current_start.isoformat(),
                "source_started_at": source_start.isoformat(),
                "previous_last_activity_at": current.isoformat(),
                "source_last_activity_at": source.isoformat(),
                "dry_run": dry_run,
            }
            if dry_run:
                return {**result, "repaired": False, "commit_seq": str(commit_seq)}
            commit_seq = _advance_commit_seq(connection, now)
            connection.execute(
                update(table)
                .where(table.c.session_id == session_id)
                .values(started_at=source_start, last_activity_at=source, updated_at=now, commit_seq=commit_seq)
            )
            # The storage row owns served history; keep its legacy projections
            # consistent without overwriting independently newer runtime evidence.
            for projection in (LiveSessionCatalog.__table__, LiveTimelineCard.__table__):
                connection.execute(
                    update(projection)
                    .where(
                        projection.c.session_id == session_id,
                        projection.c.last_activity_at <= expected_last_activity_at,
                    )
                    .values(started_at=func.min(projection.c.started_at, source_start), last_activity_at=source, updated_at=now)
                )
            return {**result, "repaired": True, "commit_seq": str(commit_seq)}

    def repair_storage_semantic_projection(
        self,
        *,
        session_id: UUID,
        owner_id: str,
        generation_id: UUID,
        objects: tuple[dict[str, Any], ...],
        observed_at: datetime,
    ) -> dict[str, Any]:
        """Apply verified semantic aggregates without rewriting immutable objects."""

        sessions = StorageSession.__table__
        generations = RenderGeneration.__table__
        render_objects = RenderObject.__table__
        tombstones = LiveSessionTombstone.__table__
        projectors = ProjectorState.__table__
        session_key = str(session_id)
        generation_key = str(generation_id)
        if not 0 <= len(objects) <= 1_000 or len({str(item.get("object_id")) for item in objects}) != len(objects):
            return {"invalid_request": True}
        with _write_transaction(self.engine) as connection:
            deleted = connection.execute(
                select(tombstones.c.deletion_revision).where(tombstones.c.session_id == session_key)
            ).scalar_one_or_none()
            if deleted is not None:
                return {"session_deleted": True, "deletion_revision": str(deleted)}
            session_row = (
                connection.execute(select(sessions).where(sessions.c.session_id == session_key, sessions.c.owner_id == owner_id))
                .mappings()
                .first()
            )
            generation_row = (
                connection.execute(
                    select(generations).where(
                        generations.c.generation_id == generation_key,
                        generations.c.session_id == session_key,
                        generations.c.state == "current",
                    )
                )
                .mappings()
                .first()
            )
            if session_row is None or generation_row is None:
                return {"not_found": True, "commit_seq": str(_current_commit_seq(connection))}
            rows = list(
                connection.execute(
                    select(render_objects).where(
                        render_objects.c.session_id == session_key,
                        render_objects.c.generation_id == generation_key,
                        render_objects.c.retired_at.is_(None),
                    )
                )
                .mappings()
                .all()
            )
            by_id = {str(row["object_id"]): row for row in rows}
            updates: list[tuple[str, dict[str, Any]]] = []
            has_semantic_corrections = any("user_messages" in item for item in objects)
            for item in objects:
                object_id = str(item.get("object_id") or "")
                row = by_id.get(object_id)
                if row is None:
                    return {"conflict": True, "commit_seq": str(_current_commit_seq(connection))}
                event_count = item.get("event_count")
                if type(event_count) is not int or event_count != int(row["event_count"]):
                    return {"conflict": True, "commit_seq": str(_current_commit_seq(connection))}
                values = {"abandoned_events": int(item["abandoned_events"])}
                if "user_messages" in item:
                    values.update(
                        user_messages=int(item["user_messages"]),
                        assistant_messages=int(item["assistant_messages"]),
                        tool_calls=int(item["tool_calls"]),
                        first_user_message_preview=item.get("first_user_message_preview"),
                        last_visible_text_preview=item.get("last_visible_text_preview"),
                        semantic_projection_version=1,
                    )
                if any(row[field] != value for field, value in values.items()):
                    updates.append((object_id, values))

            updates_by_id = {object_id: values for object_id, values in updates}
            complete_after = all(
                int(
                    updates_by_id.get(str(row["object_id"]), {}).get(
                        "semantic_projection_version",
                        row["semantic_projection_version"] or 0,
                    )
                )
                >= 1
                and updates_by_id.get(str(row["object_id"]), {}).get("abandoned_events", row["abandoned_events"]) is not None
                for row in rows
            )
            commit_time = _as_aware_utc(observed_at) or datetime.now(UTC)
            commit_seq = _current_commit_seq(connection)
            if updates:
                commit_seq = _advance_commit_seq(connection, commit_time)
                for object_id, values in updates:
                    # commit_seq is the immutable object's admission fence.
                    # Updating derived facts must not remove it from a snapshot
                    # already claimed by search or embeddings.
                    connection.execute(update(render_objects).where(render_objects.c.object_id == object_id).values(**values))

            needs_recompute = (
                has_semantic_corrections and complete_after and (bool(updates) or int(session_row["semantic_projection_version"] or 0) < 1)
            )
            if needs_recompute:
                _recompute_render_generation_projection(
                    connection,
                    session_id=session_key,
                    generation_id=generation_key,
                    commit_seq=commit_seq,
                    commit_time=commit_time,
                    reset_derived_title=int(session_row["semantic_projection_version"] or 0) < 1,
                    semantic_projection_version=1,
                )
            elif updates and has_semantic_corrections:
                connection.execute(
                    update(sessions)
                    .where(sessions.c.session_id == session_key)
                    .values(semantic_projection_version=0, commit_seq=commit_seq, updated_at=commit_time)
                )
            reopened_projectors = 0
            if complete_after and has_semantic_corrections:
                reopened_projectors = int(
                    connection.execute(
                        update(projectors)
                        .where(
                            projectors.c.session_id == session_key,
                            # Was `== "quarantined"`. Repair republishes the
                            # evidence a failure was reached against, so any
                            # failure status is stale here, not just the one
                            # that used to be terminal.
                            projectors.c.status.in_(("failed", "quarantined")),
                        )
                        .values(
                            desired_revision=func.max(projectors.c.desired_revision, commit_seq),
                            desired_at=commit_time,
                            status="idle",
                            failure_count=0,
                            last_error_code=None,
                            last_error_message=None,
                            retry_at=None,
                            commit_seq=commit_seq,
                            updated_at=commit_time,
                        )
                    ).rowcount
                    or 0
                )
            return {
                "changed": bool(updates or needs_recompute),
                "complete": complete_after,
                "updated_object_count": len(updates),
                "reopened_projectors": reopened_projectors,
                "session": _storage_session_dto(
                    connection.execute(select(sessions).where(sessions.c.session_id == session_key)).mappings().one()
                ),
                "commit_seq": str(commit_seq),
            }


def _retire_session_render(connection, session_key: str, observed_at: datetime) -> tuple[int, int, int]:
    """Hide one session's render without deleting anything.

    Render objects are marked retired and generations superseded, the session is
    hidden and ``retired``, and projectors are re-queued so search drops it. Raw
    objects stay active.
    """

    sessions = StorageSession.__table__
    render_objects = RenderObject.__table__
    generations = RenderGeneration.__table__
    projector_state = ProjectorState.__table__
    commit_seq = _advance_commit_seq(connection, observed_at)
    retired_render = connection.execute(
        update(render_objects)
        .where(render_objects.c.session_id == session_key, render_objects.c.retired_at.is_(None))
        .values(retired_at=observed_at, retirement_revision=commit_seq)
    ).rowcount
    retired_generations = connection.execute(
        update(generations)
        .where(generations.c.session_id == session_key, generations.c.state != "superseded")
        .values(state="superseded", superseded_at=observed_at, commit_seq=commit_seq, updated_at=observed_at)
    ).rowcount
    connection.execute(
        update(sessions)
        .where(sessions.c.session_id == session_key)
        .values(
            hidden_from_default_timeline=1,
            render_state="retired",
            commit_seq=commit_seq,
            updated_at=observed_at,
        )
    )
    for projector_name in KNOWN_PROJECTORS:
        state = (
            connection.execute(
                select(projector_state).where(
                    projector_state.c.projector == projector_name,
                    projector_state.c.session_id == session_key,
                )
            )
            .mappings()
            .first()
        )
        values = {
            "desired_revision": commit_seq,
            "desired_at": observed_at,
            "claimed_revision": None,
            "claim_token": None,
            "worker_id": None,
            "claim_expires_at": None,
            "status": "idle",
            "failure_count": 0,
            "last_error_code": None,
            "last_error_message": None,
            "retry_at": None,
            "commit_seq": commit_seq,
            "updated_at": observed_at,
        }
        if state is None:
            connection.execute(
                insert(projector_state).values(
                    projector=projector_name,
                    session_id=session_key,
                    completed_revision=0,
                    created_at=observed_at,
                    **values,
                )
            )
        else:
            connection.execute(
                update(projector_state)
                .where(
                    projector_state.c.projector == projector_name,
                    projector_state.c.session_id == session_key,
                )
                .values(**values)
            )
    return commit_seq, int(retired_render or 0), int(retired_generations or 0)
