"""Cold session-ingest operation executed only by the archive worker process."""

from __future__ import annotations

import os
import time
from typing import Any
from uuid import uuid4

from zerg.config import get_settings
from zerg.database import get_write_session_factory
from zerg.services.agents.models import IngestResult
from zerg.services.agents.models import SessionIngest
from zerg.services.agents.store import AgentsStore
from zerg.services.archive_shadow import insert_archive_chunk_manifests
from zerg.services.archive_shadow import prepare_ingest_shadow_archive


def archive_ingest_worker_enabled() -> bool:
    from zerg.services.archive_worker_status import archive_worker_enabled
    from zerg.services.archive_worker_status import archive_worker_status_path

    if not archive_worker_enabled():
        return False
    if archive_worker_status_path() is None:
        return False
    # Dark until every remaining monolith writer has moved behind the process
    # boundary. Enabling this while the API WriteSerializer is active would
    # create two writer processes for longhouse.db.
    explicit = os.getenv("LONGHOUSE_ARCHIVE_INGEST_WORKER_ENABLED")
    if explicit is None:
        from zerg.database import live_catalog_enabled

        return live_catalog_enabled()
    value = explicit.strip().lower()
    return value not in {"0", "false", "no", "off"}


def execute_archive_ingest_job(payload: dict[str, Any]) -> dict[str, Any]:
    data = SessionIngest.model_validate(payload.get("data") or {})
    write_label = str(payload.get("write_label") or "ingest")
    batch_index = int(payload.get("batch_index") or 0)
    ship_trace = payload.get("ship_trace") if isinstance(payload.get("ship_trace"), dict) else None
    timing = payload.get("timing") if isinstance(payload.get("timing"), dict) else {}
    if data.id is None:
        data.id = uuid4()

    settings = get_settings()
    if not settings.archive_primary_write_enabled and not settings.legacy_raw_write_enabled:
        raise RuntimeError("legacy raw writes cannot be disabled unless archive-primary writes are enabled")
    session_factory = get_write_session_factory()
    if session_factory is None:
        raise RuntimeError("archive write session factory is unavailable")

    from zerg.routers.agents_ingest import _incremental_session_counts_for_label
    from zerg.routers.agents_ingest import _ingest_chunk_for_label
    from zerg.routers.agents_ingest import _persist_ship_trace_event
    from zerg.routers.agents_ingest import _sync_derived_projections_for_label
    from zerg.routers.agents_ingest import _sync_session_counts_for_label
    from zerg.routers.agents_ingest import _unix_ms

    archive_primary_state = "disabled"
    legacy_raw_effective = settings.legacy_raw_write_enabled
    archive_primary_records_written = 0
    started = time.monotonic()
    with session_factory() as db:
        if settings.archive_primary_write_enabled:
            placeholder = IngestResult(
                session_id=data.id,
                events_inserted=0,
                events_skipped=0,
                session_created=False,
                source_lines_inserted=0,
            )
            prepared = prepare_ingest_shadow_archive(
                data=data,
                result=placeholder,
                settings=settings,
                manifest_db=db,
                force_enabled=True,
            )
            if prepared.error:
                if not settings.legacy_raw_write_enabled and write_label != "ingest-live":
                    raise RuntimeError(f"archive-primary prepare failed: {prepared.error}")
                archive_primary_state = "fallback"
                legacy_raw_effective = True
            else:
                archive_primary_state = "prepared"
                archive_primary_records_written = prepared.records_written
                if prepared.chunks:
                    try:
                        insert_archive_chunk_manifests(db, prepared.chunks)
                        archive_primary_state = "written"
                    except Exception:
                        db.rollback()
                        if not settings.legacy_raw_write_enabled and write_label != "ingest-live":
                            raise
                        archive_primary_state = "fallback"
                        legacy_raw_effective = True
                else:
                    archive_primary_state = "written"

        write_started_at_ms = _unix_ms()
        result = AgentsStore(db).ingest_session(
            data,
            chunk_size=_ingest_chunk_for_label(write_label),
            synchronous_projections=_sync_derived_projections_for_label(write_label),
            synchronous_session_counts=_sync_session_counts_for_label(write_label),
            incremental_session_counts=_incremental_session_counts_for_label(write_label),
            write_legacy_raw=legacy_raw_effective,
            raw_source_archived=archive_primary_state == "written" and archive_primary_records_written > 0,
        )
        store_returned_at_ms = _unix_ms()
        _persist_ship_trace_event(
            db,
            data=data,
            result=result,
            ship_trace=ship_trace,
            server_trace={
                **timing,
                "write_started_at_ms": write_started_at_ms,
                "store_returned_at_ms": store_returned_at_ms,
                "store_write_ms": store_returned_at_ms - write_started_at_ms,
                "store_stage_ms": result.store_stage_ms,
                "store_counts": {
                    "events_inserted": result.events_inserted,
                    "events_skipped": result.events_skipped,
                    "source_lines_inserted": result.source_lines_inserted,
                    "commit_count": result.commit_count,
                    "commit_ms_total": result.commit_ms_total,
                },
            },
        )
        if not settings.archive_primary_write_enabled and settings.archive_shadow_write_enabled:
            prepared_shadow = prepare_ingest_shadow_archive(
                data=data,
                result=result,
                settings=settings,
                manifest_db=db,
            )
            if not prepared_shadow.error and prepared_shadow.chunks:
                insert_archive_chunk_manifests(db, prepared_shadow.chunks)
        db.commit()

    from zerg.database import get_live_session_factory
    from zerg.services.live_catalog_backfill import sync_live_catalog_session

    live_session_factory = get_live_session_factory()
    if live_session_factory is None:
        raise RuntimeError("archive ingest worker requires live catalog session factory")
    with session_factory() as archive_db, live_session_factory() as live_db:
        sync_live_catalog_session(archive_db, live_db, session_id=result.session_id)

    return {
        "result": result.model_dump(mode="json"),
        "archive_primary_state": archive_primary_state,
        "legacy_raw_effective": legacy_raw_effective,
        "worker_exec_ms": round((time.monotonic() - started) * 1000, 1),
        "batch_index": batch_index,
    }
