"""Session turn index: every user turn of a session without reading its transcript.

Each render object records its user-role records in the catalog when it is
sealed (``user_turns``), and semantic repair rewrites them with the counts. A
turn index read is one catalog query over those rows. Only objects still
waiting for repair, or sealed before ``user_turns`` existed, are decoded, with
the same raw recovery the transcript reader runs. The head-branch rule and the
fragment merge are the transcript's own, so the index lists exactly the user
rows a full transcript read shows.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC
from datetime import datetime
from uuid import UUID

from fastapi import HTTPException
from fastapi import status

from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.client import CatalogUnavailable
from zerg.routers.agents_storage_v2 import _SESSION_DETAIL_WORKER_QUEUE_TIMEOUT_SECONDS as SESSION_DETAIL_WORKER_QUEUE_TIMEOUT_SECONDS
from zerg.services.catalog_read_gateway import CatalogReadError
from zerg.services.catalog_read_gateway import resolve_session_alias
from zerg.services.catalogd_supervisor import get_catalogd_client
from zerg.services.render_object_workers import RenderObjectWorkerBusy
from zerg.services.render_object_workers import RenderObjectWorkerError
from zerg.services.render_object_workers import get_render_object_worker_pool
from zerg.storage_v2.branching import infers_branches_from_parent_links
from zerg.storage_v2.branching import superseded_user_event_ids
from zerg.storage_v2.render_objects import RenderObjectCorruptError
from zerg.storage_v2.render_objects import aggregate_render_object

_CATALOG_TIMEOUT_SECONDS = 5.0
_PREVIEW_CHARS = 500
# The newest turns are what the rail shows; older ones stay a count.
DEFAULT_TURN_LIMIT = 500
MAX_TURN_LIMIT = 5_000
_DECODE_BATCH = 16
# The engine splits one prompt's content blocks into `<id>-image-<n>` and
# `<id>-text-<n>` events; the transcript shows them as one row (timelineModel's
# isUserMessageFragment), so the index does too.
_FRAGMENT_SUFFIX = re.compile(r"^(?:image|text)-\d+$")


def _is_fragment_of(base_id: str, event_id: str) -> bool:
    prefix = f"{base_id}-"
    return event_id.startswith(prefix) and _FRAGMENT_SUFFIX.match(event_id[len(prefix) :]) is not None


def _timestamp(order_time_us: int) -> str:
    seconds, microseconds = divmod(order_time_us, 1_000_000)
    return datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=microseconds).isoformat()


def build_turns(
    entries: list[dict[str, object]],
    *,
    infer_parent_branches: bool,
    branch_mode: str = "head",
) -> list[dict[str, object]]:
    """Turn rows from user-record entries, applying the transcript's branch rule and fragment merge."""

    ordered = sorted(entries, key=lambda entry: tuple(entry["order_key"]))  # type: ignore[arg-type]
    superseded: set[str] = set()
    if infer_parent_branches:
        superseded = superseded_user_event_ids(
            (tuple(entry["order_key"]), str(entry["event_id"]), entry.get("parent_uuid"))  # type: ignore[misc]
            for entry in ordered
        )
    turns: list[dict[str, object]] = []
    for entry in ordered:
        if entry.get("turn") is not True:
            continue
        event_id = str(entry["event_id"])
        if branch_mode == "head" and (entry.get("branch_kind") == "abandoned" or event_id in superseded):
            continue
        preview = entry.get("preview")
        if turns and _is_fragment_of(str(turns[-1]["event_id"]), event_id):
            # The timeline joins a prompt's blocks with a blank line
            # (timelineModel's joinMessageParts); the preview does the same.
            parts = [part.strip() for part in (turns[-1]["preview"], preview) if isinstance(part, str) and part.strip()]
            turns[-1]["preview"] = "\n\n".join(parts)[:_PREVIEW_CHARS] or None
            continue
        order_key = entry["order_key"]
        turns.append(
            {
                "event_id": event_id,
                "timestamp": _timestamp(int(order_key[0])),  # type: ignore[index]
                "preview": preview,
            }
        )
    return turns


async def _read_index(catalogd, session_id: UUID, owner_id: int) -> dict:
    try:
        return await catalogd.call(
            "storage.session.turn_index.v2",
            {"session_id": str(session_id), "owner_id": str(owner_id)},
            timeout_seconds=_CATALOG_TIMEOUT_SECONDS,
        )
    except (CatalogRemoteError, CatalogUnavailable) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="The session catalog is unavailable.") from exc


async def read_session_turn_index(
    *,
    session_id: UUID,
    owner_id: int,
    branch_mode: str = "head",
    limit: int = DEFAULT_TURN_LIMIT,
) -> dict[str, object]:
    if not 1 <= limit <= MAX_TURN_LIMIT:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"limit must be from 1 through {MAX_TURN_LIMIT}")
    if branch_mode not in {"head", "all"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="branch_mode must be one of: head, all")
    catalogd = get_catalogd_client()
    if catalogd is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="The session catalog is unavailable.")
    requested_id = session_id
    index = await _read_index(catalogd, session_id, owner_id)
    if index.get("found") is not True or index.get("retired") is True:
        # Same alias rule as the workspace: a provider alias or retired shadow
        # id opens the session it now belongs to.
        try:
            alias = await asyncio.to_thread(resolve_session_alias, str(session_id), owner_id=owner_id)
        except CatalogReadError:
            alias = {}
        try:
            resolved = UUID(str(alias["session_id"])) if alias.get("found") is True and alias.get("session_id") else None
        except ValueError:
            resolved = None
        if resolved is not None and resolved != session_id:
            session_id = resolved
            index = await _read_index(catalogd, session_id, owner_id)
    if index.get("found") is not True or index.get("retired") is True:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Session {requested_id} not found")
    generation_id = index.get("generation_id")
    objects = [item for item in index.get("objects") or [] if isinstance(item, dict)]

    entries: list[dict[str, object]] = []
    pending = [item for item in objects if item.get("user_turns") is None]
    for item in objects:
        if item.get("user_turns") is not None:
            entries.extend(item["user_turns"])
    decoded_objects = 0
    if pending:
        workers = get_render_object_worker_pool()
        try:
            for start in range(0, len(pending), _DECODE_BATCH):
                batch = pending[start : start + _DECODE_BATCH]
                decoded_batch = await asyncio.gather(
                    *(
                        workers.read(
                            str(item["object_path"]),
                            str(item["object_hash"]),
                            lane="user",
                            # The transcript reader's timeout, so concurrent
                            # reads of the same object share one decode.
                            queue_timeout_seconds=SESSION_DETAIL_WORKER_QUEUE_TIMEOUT_SECONDS,
                        )
                        for item in batch
                    )
                )
                for item, decoded in zip(batch, decoded_batch, strict=True):
                    spec = decoded.spec
                    if (
                        spec.session_id != session_id
                        or str(spec.render_generation) != str(generation_id)
                        or spec.source_envelope_id != item.get("source_envelope_id")
                        or decoded.object_hash != item.get("object_hash")
                    ):
                        raise HTTPException(
                            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="A render object does not match its catalog manifest.",
                        )
                    entries.extend(aggregate_render_object(spec)["user_turns"])  # type: ignore[arg-type]
                decoded_objects += len(batch)
        except RenderObjectWorkerBusy as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Session history is temporarily busy; retry shortly.",
                headers={"Retry-After": "1"},
            ) from exc
        except (RenderObjectCorruptError, RenderObjectWorkerError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="The immutable render generation could not be verified.",
            ) from exc

    providers = {str(item.get("provider") or "") for item in objects}
    turns = build_turns(
        entries,
        infer_parent_branches=any(infers_branches_from_parent_links(provider) for provider in providers),
        branch_mode=branch_mode,
    )
    return {
        "session_id": str(session_id),
        "generation_id": str(generation_id) if generation_id is not None else None,
        "branch_mode": branch_mode,
        "total_turns": len(turns),
        "turns": turns[-limit:],
        "objects_indexed": len(objects) - len(pending),
        "objects_decoded": decoded_objects,
    }
