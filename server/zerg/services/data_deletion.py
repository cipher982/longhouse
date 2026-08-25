"""Owner-scoped destructive deletion of a user's sessions and derived data.

Nothing here is a soft delete. A tombstone only suppresses serving, so this
service removes the bytes first: the immutable object store, the search index,
and the episode embeddings, then the manifests and bounded live rows behind
``storage.session.delete.v2``.

Stores this process cannot reach owner-scoped are named in the report's
``partial`` list rather than skipped quietly -- a deletion endpoint that leaves
data behind without saying so is worse than none.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from typing import Any
from uuid import UUID
from uuid import uuid4

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.client import CatalogUnavailable
from zerg.services.catalogd_supervisor import get_catalogd_client
from zerg.services.raw_object_workers import storage_v2_root
from zerg.services.searchd_supervisor import get_searchd_client

# The raw manifest cursor encoding belongs to the export path; sharing it keeps
# the two pagers from drifting apart on a tuple order nobody would notice.
from zerg.services.storage_v2_export import _source_key
from zerg.storage_v2.object_store import FilesystemImmutableObjectStore
from zerg.storage_v2.object_store import ObjectStoreError

logger = logging.getLogger(__name__)

# Deletion is human-initiated and fences manifests, rewrites projector state,
# and removes bounded live rows in one transaction. It does not belong under
# the hot-read budget the default client deadline is sized for.
DELETION_RPC_TIMEOUT_SECONDS = 10.0

_MANIFEST_PAGE = 100
_SESSION_PAGE = 100

_MEDIA_UNREACHABLE = (
    "media object bytes were not deleted: no owner-scoped catalog RPC lists a session's media refs, "
    "so the Runtime Host cannot enumerate them (the refs themselves are retired)"
)
_SUPERSEDED_RENDER_UNREACHABLE = (
    "render objects from superseded generations were not deleted: the catalog only lists render objects for a session's current generation"
)
_ARCHIVED_SESSIONS_UNREACHABLE = (
    "sessions you archived, snoozed, or hid were not deleted: the only owner-scoped session listing "
    "excludes them, so this process cannot enumerate them"
)


class DataDeletionError(RuntimeError):
    """Base error for the deletion surface."""


class DataDeletionUnavailable(DataDeletionError):
    """A store that must be reached to remove bytes did not answer."""


class SessionNotFound(DataDeletionError):
    """The caller does not demonstrably own a live session with this id."""


@dataclass
class SessionDeletionReport:
    """What actually went away for one session. Counts only, never content."""

    session_id: str
    already_deleted: bool = False
    raw_objects_deleted: int = 0
    render_objects_deleted: int = 0
    object_bytes_deleted: int = 0
    manifest_rows_retired: int = 0
    live_rows_removed: int = 0
    search_index_removed: bool = False
    partial: list[str] = field(default_factory=list)


@dataclass
class AccountDeletionReport:
    """What actually went away for every session this process could reach."""

    owner_id: int
    sessions_deleted: int = 0
    raw_objects_deleted: int = 0
    render_objects_deleted: int = 0
    object_bytes_deleted: int = 0
    manifest_rows_retired: int = 0
    live_rows_removed: int = 0
    search_indexes_removed: int = 0
    partial: list[str] = field(default_factory=list)


async def delete_session_data(*, session_id: UUID, owner_id: int) -> SessionDeletionReport:
    """Remove one owner's session from every store this process can reach."""

    catalog = _require_catalog()
    session, commit_seq = await _read_owned_session(catalog, session_id=session_id, owner_id=owner_id)
    if session is None:
        # A tombstoned session carries no owner in any read RPC, so a repeated
        # delete reports the terminal state without re-proving ownership. That
        # path reads nothing and removes nothing.
        return SessionDeletionReport(session_id=str(session_id), already_deleted=True)
    return await _delete_owned_session(catalog, session=session, owner_id=owner_id, commit_seq=commit_seq)


async def delete_account_data(*, owner_id: int) -> AccountDeletionReport:
    """Remove every session this owner still has in the owner-scoped listing.

    Deleted sessions drop out of that listing, so the first page is re-read
    until it comes back empty instead of paging a cursor across a shrinking
    set. A page that deletes nothing means the remainder is stuck; stop and
    say so rather than spinning.
    """

    catalog = _require_catalog()
    report = AccountDeletionReport(owner_id=owner_id)
    partial: list[str] = [_ARCHIVED_SESSIONS_UNREACHABLE]
    while True:
        page = await _call(
            catalog,
            "storage.session.timeline.list.v2",
            {
                "owner_id": str(owner_id),
                "before_last_activity_at": None,
                "before_session_id": None,
                "project": None,
                "provider": None,
                "include_test": True,
                "limit": _SESSION_PAGE,
            },
        )
        rows = page.get("sessions")
        if not isinstance(rows, list) or not rows:
            break
        commit_seq = int(page["commit_seq"])
        deleted_in_page = 0
        for row in rows:
            if not isinstance(row, dict) or str(row.get("owner_id")) != str(owner_id):
                continue
            session_report = await _delete_owned_session(catalog, session=row, owner_id=owner_id, commit_seq=commit_seq)
            deleted_in_page += 1
            report.sessions_deleted += 1
            report.raw_objects_deleted += session_report.raw_objects_deleted
            report.render_objects_deleted += session_report.render_objects_deleted
            report.object_bytes_deleted += session_report.object_bytes_deleted
            report.manifest_rows_retired += session_report.manifest_rows_retired
            report.live_rows_removed += session_report.live_rows_removed
            report.search_indexes_removed += 1 if session_report.search_index_removed else 0
            for note in session_report.partial:
                if note not in partial:
                    partial.append(note)
        if deleted_in_page == 0:
            partial.append(f"{len(rows)} session(s) remain: the catalog listing still returns them after a delete pass")
            break
    report.partial = partial
    logger.info(
        "Deleted account data owner=%s sessions=%d raw_objects=%d render_objects=%d bytes=%d "
        "manifests_retired=%d live_rows=%d search_indexes=%d partial=%d",
        owner_id,
        report.sessions_deleted,
        report.raw_objects_deleted,
        report.render_objects_deleted,
        report.object_bytes_deleted,
        report.manifest_rows_retired,
        report.live_rows_removed,
        report.search_indexes_removed,
        len(report.partial),
    )
    return report


def _require_catalog() -> CatalogClient:
    catalog = get_catalogd_client()
    if catalog is None:
        raise DataDeletionUnavailable("The session catalog is unavailable, so nothing was deleted.")
    return catalog


async def _call(client: CatalogClient, method: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        return await client.call(method, params, timeout_seconds=DELETION_RPC_TIMEOUT_SECONDS)
    except (CatalogRemoteError, CatalogUnavailable) as exc:
        raise DataDeletionUnavailable(f"{method} did not complete: {exc}") from exc


async def _read_owned_session(
    catalog: CatalogClient,
    *,
    session_id: UUID,
    owner_id: int,
) -> tuple[dict[str, Any] | None, int]:
    """Resolve a session the caller demonstrably owns, or fail closed."""

    result = await _call(catalog, "storage.session.read.v2", {"session_id": str(session_id)})
    commit_seq = int(result["commit_seq"])
    session = result.get("session")
    if isinstance(session, dict) and str(session.get("owner_id")) == str(owner_id):
        return session, commit_seq
    if result.get("deleted") is True:
        return None, commit_seq
    raise SessionNotFound(str(session_id))


async def _delete_owned_session(
    catalog: CatalogClient,
    *,
    session: dict[str, Any],
    owner_id: int,
    commit_seq: int,
) -> SessionDeletionReport:
    """Delete one already-owner-proven session, bytes before manifests."""

    session_id = str(session["session_id"])
    tenant_id = str(session["tenant_id"])
    report = SessionDeletionReport(session_id=session_id)
    report.partial.append(_MEDIA_UNREACHABLE)
    report.partial.append(_SUPERSEDED_RENDER_UNREACHABLE)

    # Enumerate before anything is fenced: a tombstone hides these manifest
    # rows from every read, and objects nothing can enumerate are objects
    # nothing can delete.
    objects = await _list_raw_objects(catalog, session_id=session_id, owner_id=owner_id)
    raw_count = len(objects)
    objects.extend(await _list_render_objects(catalog, session_id=session_id, snapshot_revision=commit_seq))

    # The search index holds readable content text, so it goes first.
    search = get_searchd_client()
    if search is None:
        report.partial.append("the search index and embeddings were not deleted: searchd is not running")
    else:
        try:
            await search.call(
                "search.session.delete.v2",
                {"session_id": session_id},
                timeout_seconds=DELETION_RPC_TIMEOUT_SECONDS,
            )
            report.search_index_removed = True
        except (CatalogRemoteError, CatalogUnavailable) as exc:
            report.partial.append(f"the search index and embeddings were not deleted: {exc}")

    deleted, freed, unverifiable = await asyncio.to_thread(_delete_objects, tenant_id=tenant_id, objects=objects)
    report.raw_objects_deleted = sum(1 for index in deleted if index < raw_count)
    report.render_objects_deleted = len(deleted) - report.raw_objects_deleted
    report.object_bytes_deleted = freed
    if unverifiable:
        report.partial.append(f"{unverifiable} object file(s) did not match their manifest hash and were left in place")

    fenced = await _call(
        catalog,
        "storage.session.delete.v2",
        {
            "session_id": session_id,
            "deletion_id": str(uuid4()),
            "reason": "user_delete",
            "deleted_at": datetime.now(UTC).isoformat(),
        },
    )
    report.manifest_rows_retired = (
        int(fenced.get("retired_raw_objects") or 0)
        + int(fenced.get("retired_render_objects") or 0)
        + int(fenced.get("retired_render_generations") or 0)
        + int(fenced.get("retired_media_refs") or 0)
    )
    report.live_rows_removed = int(fenced.get("live_rows_removed") or 0)
    logger.info(
        "Deleted session data session=%s owner=%s raw_objects=%d render_objects=%d bytes=%d "
        "manifests_retired=%d live_rows=%d search_index=%s partial=%d",
        session_id,
        owner_id,
        report.raw_objects_deleted,
        report.render_objects_deleted,
        report.object_bytes_deleted,
        report.manifest_rows_retired,
        report.live_rows_removed,
        report.search_index_removed,
        len(report.partial),
    )
    return report


async def _list_raw_objects(catalog: CatalogClient, *, session_id: str, owner_id: int) -> list[tuple[str, str, int]]:
    objects: list[tuple[str, str, int]] = []
    after_source_key: str | None = None
    while True:
        page = await _call(
            catalog,
            "storage.session.raw_manifest.v2",
            {
                "session_id": session_id,
                "owner_id": str(owner_id),
                "after_source_key": after_source_key,
                "limit": _MANIFEST_PAGE,
            },
        )
        rows = page.get("objects")
        if not isinstance(rows, list) or not rows:
            return objects
        objects.extend(_object_ref(row) for row in rows)
        if page.get("objects_truncated") is not True:
            return objects
        after_source_key = _source_key(rows[-1])


async def _list_render_objects(catalog: CatalogClient, *, session_id: str, snapshot_revision: int) -> list[tuple[str, str, int]]:
    objects: list[tuple[str, str, int]] = []
    after_object_id: str | None = None
    while True:
        page = await _call(
            catalog,
            "storage.session.render_objects.list.v2",
            {
                "session_id": session_id,
                "generation_id": None,
                "snapshot_revision": snapshot_revision,
                "after_object_id": after_object_id,
                "limit": _MANIFEST_PAGE,
            },
        )
        rows = page.get("objects")
        if not isinstance(rows, list) or not rows:
            return objects
        objects.extend(_object_ref(row) for row in rows)
        if page.get("has_more") is not True:
            return objects
        after_object_id = str(rows[-1]["object_id"])


def _object_ref(row: dict[str, Any]) -> tuple[str, str, int]:
    # Raw and render objects are both stored under their compressed hash, which
    # is what the manifest calls ``object_hash`` and what the store verifies.
    return str(row["object_path"]), str(row["object_hash"]), int(row["compressed_size"])


def _delete_objects(*, tenant_id: str, objects: list[tuple[str, str, int]]) -> tuple[list[int], int, int]:
    """Remove object files, returning which indexes went away, bytes, and misses.

    ``delete_verified`` returns False for a file that is already gone, which is
    what makes repeating a deletion safe.
    """

    store = FilesystemImmutableObjectStore(storage_v2_root(), tenant_id=tenant_id)
    deleted: list[int] = []
    freed = 0
    unverifiable = 0
    for index, (key, sha256, size) in enumerate(objects):
        try:
            if store.delete_verified(tenant_id=tenant_id, key=key, sha256=sha256):
                deleted.append(index)
                freed += size
        except ObjectStoreError:
            unverifiable += 1
    return deleted, freed, unverifiable


__all__ = [
    "AccountDeletionReport",
    "DataDeletionError",
    "DataDeletionUnavailable",
    "SessionDeletionReport",
    "SessionNotFound",
    "delete_account_data",
    "delete_session_data",
]
