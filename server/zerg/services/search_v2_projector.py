"""Coalesced render-to-search projection for storage-v2 sessions."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
from uuid import UUID
from uuid import uuid4

from zerg.catalogd.client import CatalogClient
from zerg.searchd.store import object_set_hash
from zerg.services.render_object_workers import RenderObjectWorkerPool
from zerg.services.render_object_workers import get_render_object_worker_pool
from zerg.services.searchd_supervisor import get_searchd_client

logger = logging.getLogger(__name__)

PROJECTOR = "search-v2"
PAGE_SIZE = 100


class SearchProjectionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SearchV2Projector:
    def __init__(
        self,
        *,
        catalog: CatalogClient,
        search: CatalogClient,
        render_workers: RenderObjectWorkerPool,
        worker_id: str | None = None,
    ) -> None:
        self.catalog = catalog
        self.search = search
        self.render_workers = render_workers
        self.worker_id = worker_id or f"search-v2:{os.getpid()}"
        self._bound_store_id: str | None = None

    async def run_once(self, *, limit: int = 4, now: datetime | None = None) -> int:
        observed_at = now or datetime.now(UTC)
        await self._ensure_store_binding(observed_at=observed_at)
        claim_token = str(uuid4())
        claim = await self.catalog.call(
            "projector.state.claim.v2",
            {
                "projector": PROJECTOR,
                "worker_id": self.worker_id,
                "claim_token": claim_token,
                "now": observed_at.isoformat(),
                "lease_seconds": 300,
                "limit": limit,
            },
        )
        states = claim.get("claimed")
        if not isinstance(states, list):
            raise SearchProjectionError("invalid_catalog_response", "catalog returned an invalid projector claim")
        for state in states:
            await self._run_claim(state, claim_token=claim_token)
        return len(states)

    async def _ensure_store_binding(self, *, observed_at: datetime) -> None:
        ping = await self.search.call("search.ping.v2")
        store_id = _uuid(ping.get("store_id"), "store_id")
        schema_generation = ping.get("schema_generation")
        if not isinstance(schema_generation, str) or not schema_generation:
            raise SearchProjectionError("invalid_search_response", "searchd omitted its schema generation")
        if store_id == self._bound_store_id:
            return
        await self.catalog.call(
            "projector.store.bind.v2",
            {
                "projector": PROJECTOR,
                "store_id": store_id,
                "schema_generation": schema_generation,
                "observed_at": observed_at.isoformat(),
            },
        )
        self._bound_store_id = store_id

    async def _run_claim(self, state: object, *, claim_token: str) -> None:
        try:
            if not isinstance(state, dict):
                raise SearchProjectionError("invalid_catalog_response", "catalog returned an invalid projector row")
            session_id = _uuid(state.get("session_id"), "session_id")
            claimed_revision = _revision(state.get("claimed_revision"), "claimed_revision")
            await self._project(session_id=session_id, claimed_revision=claimed_revision)
            await self.catalog.call(
                "projector.state.complete.v2",
                {
                    "projector": PROJECTOR,
                    "session_id": session_id,
                    "claim_token": claim_token,
                    "completed_revision": claimed_revision,
                    "completed_at": datetime.now(UTC).isoformat(),
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure_count = int(state.get("failure_count", 0)) if isinstance(state, dict) else 0
            retry_seconds = min(300, 5 * (2 ** min(failure_count, 6)))
            code = exc.code if isinstance(exc, SearchProjectionError) else "projection_failed"
            message = str(exc)[:2_048] or type(exc).__name__
            session_value = state.get("session_id") if isinstance(state, dict) else None
            if isinstance(session_value, str):
                failed_at = datetime.now(UTC)
                await self.catalog.call(
                    "projector.state.fail.v2",
                    {
                        "projector": PROJECTOR,
                        "session_id": session_value,
                        "claim_token": claim_token,
                        "error_code": code,
                        "error_message": message,
                        "failed_at": failed_at.isoformat(),
                        "retry_at": (failed_at + timedelta(seconds=retry_seconds)).isoformat(),
                    },
                )
            logger.warning("Search-v2 projection failed session=%s code=%s error=%s", session_value, code, message)

    async def _project(self, *, session_id: str, claimed_revision: int) -> None:
        generation_id: str | None = None
        after_object_id: str | None = None
        expected_objects: int | None = None
        expected_events: int | None = None
        session: dict[str, Any] | None = None
        object_ids: list[str] = []
        indexed_events = 0
        while True:
            page = await self.catalog.call(
                "storage.session.render_objects.list.v2",
                {
                    "session_id": session_id,
                    "generation_id": generation_id,
                    "snapshot_revision": claimed_revision,
                    "after_object_id": after_object_id,
                    "limit": PAGE_SIZE,
                },
            )
            if page.get("deleted") is True:
                await self.search.call("search.session.delete.v2", {"session_id": session_id})
                return
            if page.get("found") is not True:
                raise SearchProjectionError("session_missing", "storage session is not available for projection")
            if str(page.get("snapshot_revision")) != str(claimed_revision):
                raise SearchProjectionError("revision_drift", "catalog changed the claimed projector revision")
            page_generation = _uuid(page.get("generation_id"), "generation_id")
            if generation_id is None:
                generation_id = page_generation
                session_value = page.get("session")
                if not isinstance(session_value, dict):
                    raise SearchProjectionError("invalid_catalog_response", "catalog omitted storage session facts")
                session = session_value
                expected_objects = _count(page.get("snapshot_object_count"), "snapshot_object_count")
                expected_events = _count(page.get("snapshot_event_count"), "snapshot_event_count")
            elif page_generation != generation_id:
                raise SearchProjectionError("generation_drift", "render generation changed during frozen projection")
            if (
                _count(page.get("snapshot_object_count"), "snapshot_object_count") != expected_objects
                or _count(page.get("snapshot_event_count"), "snapshot_event_count") != expected_events
            ):
                raise SearchProjectionError("revision_drift", "render snapshot counts changed during projection")
            objects = page.get("objects")
            if not isinstance(objects, list):
                raise SearchProjectionError("invalid_catalog_response", "catalog returned invalid render objects")
            for manifest in objects:
                event_count = await self._index_object(
                    manifest=manifest,
                    session_id=session_id,
                    generation_id=generation_id,
                    claimed_revision=claimed_revision,
                    session=session,
                )
                object_id = _hash(manifest.get("object_id") if isinstance(manifest, dict) else None, "object_id")
                if object_ids and object_id <= object_ids[-1]:
                    raise SearchProjectionError("invalid_catalog_response", "render object page is not strictly ordered")
                object_ids.append(object_id)
                indexed_events += event_count
            if page.get("has_more") is not True:
                break
            if not objects:
                raise SearchProjectionError("invalid_catalog_response", "catalog returned an empty truncated page")
            after_object_id = object_ids[-1]

        assert generation_id is not None and session is not None
        if len(object_ids) != expected_objects or indexed_events != expected_events:
            raise SearchProjectionError("projection_lag", "render object coverage does not match the frozen manifest")
        owner_id = session.get("owner_id")
        if owner_id is None:
            raise SearchProjectionError("owner_missing", "storage session has no owner for search isolation")
        published = await self.search.call(
            "search.index.publish.v2",
            {
                "session_id": session_id,
                "generation_id": generation_id,
                "owner_id": str(owner_id),
                "desired_revision": str(claimed_revision),
                "object_count": len(object_ids),
                "object_set_hash": object_set_hash(object_ids),
                "event_count": indexed_events,
                "project": session.get("project"),
                "provider": session["provider"],
                "environment": session["environment"],
                "cwd": session.get("cwd"),
                "git_repo": session.get("git_repo"),
                "started_at": session["started_at"],
            },
        )
        if published.get("published") is not True:
            raise SearchProjectionError("projection_lag", "searchd did not publish the exact render object set")

    async def _index_object(
        self,
        *,
        manifest: object,
        session_id: str,
        generation_id: str,
        claimed_revision: int,
        session: dict[str, Any],
    ) -> int:
        if not isinstance(manifest, dict):
            raise SearchProjectionError("invalid_catalog_response", "render manifest row is invalid")
        object_id = _hash(manifest.get("object_id"), "object_id")
        object_hash = _hash(manifest.get("object_hash"), "object_hash")
        object_path = manifest.get("object_path")
        if not isinstance(object_path, str) or not object_path:
            raise SearchProjectionError("invalid_catalog_response", "render object path is invalid")
        decoded = await self.render_workers.read(object_path, object_hash, lane="background")
        spec = decoded.spec
        if str(spec.session_id) != session_id or str(spec.render_generation) != generation_id or decoded.object_hash != object_id:
            raise SearchProjectionError("render_corrupt", "render object identity does not match its catalog manifest")
        records = [
            {
                "event_id": record.event_id,
                "record_ordinal": ordinal,
                "order_time_us": record.order_time_us,
                "source_position": record.source_position,
                "event_subordinal": record.event_subordinal,
                "role": record.role,
                "content_text": record.content_text,
                "tool_name": record.tool_name,
                "tool_output_text": record.tool_output_text,
                "tool_call_id": record.tool_call_id,
                "thread_id": record.thread_id,
                "branch_kind": record.branch_kind,
            }
            for ordinal, record in enumerate(spec.records)
        ]
        await self.search.call(
            "search.index.object.v2",
            {
                "session_id": session_id,
                "generation_id": generation_id,
                "object_id": object_id,
                "desired_revision": str(claimed_revision),
                "provider": spec.provider,
                "machine_id": spec.machine_id,
                "project": session.get("project"),
                "environment": session["environment"],
                "cwd": session.get("cwd"),
                "git_repo": session.get("git_repo"),
                "opaque_source_id": spec.opaque_source_id,
                "source_epoch": str(spec.source_epoch),
                "records": records,
            },
        )
        return len(records)


def _uuid(value: object, field: str) -> str:
    try:
        parsed = UUID(str(value))
    except ValueError as exc:
        raise SearchProjectionError("invalid_catalog_response", f"{field} is not a UUID") from exc
    if str(parsed) != value:
        raise SearchProjectionError("invalid_catalog_response", f"{field} is not canonical")
    return str(parsed)


def _revision(value: object, field: str) -> int:
    if not isinstance(value, str) or not value.isdecimal() or not 0 <= int(value) < 1 << 63:
        raise SearchProjectionError("invalid_catalog_response", f"{field} is invalid")
    return int(value)


def _count(value: object, field: str) -> int:
    if type(value) is not int or not 0 <= value <= 1_000_000_000:
        raise SearchProjectionError("invalid_catalog_response", f"{field} is invalid")
    return value


def _hash(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise SearchProjectionError("invalid_catalog_response", f"{field} is invalid")
    return value


_task: asyncio.Task[None] | None = None


async def _run_forever(projector: SearchV2Projector) -> None:
    while True:
        try:
            claimed = await projector.run_once()
            await asyncio.sleep(0 if claimed else 0.5)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Search-v2 projector tick failed")
            await asyncio.sleep(1.0)


def start_search_v2_projector() -> bool:
    global _task
    if _task is not None and not _task.done():
        return True
    from zerg.services.catalogd_supervisor import get_catalogd_client

    catalog = get_catalogd_client()
    search = get_searchd_client()
    if catalog is None or search is None:
        return False
    projector = SearchV2Projector(
        catalog=catalog,
        search=search,
        render_workers=get_render_object_worker_pool(),
    )
    _task = asyncio.create_task(_run_forever(projector), name="search-v2-projector")
    return True


async def stop_search_v2_projector() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    _task = None


__all__ = ["SearchV2Projector", "start_search_v2_projector", "stop_search_v2_projector"]
