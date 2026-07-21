"""Streaming, exact raw-session export from storage-v2 objects."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import HTTPException
from fastapi import status
from starlette.responses import StreamingResponse

from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.client import CatalogUnavailable
from zerg.services.catalogd_supervisor import get_catalogd_client
from zerg.services.raw_object_workers import RawObjectWorkerError
from zerg.services.raw_object_workers import get_raw_object_worker_pool
from zerg.storage_v2.raw_objects import RawObjectCorruptError


def _source_key(item: dict[str, object]) -> str:
    return json.dumps(
        [
            item["machine_id"],
            item["provider"],
            item["opaque_source_id"],
            item["source_epoch"],
            f"{int(item['range_start']):020d}",
            item["envelope_id"],
        ],
        separators=(",", ":"),
    )


async def build_storage_v2_raw_export(
    *,
    session_id: UUID,
    owner_id: int,
    branch_mode: str,
) -> StreamingResponse:
    """Return a bounded streaming JSONL export without opening legacy SQLite."""

    if branch_mode not in {"head", "all"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="branch_mode must be one of: head, all",
        )
    catalog = get_catalogd_client()
    if catalog is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The session catalog is unavailable.",
        )
    try:
        session_result = await catalog.call("storage.session.read.v2", {"session_id": str(session_id)})
    except (CatalogRemoteError, CatalogUnavailable) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The session catalog is unavailable.",
        ) from exc
    session = session_result.get("session")
    if not isinstance(session, dict) or str(session.get("owner_id")) != str(owner_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Session {session_id} not found")

    async def records() -> AsyncIterator[bytes]:
        after_source_key: str | None = None
        workers = get_raw_object_worker_pool()
        while True:
            try:
                manifest = await catalog.call(
                    "storage.session.raw_manifest.v2",
                    {
                        "session_id": str(session_id),
                        "owner_id": str(owner_id),
                        "after_source_key": after_source_key,
                        "limit": 8,
                    },
                )
            except (CatalogRemoteError, CatalogUnavailable) as exc:
                raise RuntimeError("catalog became unavailable during raw export") from exc
            objects = manifest.get("objects")
            if not isinstance(objects, list):
                raise RuntimeError("catalog returned an invalid raw manifest")
            if not objects:
                return
            for item in objects:
                if not isinstance(item, dict):
                    raise RuntimeError("catalog returned an invalid raw-object row")
                try:
                    decoded = await workers.read(str(item["object_path"]), str(item["object_hash"]), str(item["tenant_id"]))
                except (KeyError, RawObjectCorruptError, RawObjectWorkerError) as exc:
                    raise RuntimeError("immutable raw object could not be verified") from exc
                if decoded.envelope_id != item.get("envelope_id") or decoded.spec.session_id != session_id:
                    raise RuntimeError("raw object does not match its catalog manifest")
                for record in decoded.spec.records:
                    yield record.data
                    if not record.data.endswith(b"\n"):
                        yield b"\n"
            after_source_key = _source_key(objects[-1])
            if manifest.get("objects_truncated") is not True:
                return

    headers = {
        "Content-Disposition": f"attachment; filename={session_id}.jsonl",
        "X-Session-CWD": str(session.get("cwd") or ""),
        "X-Session-Provider": str(session.get("provider") or ""),
        "X-Session-Project": str(session.get("project") or ""),
        "X-Session-Branch-Mode": branch_mode,
        "X-Longhouse-Storage": "v2",
    }
    return StreamingResponse(records(), media_type="application/x-ndjson", headers=headers)


__all__ = ["build_storage_v2_raw_export"]
