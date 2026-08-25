"""Owner-scoped destructive deletion of a user's sessions and derived data.

Nothing here is a soft delete. A tombstone only suppresses serving, so this
service removes the bytes: the immutable object store (raw, render, and media),
the search index, and the episode embeddings, alongside the manifests and
bounded live rows behind ``storage.session.delete.v2``.

Two rules hold this file together.

*Fence before you enumerate.* Every catalog write that can attach bytes to a
session -- ``commit_raw_object``, the render generation publish and restore
paths, and ``commit_media_object`` -- reads the session tombstone inside the
same write transaction, and catalogd serializes writers. So once
``storage.session.delete.v2`` commits, the object set for that session is final
and can only shrink. Enumerating strictly *after* the fence therefore sees
every object that will ever exist for the session; enumerating before it (what
this service used to do) leaves anything committed in the gap on disk forever,
fenced from serving but never removed.

*Say what is still there.* Stores this process cannot reach owner-scoped are
named in the report's ``partial`` list and drop ``complete`` to false, rather
than being skipped quietly. A deletion endpoint that leaves data behind without
saying so is worse than none.
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
from zerg.storage_v2.object_store import FilesystemImmutableObjectStore
from zerg.storage_v2.object_store import ObjectStoreError

logger = logging.getLogger(__name__)

# Deletion is human-initiated and fences manifests, rewrites projector state,
# and removes bounded live rows in one transaction. It does not belong under
# the hot-read budget the default client deadline is sized for.
DELETION_RPC_TIMEOUT_SECONDS = 10.0

_PURGE_PAGE = 500
_SESSION_PAGE = 500

# The Runtime Host keeps a second, older copy of the same transcripts: the
# archive database (``events``, ``source_lines``, ``archive_chunks`` and the
# other session-keyed tables under ``zerg.models.agents``), its content-addressed
# archive media blobs, and the per-session archive chunk directory. Ingest still
# writes it (``AgentsStore.ingest_session``), and nothing in this service or in
# ``storage.session.delete.v2`` removes any of it -- catalogd owns the live
# database only. Until an archive purge exists, every deletion is incomplete and
# has to say so.
_ARCHIVE_TIER_UNREACHABLE = (
    "your transcript rows in the archive database were not deleted: events, source_lines, "
    "archive_chunks, the archive media blobs, and the per-session archive chunk directory are a "
    "separate store that this endpoint does not yet purge"
)


class DataDeletionError(RuntimeError):
    """Base error for the deletion surface."""


class DataDeletionUnavailable(DataDeletionError):
    """A store that must be reached to remove bytes did not answer."""


class SessionNotFound(DataDeletionError):
    """The caller does not demonstrably own a session with this id.

    Raised identically for a session that never existed, one that belongs to
    someone else, and one that is already deleted. The three are
    indistinguishable on purpose: reporting "already deleted" to a caller who
    cannot prove ownership turns a guessed UUID into an existence oracle.
    """


@dataclass
class SessionDeletionReport:
    """What actually went away for one session. Counts only, never content."""

    session_id: str
    already_deleted: bool = False
    raw_objects_deleted: int = 0
    render_objects_deleted: int = 0
    media_objects_deleted: int = 0
    media_objects_retained_shared: int = 0
    object_bytes_deleted: int = 0
    manifest_rows_retired: int = 0
    live_rows_removed: int = 0
    search_index_removed: bool = False
    complete: bool = False
    partial: list[str] = field(default_factory=list)


@dataclass
class AccountDeletionReport:
    """What actually went away for every session this process could reach."""

    owner_id: int
    sessions_deleted: int = 0
    raw_objects_deleted: int = 0
    render_objects_deleted: int = 0
    media_objects_deleted: int = 0
    media_objects_retained_shared: int = 0
    object_bytes_deleted: int = 0
    manifest_rows_retired: int = 0
    live_rows_removed: int = 0
    search_indexes_removed: int = 0
    complete: bool = False
    partial: list[str] = field(default_factory=list)


async def delete_session_data(*, session_id: UUID, owner_id: int) -> SessionDeletionReport:
    """Remove one owner's session from every store this process can reach."""

    catalog = _require_catalog()
    head = await _read_purge_head(catalog, session_id=str(session_id))
    # Ownership is proven from the durable ``sessions`` row, which survives a
    # tombstone with its owner_id intact. Anyone who cannot match it gets the
    # one indistinguishable answer.
    if not head["session_found"] or head["owner_id"] is None or str(head["owner_id"]) != str(owner_id):
        raise SessionNotFound(str(session_id))
    return await _delete_owned_session(
        catalog,
        session_id=str(session_id),
        tenant_id=str(head["tenant_id"]),
        owner_id=owner_id,
        already_deleted=bool(head["deleted"]),
    )


async def delete_account_data(*, owner_id: int) -> AccountDeletionReport:
    """Remove every session this owner has in the durable owner-scoped listing.

    ``storage.session.owned.list.v2`` is the unfiltered listing on purpose: the
    timeline listing hides archived, snoozed, hidden, and already-tombstoned
    sessions, and a user asking to delete their history means those too. Paging
    forward by session id visits each row once and terminates even though rows
    change underneath the walk.
    """

    catalog = _require_catalog()
    report = AccountDeletionReport(owner_id=owner_id)
    partial: list[str] = []
    after_session_id: str | None = None
    while True:
        page = await _call(
            catalog,
            "storage.session.owned.list.v2",
            {
                "owner_id": str(owner_id),
                "after_session_id": after_session_id,
                "limit": _SESSION_PAGE,
            },
        )
        rows = page.get("sessions")
        if not isinstance(rows, list) or not rows:
            break
        for row in rows:
            if not isinstance(row, dict) or str(row.get("owner_id")) != str(owner_id):
                continue
            session_report = await _delete_owned_session(
                catalog,
                session_id=str(row["session_id"]),
                tenant_id=str(row["tenant_id"]),
                owner_id=owner_id,
                already_deleted=bool(row.get("deleted")),
            )
            report.sessions_deleted += 1
            report.raw_objects_deleted += session_report.raw_objects_deleted
            report.render_objects_deleted += session_report.render_objects_deleted
            report.media_objects_deleted += session_report.media_objects_deleted
            report.media_objects_retained_shared += session_report.media_objects_retained_shared
            report.object_bytes_deleted += session_report.object_bytes_deleted
            report.manifest_rows_retired += session_report.manifest_rows_retired
            report.live_rows_removed += session_report.live_rows_removed
            report.search_indexes_removed += 1 if session_report.search_index_removed else 0
            for note in session_report.partial:
                if note not in partial:
                    partial.append(note)
        after_session_id = str(rows[-1]["session_id"])
        if page.get("has_more") is not True:
            break
    if _ARCHIVE_TIER_UNREACHABLE not in partial:
        partial.append(_ARCHIVE_TIER_UNREACHABLE)
    report.partial = partial
    report.complete = not partial
    logger.warning(
        "Deleted account data owner=%s sessions=%d raw_objects=%d render_objects=%d media_objects=%d "
        "bytes=%d manifests_retired=%d live_rows=%d search_indexes=%d complete=%s partial=%d",
        owner_id,
        report.sessions_deleted,
        report.raw_objects_deleted,
        report.render_objects_deleted,
        report.media_objects_deleted,
        report.object_bytes_deleted,
        report.manifest_rows_retired,
        report.live_rows_removed,
        report.search_indexes_removed,
        report.complete,
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


async def _read_purge_head(catalog: CatalogClient, *, session_id: str) -> dict[str, Any]:
    """Read the ownership facts on the purge manifest without paging it."""

    return await _call(
        catalog,
        "storage.session.purge_manifest.v2",
        {"session_id": session_id, "after_kind": None, "after_key": None, "limit": 1},
    )


async def _delete_owned_session(
    catalog: CatalogClient,
    *,
    session_id: str,
    tenant_id: str,
    owner_id: int,
    already_deleted: bool,
) -> SessionDeletionReport:
    """Delete one already-owner-proven session: fence, then remove the bytes."""

    report = SessionDeletionReport(session_id=session_id, already_deleted=already_deleted)

    # 1. Fence first. This is what makes the enumeration below complete: after
    #    it commits, no write path will accept another object for this session,
    #    so nothing can appear behind the walk. Re-fencing an already-fenced
    #    session is a no-op that reports zero retired rows.
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

    # 2. The search index holds readable content text and its embeddings.
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

    # 3. Enumerate the final object set and remove the bytes.
    raw, render, media = await _list_purge_objects(catalog, session_id=session_id)
    deleted, freed, unverifiable = await asyncio.to_thread(
        _delete_objects,
        tenant_id=tenant_id,
        objects=raw + render,
    )
    report.raw_objects_deleted = sum(1 for index in deleted if index < len(raw))
    report.render_objects_deleted = len(deleted) - report.raw_objects_deleted
    report.object_bytes_deleted = freed
    if unverifiable:
        report.partial.append(f"{unverifiable} object file(s) did not match their manifest hash and were left in place")

    media_deleted, media_bytes, media_shared, media_unverifiable = await _delete_media(
        catalog,
        tenant_id=tenant_id,
        media=media,
    )
    report.media_objects_deleted = media_deleted
    report.media_objects_retained_shared = media_shared
    report.object_bytes_deleted += media_bytes
    if media_shared:
        report.partial.append(f"{media_shared} media object(s) are still referenced by another session, so their bytes were kept")
    if media_unverifiable:
        report.partial.append(f"{media_unverifiable} media file(s) did not match their content hash and were left in place")

    report.partial.append(_ARCHIVE_TIER_UNREACHABLE)
    report.complete = not report.partial
    logger.warning(
        "Deleted session data session=%s owner=%s raw_objects=%d render_objects=%d media_objects=%d "
        "bytes=%d manifests_retired=%d live_rows=%d search_index=%s complete=%s partial=%d",
        session_id,
        owner_id,
        report.raw_objects_deleted,
        report.render_objects_deleted,
        report.media_objects_deleted,
        report.object_bytes_deleted,
        report.manifest_rows_retired,
        report.live_rows_removed,
        report.search_index_removed,
        report.complete,
        len(report.partial),
    )
    return report


async def _list_purge_objects(
    catalog: CatalogClient,
    *,
    session_id: str,
) -> tuple[list[tuple[str, str, int]], list[tuple[str, str, int]], list[dict[str, Any]]]:
    """Page every object the session will ever own, split by kind.

    Called only after the fence. The RPC applies no retirement, generation, or
    snapshot filter, so superseded render generations and objects retired by
    the fence itself are all in here.
    """

    raw: list[tuple[str, str, int]] = []
    render: list[tuple[str, str, int]] = []
    media: list[dict[str, Any]] = []
    after_kind: str | None = None
    after_key: str | None = None
    while True:
        page = await _call(
            catalog,
            "storage.session.purge_manifest.v2",
            {
                "session_id": session_id,
                "after_kind": after_kind,
                "after_key": after_key,
                "limit": _PURGE_PAGE,
            },
        )
        rows = page.get("objects")
        if not isinstance(rows, list) or not rows:
            return raw, render, media
        for row in rows:
            kind = str(row["kind"])
            if kind == "media":
                media.append(row)
            elif kind == "render":
                render.append(_object_ref(row))
            else:
                raw.append(_object_ref(row))
        if page.get("has_more") is not True:
            return raw, render, media
        after_kind = str(rows[-1]["kind"])
        after_key = str(rows[-1]["key"])


def _object_ref(row: dict[str, Any]) -> tuple[str, str, int]:
    # Raw and render objects are both stored under their compressed hash, which
    # is what the manifest calls ``object_hash`` and what the store verifies.
    return str(row["object_path"]), str(row["object_hash"]), int(row["byte_size"] or 0)


async def _delete_media(
    catalog: CatalogClient,
    *,
    tenant_id: str,
    media: list[dict[str, Any]],
) -> tuple[int, int, int, int]:
    """Retire each media manifest, then remove the bytes it stopped protecting.

    Media is content-addressed across sessions, so the bytes are only this
    user's to delete once no other session holds an active reference.
    ``storage.media.commit.v2`` decides that transactionally: it refuses to move
    a manifest to ``deleted`` while any active ref remains, and refuses to move
    it back out of ``deleted`` afterwards. Marking the manifest first and
    deleting the file second means no session can acquire a reference to bytes
    that are on their way out.
    """

    retirable: list[tuple[str, str, int]] = []
    shared = 0
    observed_at = datetime.now(UTC).isoformat()
    for row in media:
        media_hash = str(row["key"])
        if bool(row.get("shared")):
            shared += 1
            continue
        try:
            await catalog.call(
                "storage.media.commit.v2",
                {
                    "media_hash": media_hash,
                    "state": "deleted",
                    "mime_type": None,
                    "byte_size": None,
                    "object_path": None,
                    "session_refs": [],
                    "observed_at": observed_at,
                },
                timeout_seconds=DELETION_RPC_TIMEOUT_SECONDS,
            )
        except CatalogRemoteError as exc:
            if exc.code == "conflict":
                # Another session acquired an active reference between the
                # enumeration and this call. Its bytes, not ours.
                shared += 1
                continue
            raise DataDeletionUnavailable(f"storage.media.commit.v2 did not complete: {exc}") from exc
        except CatalogUnavailable as exc:
            raise DataDeletionUnavailable(f"storage.media.commit.v2 did not complete: {exc}") from exc
        object_path = row.get("object_path")
        if object_path:
            retirable.append((str(object_path), media_hash, int(row.get("byte_size") or 0)))

    deleted, freed, unverifiable = await asyncio.to_thread(_delete_objects, tenant_id=tenant_id, objects=retirable)
    return len(deleted), freed, shared, unverifiable


def _delete_objects(*, tenant_id: str, objects: list[tuple[str, str, int]]) -> tuple[list[int], int, int]:
    """Remove object files, returning which indexes went away, bytes, and misses.

    ``delete_verified`` returns False for a file that is already gone, which is
    what makes repeating a deletion safe.
    """

    if not objects:
        return [], 0, 0
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
