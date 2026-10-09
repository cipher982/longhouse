"""CatalogStore: storage-v2 ingest: source epochs, raw objects and media objects."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete
from sqlalchemy import func
from sqlalchemy import insert
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import update

from zerg.catalogd.fact_reducer import _advance_commit_seq
from zerg.catalogd.fact_reducer import _current_commit_seq
from zerg.catalogd.models import MediaObject
from zerg.catalogd.models import ProjectorState
from zerg.catalogd.models import RawObject as LiveRawObject
from zerg.catalogd.models import RenderGeneration
from zerg.catalogd.models import RenderObject
from zerg.catalogd.models import SessionMediaRef
from zerg.catalogd.models import SessionProviderFact
from zerg.catalogd.models import SessionTombstone as LiveSessionTombstone
from zerg.catalogd.models import SourceEpoch as LiveSourceEpoch
from zerg.catalogd.models import StorageSession

# store.py imports this module after defining these, at its end.
from zerg.catalogd.store import KNOWN_PROJECTORS
from zerg.catalogd.store import _apply_delegation_lineage
from zerg.catalogd.store import _as_aware_utc
from zerg.catalogd.store import _bind_orphan_subagents_to_parent
from zerg.catalogd.store import _delegation_native_ids
from zerg.catalogd.store import _delegation_parent_id
from zerg.catalogd.store import _insert_provider_facts
from zerg.catalogd.store import _machine_is_automation
from zerg.catalogd.store import _maximum_order_key
from zerg.catalogd.store import _media_object_dto
from zerg.catalogd.store import _media_ref_dto
from zerg.catalogd.store import _minimum_order_key
from zerg.catalogd.store import _normalized_parent_source_id
from zerg.catalogd.store import _raw_object_manifest_dto
from zerg.catalogd.store import _raw_object_matches
from zerg.catalogd.store import _raw_object_receipt
from zerg.catalogd.store import _read_snapshot
from zerg.catalogd.store import _recompute_render_generation_projection
from zerg.catalogd.store import _render_order_columns
from zerg.catalogd.store import _resolve_session_id_by_provider_session_id
from zerg.catalogd.store import _resolve_session_id_by_source_path
from zerg.catalogd.store import _session_keeps_published_render
from zerg.catalogd.store import _source_epoch_conflict
from zerg.catalogd.store import _source_epoch_dto
from zerg.catalogd.store import _StageTimer
from zerg.catalogd.store import _storage_title_candidate_clause
from zerg.catalogd.store import _u64_key
from zerg.catalogd.store import _write_transaction
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionLivePreview
from zerg.models.live_store import LiveSessionThread
from zerg.models.live_store import LiveSessionThreadAlias
from zerg.models.live_store import LiveTimelineCard
from zerg.services.internal_sessions import classify_provider_proof_environment
from zerg.services.internal_sessions import is_factory_title_assurance_session
from zerg.services.session_title import sanitize_timeline_title
from zerg.services.session_visibility_policy import SessionVisibilityFacts
from zerg.services.session_visibility_policy import evaluate_origin_visibility
from zerg.services.session_visibility_policy import primary_worker_only_clause
from zerg.storage_v2.contracts import EnvelopeIdentity
from zerg.storage_v2.contracts import envelope_id as compute_envelope_id


class IngestMixin:
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
        conversation_resets: tuple[dict[str, Any], ...] = (),
        provider_facts: tuple[dict[str, Any], ...] = (),
    ) -> dict[str, Any]:
        del protocol_version  # validated as v2 by the RPC boundary
        timer = _StageTimer("commit_raw_object")
        timer.annotate(
            provider=provider,
            record_count=len(record_hashes),
            render_manifest=1 if render_manifest is not None else 0,
            source_replacement=1 if predecessor_source_epoch is not None else 0,
        )
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
        live_timeline_card = LiveTimelineCard.__table__
        live_session_thread = LiveSessionThread.__table__
        live_session_preview = LiveSessionLivePreview.__table__
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
        timer.mark("prepare")
        with _write_transaction(self.engine, timer=timer) as connection:
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
                # An exact replay may carry facts a pre-facts engine never
                # shipped; keep them without moving the raw receipt.
                _insert_provider_facts(
                    connection,
                    session_id=session_key,
                    source_epoch=str(source_epoch),
                    provider_facts=provider_facts,
                    commit_seq=int(_current_commit_seq(connection)),
                    now=datetime.now(UTC),
                )
                existing_storage = (
                    connection.execute(select(storage_session).where(storage_session.c.session_id == session_key)).mappings().first()
                )
                exact_owner = str((existing_storage or {}).get("owner_id") or owner_id or "") or None
                exact_native_ids = _delegation_native_ids(provider_facts=provider_facts, session_facts=session_facts)
                primary_thread_id = connection.execute(
                    select(LiveSessionCatalog.__table__.c.primary_thread_id).where(LiveSessionCatalog.__table__.c.session_id == session_key)
                ).scalar_one_or_none()
                if primary_thread_id:
                    alias_table = LiveSessionThreadAlias.__table__
                    alias_seen_at = _as_aware_utc(session_facts["last_activity_at"]) or datetime.now(UTC)
                    for alias_value in exact_native_ids:
                        existing_alias = connection.execute(
                            select(alias_table.c.id, alias_table.c.thread_id)
                            .where(
                                alias_table.c.provider == provider,
                                alias_table.c.alias_kind == "provider_session_id",
                                alias_table.c.alias_value == alias_value,
                            )
                            .limit(1)
                        ).first()
                        if existing_alias is None:
                            connection.execute(
                                insert(alias_table).values(
                                    thread_id=str(primary_thread_id),
                                    provider=provider,
                                    alias_kind="provider_session_id",
                                    alias_value=alias_value,
                                    first_seen_at=alias_seen_at,
                                    last_seen_at=alias_seen_at,
                                )
                            )
                        elif str(existing_alias.thread_id) == str(primary_thread_id):
                            connection.execute(
                                update(alias_table)
                                .where(alias_table.c.id == existing_alias.id)
                                .values(last_seen_at=func.max(alias_table.c.last_seen_at, alias_seen_at))
                            )
                _apply_delegation_lineage(
                    connection,
                    session_id=session_key,
                    provider=provider,
                    owner_id=exact_owner,
                    machine_id=machine_id,
                    native_ids=exact_native_ids,
                    provider_facts=provider_facts,
                    session_facts=session_facts,
                    commit_seq=int(_current_commit_seq(connection)),
                    commit_time=datetime.now(UTC),
                )
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
            live_catalog_session = (
                connection.execute(select(live_session_catalog).where(live_session_catalog.c.session_id == session_key)).mappings().first()
            )
            live_console_session = (
                live_catalog_session if live_catalog_session is not None and live_catalog_session["origin_kind"] == "console" else None
            )
            effective_owner_id = owner_id
            if existing_session is None:
                resolved_owner_id = self._resolve_session_owner_id(connection, session_id=session_key)
                if owner_id is not None and resolved_owner_id is not None and str(owner_id) != resolved_owner_id:
                    return {
                        "source_epoch_conflict": True,
                        "commit_seq": str(_current_commit_seq(connection)),
                        "conflict_details": {"reason": "session_owner_conflict"},
                    }
                # A missing ingest owner may still have an explicit live or
                # launch binding. Carry that authority into the first durable
                # row; leave genuinely unbound archive ingestion unbound.
                if owner_id is None:
                    effective_owner_id = resolved_owner_id
            # Gemini was retired as a product provider in favor of
            # Antigravity, but legacy Gemini JSON imports retain their original
            # immutable session rows. Let the canonical Antigravity source
            # converge onto that exact legacy identity. This is intentionally
            # one-way: a new Antigravity session must not admit an old Gemini
            # writer, and all unrelated cross-provider collisions stay fenced.
            legacy_antigravity_alias = (
                existing_session is not None
                and str(existing_session["provider"]).strip().lower() == "gemini"
                and provider.strip().lower() == "antigravity"
            )
            if existing_session is not None and (
                existing_session["tenant_id"] != tenant_id or (existing_session["provider"] != provider and not legacy_antigravity_alias)
            ):
                return _source_epoch_conflict(
                    connection,
                    reason="session_identity_conflict",
                    existing_tenant_id=str(existing_session["tenant_id"]),
                    requested_tenant_id=tenant_id,
                    existing_provider=str(existing_session["provider"]),
                    requested_provider=provider,
                )
            if (
                existing_session is not None
                and existing_session["owner_id"] is not None
                and owner_id is not None
                and str(existing_session["owner_id"]) != str(owner_id)
            ):
                # The ingest path may optimistically classify a first
                # envelope when the owner-scoped manifest lookup says the
                # session is absent. Make the ownership boundary authoritative
                # inside this transaction so that an owner-mismatched session
                # can never be claimed or rewritten by that relaxation.
                return {
                    "source_epoch_conflict": True,
                    "commit_seq": str(_current_commit_seq(connection)),
                    "conflict_details": {"reason": "session_owner_conflict"},
                }

            existing_generation = None
            if render_manifest is not None:
                generation_key = str(render_manifest["generation_id"])
                existing_generation = (
                    connection.execute(select(render_generation).where(render_generation.c.generation_id == generation_key))
                    .mappings()
                    .first()
                )
                revision_generation = (
                    connection.execute(
                        select(render_generation).where(
                            render_generation.c.session_id == session_key,
                            render_generation.c.parser_revision == render_manifest["parser_revision"],
                            render_generation.c.ordering_revision == render_manifest["ordering_revision"],
                        )
                    )
                    .mappings()
                    .first()
                )
                if revision_generation is not None and revision_generation["generation_id"] != generation_key:
                    return {
                        "source_epoch_conflict": True,
                        "commit_seq": str(_current_commit_seq(connection)),
                        "conflict_details": {
                            "reason": "render_generation_revision_conflict",
                            "existing_generation_id": str(revision_generation["generation_id"]),
                            "requested_generation_id": generation_key,
                            "parser_revision": str(render_manifest["parser_revision"]),
                            "ordering_revision": str(render_manifest["ordering_revision"]),
                        },
                    }
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
                        # Name the epoch that IS open. A shipper whose local
                        # registry was lost or salvaged can name a predecessor
                        # the host never saw; without this it can prove only
                        # that its own lineage is wrong, not what to adopt, and
                        # the source stays blocked until a human intervenes.
                        return _source_epoch_conflict(
                            connection,
                            reason="predecessor_not_open_for_this_identity",
                            predecessor_exists=predecessor_row is not None,
                            expected_predecessor=expected_predecessor,
                            predecessor_state=(predecessor_row["state"] if predecessor_row is not None else None),
                            open_source_epochs=[str(row["source_epoch"]) for row in open_rows],
                        )
                    if any(row["source_epoch"] != expected_predecessor for row in open_rows):
                        return _source_epoch_conflict(
                            connection,
                            reason="open_epoch_is_not_the_named_predecessor",
                            expected_predecessor=expected_predecessor,
                            open_source_epochs=[str(row["source_epoch"]) for row in open_rows],
                        )
                elif open_rows:
                    # The branch that produced the 2026-08-04 incident: an
                    # initial epoch arrives while this identity already has an
                    # open one. It returned no reason at all, so the shipper
                    # could only report that a later manifest probe 404'd.
                    return _source_epoch_conflict(
                        connection,
                        reason="another_epoch_is_already_open_for_this_source",
                        open_source_epochs=[str(row["source_epoch"]) for row in open_rows],
                    )
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
                # Every accepted append is already fenced to the current
                # accepted_through value. Replaying every historical range to
                # derive the same watermark made the Nth append O(N), turning
                # long Cursor histories into quadratic writer stalls. The
                # epoch/range index can resolve the durable tail directly.
                last_range_end = connection.execute(
                    select(raw.c.range_end)
                    .where(raw.c.source_epoch == epoch_key, raw.c.retired_at.is_(None))
                    .order_by(raw.c.range_start.desc(), raw.c.range_end.desc())
                    .limit(1)
                ).scalar_one_or_none()
                accepted_through = (
                    range_start_key
                    if last_range_end is None and int(epoch_row["object_count"] or 0) == 0
                    else str(last_range_end or epoch_row["accepted_through"])
                )
                timer.annotate(epoch_object_count=int(epoch_row["object_count"] or 0))
                if last_range_end is not None and accepted_through != str(epoch_row["accepted_through"]):
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

            timer.mark("validate")

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
                    predecessor_envelopes = select(raw.c.envelope_id).where(raw.c.source_epoch == expected_predecessor)
                    replaced_session_ids = {
                        str(value)
                        for value in connection.execute(
                            select(raw.c.session_id)
                            .where(
                                raw.c.source_epoch == expected_predecessor,
                                raw.c.retired_at.is_(None),
                                raw.c.session_id != session_key,
                            )
                            .distinct()
                        ).scalars()
                    }
                    retired_raw = connection.execute(
                        update(raw)
                        .where(raw.c.source_epoch == expected_predecessor, raw.c.retired_at.is_(None))
                        .values(retired_at=commit_time, retirement_revision=commit_seq)
                    ).rowcount
                    retired_render = connection.execute(
                        update(render_object)
                        .where(
                            render_object.c.source_envelope_id.in_(predecessor_envelopes),
                            render_object.c.retired_at.is_(None),
                        )
                        .values(retired_at=commit_time, retirement_revision=commit_seq)
                    ).rowcount
                    retired_media = connection.execute(
                        update(session_media_ref)
                        .where(
                            session_media_ref.c.envelope_id.in_(predecessor_envelopes),
                            session_media_ref.c.state == "active",
                        )
                        .values(
                            state="retired",
                            retired_at=commit_time,
                            deletion_revision=commit_seq,
                            commit_seq=commit_seq,
                        )
                    ).rowcount
                    # Facts derive from the predecessor's bytes; a replacement
                    # that dropped turns must not keep their durations, usage,
                    # recap or title eligible for the session's projections.
                    retired_facts = connection.execute(
                        delete(SessionProviderFact.__table__).where(SessionProviderFact.__table__.c.source_epoch == expected_predecessor)
                    ).rowcount
                    timer.annotate(
                        retired_raw=int(retired_raw or 0),
                        retired_render=int(retired_render or 0),
                        retired_media=int(retired_media or 0),
                        retired_facts=int(retired_facts or 0),
                        replaced_sessions=len(replaced_session_ids),
                    )
                    timer.mark("retire_predecessor")
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
                            projector_table = ProjectorState.__table__
                            for projector_name in KNOWN_PROJECTORS:
                                connection.execute(
                                    update(projector_table)
                                    .where(
                                        projector_table.c.projector == projector_name,
                                        projector_table.c.session_id == replaced_session_id,
                                    )
                                    .values(
                                        desired_revision=commit_seq,
                                        desired_at=commit_time,
                                        claimed_revision=None,
                                        claim_token=None,
                                        worker_id=None,
                                        claim_expires_at=None,
                                        status="idle",
                                        retry_at=None,
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
            timer.mark("write_manifests")
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
                "owner_id": effective_owner_id,
                "provider_session_id": session_facts.get("provider_session_id")
                or (existing_session.get("provider_session_id") if existing_session is not None else None),
                "environment": session_facts["environment"],
                "project": session_facts["project"],
                "cwd": session_facts["cwd"],
                "git_repo": session_facts["git_repo"],
                "git_branch": session_facts["git_branch"],
                "provider_version": session_facts.get("provider_version"),
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
            # Launch provenance is sticky: an envelope that carries no actor
            # never clears one the session already recorded. Only then may an
            # automation credential fill the gap
            # (docs/specs/automation-machine-credentials.md).
            if not session_values["launch_actor"]:
                # Recorded on either row counts: a live-only session (no archived
                # row yet) records its actor on the live catalog alone.
                recorded = next(
                    (row for row in (existing_session, live_catalog_session) if row is not None and row.get("launch_actor")),
                    None,
                )
                if recorded is not None:
                    session_values["launch_actor"] = recorded["launch_actor"]
                    session_values["launch_surface"] = session_values["launch_surface"] or recorded.get("launch_surface")
                elif (
                    live_console_session is None
                    and not (existing_session is not None and existing_session["origin_kind"] == "console")
                    and _machine_is_automation(connection, owner_id=effective_owner_id, machine_id=machine_id)
                ):
                    session_values["launch_actor"] = "automation"
                if session_values["launch_actor"]:
                    # Whichever fallback supplied it, a live row with no actor of
                    # its own takes the same one, so a later full visibility
                    # reconcile reads the same provenance from every row.
                    for table in (live_session_catalog, live_timeline_card):
                        connection.execute(
                            update(table)
                            .where(table.c.session_id == session_key, table.c.launch_actor.is_(None))
                            .values(launch_actor=session_values["launch_actor"], updated_at=commit_time)
                        )
            if render_manifest is None and _session_keeps_published_render(connection, existing_session):
                # This envelope's `render_state` is a receipt about the envelope
                # (no render attached), not a verdict on the session. A commit
                # with nothing to render cannot unpublish the render the session
                # already serves.
                del session_values["render_state"]
            proof_environment = classify_provider_proof_environment(
                cwd=session_facts["cwd"],
                machine_id=machine_id,
            )
            provider_automation = (
                proof_environment == "test"
                and not is_factory_title_assurance_session(
                    provider=provider,
                    environment=session_facts["environment"],
                    project=session_facts["project"],
                    cwd=session_facts["cwd"],
                    machine_id=machine_id,
                    origin_kind=session_facts["origin_kind"],
                    hidden_from_default_timeline=session_facts["hidden_from_default_timeline"],
                    launch_actor=session_facts["launch_actor"],
                    launch_surface=session_facts["launch_surface"],
                )
                and live_console_session is None
                and not (existing_session is not None and existing_session["origin_kind"] == "console")
                and not (live_catalog_session is not None and live_catalog_session["origin_kind"] == "console")
            )
            hatch_automation = (
                (
                    (existing_session is not None and existing_session["origin_kind"] == "hatch_automation")
                    or (live_catalog_session is not None and live_catalog_session["origin_kind"] == "hatch_automation")
                )
                and live_console_session is None
                and not (existing_session is not None and existing_session["origin_kind"] == "console")
            )
            retained_launch_provenance = bool(
                live_catalog_session is not None and live_catalog_session["launch_actor"] and live_catalog_session["launch_surface"]
            )
            retained_test_policy = bool(live_catalog_session is not None and live_catalog_session["origin_kind"] == "test_or_canary")
            if retained_launch_provenance:
                # Registration owns managed launch provenance. A later provider
                # transcript may look like factory automation from its cwd or
                # prompt, but it cannot turn a human Helm launch into an
                # automation launch (or vice versa) before Resume validates it.
                session_values.update(
                    origin_kind=live_catalog_session["origin_kind"],
                    hidden_from_default_timeline=int(live_catalog_session["hidden_from_default_timeline"] or 0),
                    launch_actor=live_catalog_session["launch_actor"],
                    launch_surface=live_catalog_session["launch_surface"],
                )
                if live_catalog_session["launch_actor"] == "automation":
                    session_values["environment"] = "test"
            elif retained_test_policy:
                # Older canaries may predate launch provenance. Preserve their
                # already-retained policy even when a later transcript no
                # longer carries the prompt/cwd heuristic that found them.
                session_values.update(
                    environment=live_catalog_session["environment"],
                    project=live_catalog_session["project"],
                    cwd=live_catalog_session["cwd"],
                    git_repo=live_catalog_session["git_repo"],
                    git_branch=live_catalog_session["git_branch"],
                    origin_kind=live_catalog_session["origin_kind"],
                    hidden_from_default_timeline=int(live_catalog_session["hidden_from_default_timeline"] or 0),
                    launch_actor=live_catalog_session["launch_actor"],
                    launch_surface=live_catalog_session["launch_surface"],
                )
            elif provider_automation:
                session_values.update(
                    environment="test",
                    origin_kind="test_or_canary",
                    hidden_from_default_timeline=1,
                    launch_actor="automation",
                    launch_surface="test",
                )
                for table in (live_session_catalog, live_timeline_card):
                    connection.execute(
                        update(table)
                        .where(table.c.session_id == session_key)
                        .values(
                            environment="test",
                            origin_kind="test_or_canary",
                            hidden_from_default_timeline=1,
                            launch_actor="automation",
                            launch_surface="test",
                            updated_at=commit_time,
                        )
                    )
                connection.execute(
                    update(live_session_thread)
                    .where(live_session_thread.c.session_id == session_key)
                    .values(
                        origin_kind="test_or_canary",
                        hidden_from_default_timeline=1,
                        updated_at=commit_time,
                    )
                )
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
            if render_manifest is not None and int(render_manifest["assistant_messages"] or 0) > 0:
                # The live bridge preview and storage-v2 render are two views
                # of the same provider output. Once a render at least as new as
                # the preview is durable, retire the provisional row in this
                # same catalog commit. Historical repair envelopes must not
                # erase a newer live turn.
                durable_at = _as_aware_utc(session_facts["last_activity_at"])
                if durable_at is not None:
                    connection.execute(
                        update(live_session_preview)
                        .where(
                            live_session_preview.c.session_id == session_key,
                            live_session_preview.c.superseded_at.is_(None),
                            live_session_preview.c.preview_observed_at <= durable_at,
                        )
                        .values(
                            superseded_at=durable_at,
                            superseded_by_event_id=None,
                            superseded_reason="superseded_by_storage_v2",
                            preview_updated_at=commit_time,
                        )
                    )
            if hatch_automation and not retained_launch_provenance:
                session_values.update(
                    origin_kind="hatch_automation",
                    hidden_from_default_timeline=1,
                    launch_actor="automation",
                    launch_surface="hatch",
                )
                connection.execute(
                    update(live_session_catalog)
                    .where(live_session_catalog.c.session_id == session_key)
                    .values(
                        origin_kind="hatch_automation",
                        hidden_from_default_timeline=1,
                        launch_actor="automation",
                        launch_surface="hatch",
                        updated_at=commit_time,
                    )
                )
                connection.execute(
                    update(live_timeline_card)
                    .where(live_timeline_card.c.session_id == session_key)
                    .values(
                        origin_kind="hatch_automation",
                        hidden_from_default_timeline=1,
                        launch_actor="automation",
                        launch_surface="hatch",
                        updated_at=commit_time,
                    )
                )
            # Subagent lineage. The engine observes that a transcript is an
            # in-harness worker; nothing downstream knew, because the storage-v2
            # contract carried no lineage and the visibility recomputation below
            # reads only the primary thread's branch_kind. Mark the thread here
            # and the existing policy hides the row on its own — one authority,
            # not a second visibility fact.
            #
            # The parent pointer is provider evidence. Resolving it to a
            # Longhouse session is this side's job, through native aliases or
            # the source identity hash, and it can legitimately fail: a child
            # routinely ships before its parent. Keep the raw pointer either
            # way so a later arrival can bind it.
            # A parent pointer and a hidden worker are two different facts, and
            # gating the first on the second meant a plain fork -- which the
            # shipper now sends with a parent and `is_subagent` false -- lost its
            # parent on arrival. Persist the edge whenever one is offered; let
            # `is_subagent` say only whether this row is a worker.
            source_native_ids = _delegation_native_ids(provider_facts=provider_facts, session_facts=session_facts)
            parent_provider_id = _delegation_parent_id(provider_facts=provider_facts, session_facts=session_facts)
            parent_source_id = _normalized_parent_source_id(parent_provider_id or "")
            if parent_provider_id and parent_provider_id not in source_native_ids:
                # OMP's parentSession is an absolute JSONL path, while other
                # providers generally send a native session id. Try the native
                # alias first, then the engine's durable path-sha256 identity.
                resolved_parent = _resolve_session_id_by_provider_session_id(
                    connection,
                    provider=provider,
                    provider_session_id=parent_provider_id,
                    owner_id=effective_owner_id,
                    machine_id=machine_id,
                )
                if resolved_parent is None:
                    resolved_parent = _resolve_session_id_by_source_path(
                        connection,
                        provider=provider,
                        source_path=parent_provider_id,
                        owner_id=effective_owner_id,
                        machine_id=machine_id,
                    )
                existing_parent_provider_id = (
                    str(existing_session.get("subagent_parent_provider_session_id") or "").strip() if existing_session is not None else ""
                )
                existing_parent_session_id = (
                    str(existing_session.get("subagent_parent_session_id") or "").strip() if existing_session is not None else ""
                )
                # Preserve the raw provider pointer exactly as received. A
                # source cannot parent itself, and conflicting durable lineage
                # is never overwritten; provider facts retain the evidence.
                if (
                    parent_provider_id != session_key
                    and resolved_parent != session_key
                    and not existing_parent_provider_id
                    and not existing_parent_session_id
                ):
                    session_values["subagent_parent_provider_session_id"] = parent_provider_id
                    if parent_source_id is not None:
                        session_values["subagent_parent_source_id"] = parent_source_id
                    if resolved_parent is not None:
                        session_values["subagent_parent_session_id"] = resolved_parent
            if bool(session_facts.get("is_subagent")):
                session_values.setdefault("subagent_parent_provider_session_id", None)
                session_values.setdefault("subagent_parent_session_id", None)
                session_values["subagent_parent_tool_call_id"] = str(session_facts.get("parent_tool_call_id") or "").strip() or None
                session_values["subagent_run_id"] = str(session_facts.get("workflow_run_id") or "").strip() or None
                session_values["is_subagent"] = 1

            primary_branch_kind = connection.execute(
                select(live_session_thread.c.branch_kind).where(
                    live_session_thread.c.session_id == session_key,
                    live_session_thread.c.is_primary == 1,
                )
            ).scalar_one_or_none()
            canonical_hidden = evaluate_origin_visibility(
                SessionVisibilityFacts(
                    provider=session_values.get("provider", provider),
                    project=session_values.get("project"),
                    environment=session_values.get("environment"),
                    origin_kind=session_values.get("origin_kind"),
                    launch_actor=session_values.get("launch_actor"),
                    launch_surface=session_values.get("launch_surface"),
                    cwd=session_values.get("cwd"),
                    machine_id=machine_id,
                    primary_thread_is_worker_only=primary_branch_kind == "subagent",
                    is_subagent=bool(
                        session_values.get("is_subagent", existing_session.get("is_subagent") if existing_session is not None else False)
                    ),
                )
            ).system_hidden
            session_values["hidden_from_default_timeline"] = int(canonical_hidden)
            for policy_table in (live_session_catalog, live_timeline_card):
                connection.execute(
                    update(policy_table)
                    .where(policy_table.c.session_id == session_key)
                    .values(hidden_from_default_timeline=int(canonical_hidden), updated_at=commit_time)
                )
            connection.execute(
                update(live_session_thread)
                .where(
                    live_session_thread.c.session_id == session_key,
                    live_session_thread.c.is_primary == 1,
                )
                .values(hidden_from_default_timeline=int(canonical_hidden), updated_at=commit_time)
            )
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
                    # A null incoming version never clears what an earlier ingest recorded.
                    "provider_version",
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
            timer.mark("project_session")
            alias_values: list[str] = list(source_native_ids)
            # Rotation capture (spec group C): a conversation_reset boundary
            # means the provider rotated its native session id inside the same
            # transcript (raw `claude --resume` outside Longhouse). Aliasing the
            # new id here is what makes it resolvable via the group-A read path
            # (session.alias.resolve.v2); before this the id existed only hashed
            # inside the reset record's event_id. Cross-thread collisions cannot
            # crash: the live alias table is unique only per
            # (thread_id, provider, alias_kind, alias_value), and readers order
            # duplicates by last_seen_at with primary-key lookups winning first.
            # Remaining fork-linkage work (deliberately not built here): the
            # live serving path has no SessionEdge equivalent and hardcodes
            # continued_from_session_id=None (services/storage_v2_workspace.py),
            # and the archive-side SessionEdge/lineage projection
            # (session_kernel_projection._source_session_id_for_thread) lives in
            # a different database this single-writer commit cannot reach.
            # Wiring reset linkage into served lineage therefore needs an
            # archive-outbox projection or a live edge store — new machinery,
            # tracked in the session-identity spec, not smuggled into ingest.
            for reset in conversation_resets:
                rotated = str(reset.get("provider_session_id") or "").strip()
                if rotated and rotated not in alias_values:
                    alias_values.append(rotated)
            if alias_values:
                primary_thread_id = connection.execute(
                    select(live_session_catalog.c.primary_thread_id).where(live_session_catalog.c.session_id == session_key)
                ).scalar_one_or_none()
                if primary_thread_id:
                    alias_table = LiveSessionThreadAlias.__table__
                    alias_seen_at = _as_aware_utc(session_facts["last_activity_at"]) or commit_time
                    for alias_value in alias_values:
                        # The routing index makes (provider, alias_value) unique
                        # across threads for provider_session_id, so a row on
                        # another thread is a conflict, not an insert. Existing
                        # thread wins — same semantics as the binding_signal and
                        # launch writers; an ON CONFLICT keyed on the per-thread
                        # constraint would trip the routing index and fail the
                        # whole storage commit.
                        existing = connection.execute(
                            select(alias_table.c.id, alias_table.c.thread_id)
                            .where(alias_table.c.provider == provider)
                            .where(alias_table.c.alias_kind == "provider_session_id")
                            .where(alias_table.c.alias_value == alias_value)
                            .order_by(alias_table.c.id.asc())
                            .limit(1)
                        ).first()
                        if existing is None:
                            connection.execute(
                                insert(alias_table).values(
                                    thread_id=str(primary_thread_id),
                                    provider=provider,
                                    alias_kind="provider_session_id",
                                    alias_value=alias_value,
                                    first_seen_at=alias_seen_at,
                                    last_seen_at=alias_seen_at,
                                )
                            )
                        elif str(existing.thread_id) == str(primary_thread_id):
                            connection.execute(
                                update(alias_table)
                                .where(alias_table.c.id == existing.id)
                                .values(last_seen_at=func.max(alias_table.c.last_seen_at, alias_seen_at))
                            )
                        else:
                            logging.getLogger(__name__).warning(
                                "Provider session alias routing conflict during storage commit: "
                                "provider=%s alias_value=%s existing_thread_id=%s requested_thread_id=%s",
                                provider,
                                alias_value,
                                existing.thread_id,
                                primary_thread_id,
                            )
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
                        abandoned_events=render_manifest.get("abandoned_events"),
                        first_user_message_preview=render_manifest["first_user_message_preview"],
                        last_visible_text_preview=render_manifest["last_visible_text_preview"],
                        semantic_projection_version=render_manifest.get("semantic_projection_version", 0),
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
                        "semantic_projection_version": (
                            0
                            if provider.strip().lower() == "claude"
                            else (
                                int(render_manifest.get("semantic_projection_version", 0))
                                if existing_session is None or existing_session.get("current_render_generation") is None
                                else min(
                                    int(existing_session.get("semantic_projection_version") or 0),
                                    int(render_manifest.get("semantic_projection_version", 0)),
                                )
                            )
                        ),
                    }
                    immediate_title = sanitize_timeline_title(render_manifest["first_user_message_preview"], max_words=6)
                    if provider.strip().lower() != "claude" and not (existing_session or {}).get("summary_title") and immediate_title:
                        projection_values["summary_title"] = immediate_title
                    if not (existing_session or {}).get("first_user_message_preview") and render_manifest["first_user_message_preview"]:
                        projection_values["first_user_message_preview"] = render_manifest["first_user_message_preview"]
                    if render_manifest["last_visible_text_preview"]:
                        projection_values["last_visible_text_preview"] = render_manifest["last_visible_text_preview"]
                    connection.execute(
                        update(storage_session).where(storage_session.c.session_id == session_key).values(**projection_values)
                    )
            # This session may be the parent a previously-shipped worker named
            # but could not resolve. Its raw source identity is only now known,
            # so adopt those orphans in the same commit.
            _bind_orphan_subagents_to_parent(
                connection,
                provider=provider,
                session_key=session_key,
                alias_values=alias_values,
                parent_source_id=opaque_source_id,
                owner_id=effective_owner_id,
                machine_id=machine_id,
                commit_seq=commit_seq,
                commit_time=commit_time,
            )
            timer.mark("project_render")
            generation_replaced = bool(
                render_manifest is not None
                and existing_session is not None
                and existing_session.get("current_render_generation") is not None
                and str(existing_session["current_render_generation"]) != str(render_manifest["generation_id"])
            )
            if (predecessor_row is not None or generation_replaced) and render_state == "ready":
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
                            desired_at=commit_time,
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
                            desired_at=commit_time,
                            commit_seq=commit_seq,
                            updated_at=commit_time,
                        )
                    )
            _insert_provider_facts(
                connection,
                session_id=session_key,
                source_epoch=str(source_epoch),
                provider_facts=provider_facts,
                commit_seq=commit_seq,
                now=commit_time,
            )
            _apply_delegation_lineage(
                connection,
                session_id=session_key,
                provider=provider,
                owner_id=effective_owner_id,
                machine_id=machine_id,
                native_ids=alias_values,
                provider_facts=provider_facts,
                commit_seq=commit_seq,
                commit_time=commit_time,
                session_facts=session_facts,
            )
            delegation_parent_session_id = None
            if effective_owner_id is not None:
                parent_session_id = connection.execute(
                    select(storage_session.c.subagent_parent_session_id).where(
                        storage_session.c.session_id == session_key,
                        or_(
                            storage_session.c.is_subagent == 1,
                            primary_worker_only_clause(storage_session, LiveSessionThread.__table__),
                        ),
                    )
                ).scalar_one_or_none()
                if parent_session_id is not None:
                    delegation_parent_session_id = connection.execute(
                        select(storage_session.c.session_id).where(
                            storage_session.c.session_id == parent_session_id,
                            storage_session.c.owner_id == str(effective_owner_id),
                            storage_session.c.provider == provider,
                            storage_session.c.machine_id == machine_id,
                        )
                    ).scalar_one_or_none()
            timer.mark("projector_state")
            row = connection.execute(select(raw).where(raw.c.envelope_id == envelope_id)).mappings().one()
            title_generation_required = bool(
                connection.execute(
                    select(StorageSession.__table__.c.session_id)
                    .where(
                        StorageSession.__table__.c.session_id == session_key,
                        _storage_title_candidate_clause(StorageSession.__table__, observed_at=commit_time),
                    )
                    .limit(1)
                ).first()
            )
            return {
                "created": True,
                "exact_replay": False,
                "receipt": _raw_object_receipt(row),
                "title_generation_required": title_generation_required,
                "delegation_parent_session_id": (str(delegation_parent_session_id) if delegation_parent_session_id is not None else None),
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
        thumb_hash: str | None = None,
        derived_from: str | None = None,
        width: int | None = None,
        height: int | None = None,
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
                    # A preview is named by no session, so the reference check
                    # above cannot see it. Anything a still-live image points at
                    # is still in use: retiring it would leave that image
                    # advertising bytes nobody has.
                    still_named = connection.execute(
                        select(media.c.media_hash).where(media.c.thumb_hash == media_hash, media.c.state != "deleted").limit(1)
                    ).first()
                    if still_named is not None:
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
                        existing["thumb_hash"] is None and thumb_hash is not None,
                        existing["derived_from"] is None and derived_from is not None,
                        existing["width"] is None and width is not None,
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
                        thumb_hash=thumb_hash,
                        derived_from=derived_from,
                        width=width,
                        height=height,
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
                        thumb_hash=existing["thumb_hash"] or thumb_hash,
                        derived_from=existing["derived_from"] or derived_from,
                        width=existing["width"] or width,
                        height=existing["height"] or height,
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
        owner_id: str,
        limit: int,
    ) -> dict[str, Any]:
        media = MediaObject.__table__
        refs = SessionMediaRef.__table__
        observed_at = datetime.now(UTC)
        with _read_snapshot(self.engine) as connection:
            owned_session_ids = select(StorageSession.__table__.c.session_id).where(StorageSession.__table__.c.owner_id == owner_id)
            # A derived preview is referenced by no session of its own, so a
            # reader is authorized for it through the image that names it - and
            # only when the preview names that same image back. A hash is an
            # identifier, never authority: naming someone else's bytes as your
            # own preview must grant nothing.
            parent = media.alias("parent")
            preview = media.alias("preview")
            preview_of = (
                select(parent.c.media_hash)
                .select_from(parent.join(preview, preview.c.media_hash == parent.c.thumb_hash))
                .where(
                    parent.c.thumb_hash == media_hash,
                    preview.c.derived_from == parent.c.media_hash,
                )
            )
            authorized = connection.execute(
                select(refs.c.id)
                .where(
                    or_(refs.c.media_hash == media_hash, refs.c.media_hash.in_(preview_of)),
                    refs.c.state == "active",
                    refs.c.session_id.in_(owned_session_ids),
                    *([refs.c.session_id == str(session_id)] if session_id is not None else []),
                )
                .limit(1)
            ).first()
            if authorized is None:
                return {
                    "found": False,
                    "commit_seq": str(_current_commit_seq(connection)),
                    "observed_at": observed_at.isoformat(),
                }
            row = connection.execute(select(media).where(media.c.media_hash == media_hash)).mappings().first()
            if row is None:
                return {
                    "found": False,
                    "commit_seq": str(_current_commit_seq(connection)),
                    "observed_at": observed_at.isoformat(),
                }
            statement = select(refs).where(
                refs.c.media_hash == media_hash,
                refs.c.state == "active",
                refs.c.session_id.in_(owned_session_ids),
            )
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

    def media_objects_exist_batch(self, *, media_hashes: tuple[str, ...], owner_id: str) -> dict[str, Any]:
        media = MediaObject.__table__
        refs = SessionMediaRef.__table__
        sessions = StorageSession.__table__
        observed_at = datetime.now(UTC)
        with _read_snapshot(self.engine) as connection:
            rows = (
                connection.execute(
                    select(media)
                    .join(refs, refs.c.media_hash == media.c.media_hash)
                    .join(sessions, sessions.c.session_id == refs.c.session_id)
                    .where(
                        media.c.media_hash.in_(media_hashes),
                        refs.c.state == "active",
                        sessions.c.owner_id == owner_id,
                    )
                    .distinct()
                )
                .mappings()
                .all()
            )
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
