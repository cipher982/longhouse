"""Episode embedding projection for storage-v2 render objects."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import UUID
from uuid import uuid4

from zerg.catalogd.client import CatalogClient
from zerg.services.render_object_workers import RenderObjectWorkerPool
from zerg.services.render_object_workers import get_render_object_worker_pool
from zerg.services.session_processing.embeddings import EMBEDDING_BATCH_SIZE
from zerg.services.session_processing.embeddings import EMBEDDING_MAX_CHUNKS_PER_PASS
from zerg.services.session_processing.embeddings import PermanentEmbeddingConfigError
from zerg.services.session_processing.embeddings import embedding_to_bytes
from zerg.services.session_processing.embeddings import generate_embeddings
from zerg.services.session_processing.embeddings import iter_turn_chunks

logger = logging.getLogger(__name__)
PROJECTOR = "embeddings-v1"
PAGE_SIZE = 100
PROJECTOR_WORKERS = max(1, int(os.getenv("LONGHOUSE_EMBEDDING_PROJECTOR_WORKERS", "4")))


class EmbeddingsV2Projector:
    def __init__(
        self, *, catalog: CatalogClient, search: CatalogClient, render_workers: RenderObjectWorkerPool, worker_id: str | None = None
    ) -> None:
        self.catalog = catalog
        self.search = search
        self.render_workers = render_workers
        self.worker_id = worker_id or f"embeddings-v2:{os.getpid()}"
        self._bound_store_id: str | None = None

    async def run_once(self, *, limit: int = 4, now: datetime | None = None) -> int:
        observed_at = now or datetime.now(UTC)
        await self._ensure_store_binding(observed_at)
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
            raise ValueError("catalog returned invalid embedding claims")
        for state in states:
            await self._run_claim(state, claim_token)
        return len(states)

    async def _ensure_store_binding(self, observed_at: datetime) -> None:
        ping = await self.search.call("search.ping.v2")
        store_id = _uuid(ping.get("store_id"))
        generation = ping.get("schema_generation")
        if not isinstance(generation, str) or not generation:
            raise ValueError("searchd omitted schema generation")
        if store_id != self._bound_store_id:
            await self.catalog.call(
                "projector.store.bind.v2",
                {"projector": PROJECTOR, "store_id": store_id, "schema_generation": generation, "observed_at": observed_at.isoformat()},
            )
            self._bound_store_id = store_id

    async def _run_claim(self, state: object, claim_token: str) -> None:
        session_id = state.get("session_id") if isinstance(state, dict) else None
        try:
            if not isinstance(state, dict):
                raise ValueError("catalog returned invalid embedding claim")
            session_id = _uuid(session_id)
            revision = int(str(state["claimed_revision"]))
            complete = await self._project(session_id=session_id, claimed_revision=revision)
            if not complete:
                failed_at = datetime.now(UTC)
                # Partial work is released promptly without increasing failure_count.
                await self.catalog.call(
                    "projector.state.fail.v2",
                    {
                        "projector": PROJECTOR,
                        "session_id": session_id,
                        "claim_token": claim_token,
                        "error_code": "partial_progress",
                        "error_message": "embedding pass reached chunk limit",
                        "failed_at": failed_at.isoformat(),
                        "retry_at": (failed_at + timedelta(seconds=1)).isoformat(),
                    },
                )
                return
            await self.catalog.call(
                "projector.state.complete.v2",
                {
                    "projector": PROJECTOR,
                    "session_id": session_id,
                    "claim_token": claim_token,
                    "completed_revision": revision,
                    "completed_at": datetime.now(UTC).isoformat(),
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # PermanentEmbeddingConfigError (bad provider/model config, a
            # persistent dims mismatch) will produce the identical failure on
            # every retry -- fast exponential backoff just burns API calls on
            # something a human has to fix. Every other exception here
            # (including this file's own ValueErrors for catalog-side
            # conditions like revision drift or a corrupt render page) is
            # genuinely transient and should keep retrying quickly, so this
            # must check the specific subclass, not ValueError broadly.
            is_permanent = isinstance(exc, PermanentEmbeddingConfigError)
            if isinstance(session_id, str):
                failures = int(state.get("failure_count", 0)) if isinstance(state, dict) else 0
                failed_at = datetime.now(UTC)
                retry_delay = timedelta(hours=24) if is_permanent else timedelta(seconds=min(300, 5 * 2 ** min(failures, 6)))
                await self.catalog.call(
                    "projector.state.fail.v2",
                    {
                        "projector": PROJECTOR,
                        "session_id": session_id,
                        "claim_token": claim_token,
                        "error_code": "embedding_config_permanent" if is_permanent else "embedding_projection_failed",
                        "error_message": str(exc)[:2048] or type(exc).__name__,
                        "failed_at": failed_at.isoformat(),
                        "retry_at": (failed_at + retry_delay).isoformat(),
                    },
                )
            (logger.error if is_permanent else logger.warning)("Embedding projection failed session=%s error=%s", session_id, exc)

    async def _project(self, *, session_id: str, claimed_revision: int) -> bool:
        from zerg.models_config import get_embedding_config

        config = get_embedding_config()
        if config is None:
            return True
        generation_id: str | None = None
        after_object_id: str | None = None
        records: list[dict[str, object]] = []
        owner_id: str | None = None
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
                return True
            if page.get("found") is not True or str(page.get("snapshot_revision")) != str(claimed_revision):
                raise ValueError("catalog render snapshot is unavailable or drifted")
            if page.get("generation_id") is None:
                # Session exists but has never been rendered (render_state
                # 'pending', no current_render_generation) -- seen on
                # zero-message CI/benchmark artifacts. There is nothing to
                # embed, and this is permanent for this revision, not a
                # transient catalog hiccup: calling _uuid(None) here raised
                # "badly formed hexadecimal UUID string" and got retried
                # forever at real cost, since it can never resolve on retry.
                return True
            page_generation = _uuid(page.get("generation_id"))
            if owner_id is None:
                session = page.get("session")
                if not isinstance(session, dict) or session.get("owner_id") is None:
                    raise ValueError("catalog omitted embedding session owner")
                owner_id = str(session["owner_id"])
            generation_id = generation_id or page_generation
            if page_generation != generation_id:
                raise ValueError("render generation drifted")
            objects = page.get("objects")
            if not isinstance(objects, list):
                raise ValueError("catalog returned invalid render objects")
            for manifest in objects:
                if not isinstance(manifest, dict):
                    raise ValueError("catalog returned invalid render manifest")
                object_id = _hash(manifest.get("object_id"))
                object_path = manifest.get("object_path")
                if not isinstance(object_path, str) or not object_path:
                    raise ValueError("render object path is invalid")
                decoded = await self.render_workers.read(object_path, _hash(manifest.get("object_hash")), lane="background")
                if (
                    str(decoded.spec.session_id) != session_id
                    or str(decoded.spec.render_generation) != generation_id
                    or decoded.object_hash != object_id
                ):
                    raise ValueError("render object identity does not match manifest")
                records.extend(
                    {
                        "role": record.role,
                        "content_text": record.content_text,
                        "tool_name": record.tool_name,
                        "tool_output_text": record.tool_output_text,
                        "timestamp": record.order_time_us,
                        "machine_id": decoded.spec.machine_id,
                        "provider": decoded.spec.provider,
                        "opaque_source_id": decoded.spec.opaque_source_id,
                        "source_epoch": str(decoded.spec.source_epoch),
                        "source_position": record.source_position,
                        "event_subordinal": record.event_subordinal,
                    }
                    for record in decoded.spec.records
                )
            if page.get("has_more") is not True:
                break
            if not objects:
                raise ValueError("catalog returned empty truncated page")
            after_object_id = _hash(objects[-1].get("object_id"))
        if generation_id is None:
            return True
        # The shared chunker sorts datetime timestamps only. Order records here and
        # assign monotonic ids so its stable fallback preserves catalog ordering.
        records.sort(
            key=lambda record: (
                int(record["timestamp"]),
                str(record["machine_id"]),
                str(record["provider"]),
                str(record["opaque_source_id"]),
                str(record["source_epoch"]),
                int(record["source_position"]),
                int(record["event_subordinal"]),
            )
        )
        for index, record in enumerate(records):
            record["id"] = index
        chunks = list(iter_turn_chunks(records))
        hashes_result = await self.search.call(
            "search.embedding.hashes.v2", {"session_id": session_id, "model": config.model, "dims": config.dims}
        )
        hashes = {int(key): value for key, value in (hashes_result.get("hashes") or {}).items() if isinstance(value, str)}
        all_missing = [chunk for chunk in chunks if hashes.get(chunk.chunk_index) != chunk.content_hash]
        missing = all_missing[: max(1, EMBEDDING_MAX_CHUNKS_PER_PASS)]
        complete = len(all_missing) == len(missing)
        # `desired_ordinals` is every episode this session should currently have,
        # including ones whose hash already matched and were therefore never
        # sent as `episodes` in any batch below. The searchd write handler only
        # deletes stale episode_embeddings rows on a `complete=True` call, using
        # this list (not the batch's own `episodes`) to decide what to keep --
        # otherwise "complete" on a partial batch would delete every chunk not
        # rewritten in that exact call, including already-current ones.
        desired_ordinals = [chunk.chunk_index for chunk in chunks]
        batches = [missing[start : start + max(1, EMBEDDING_BATCH_SIZE)] for start in range(0, len(missing), max(1, EMBEDDING_BATCH_SIZE))]
        for index, batch in enumerate(batches):
            is_last_batch = complete and index == len(batches) - 1
            vectors = await generate_embeddings([chunk.text for chunk in batch], config)
            await self.search.call(
                "search.embedding.write.v2",
                {
                    "session_id": session_id,
                    "owner_id": owner_id,
                    "generation_id": generation_id,
                    "revision": str(claimed_revision),
                    "model": config.model,
                    "dims": config.dims,
                    "complete": is_last_batch,
                    "desired_episode_ordinals": desired_ordinals if is_last_batch else None,
                    "episodes": [
                        {
                            "episode_ordinal": chunk.chunk_index,
                            "event_index_start": chunk.event_index_start,
                            "event_index_end": chunk.event_index_end,
                            # Clean-message indices are unresolvable outside this
                            # module. The chunker hands back the source record id
                            # of the episode's first event, and the ids assigned
                            # above are positions in `records`, so the record's
                            # own order time is the locator searchd can use to
                            # place the episode in the published generation.
                            "start_order_time_us": _record_order_time(records, chunk.source_event_id_start),
                            "content_hash": chunk.content_hash,
                            "embedding": base64.b64encode(embedding_to_bytes(vector)).decode("ascii"),
                        }
                        for chunk, vector in zip(batch, vectors, strict=True)
                    ],
                },
            )
        if complete and not missing:
            await self.search.call(
                "search.embedding.write.v2",
                {
                    "session_id": session_id,
                    "owner_id": owner_id,
                    "generation_id": generation_id,
                    "revision": str(claimed_revision),
                    "model": config.model,
                    "dims": config.dims,
                    "complete": True,
                    "desired_episode_ordinals": desired_ordinals,
                    "episodes": [],
                },
            )
        return complete


def _record_order_time(records: list[dict], record_index: object) -> int | None:
    """Order time of the record a chunk starts at, or None if it cannot be placed.

    ``record_index`` is a position in the sorted ``records`` list because the
    caller stamps ``record["id"] = index`` before chunking. Returning None on a
    miss is deliberate: an unplaceable episode must report unavailable evidence
    rather than borrow some other event's position.
    """

    if not isinstance(record_index, int) or isinstance(record_index, bool):
        return None
    if record_index < 0 or record_index >= len(records):
        return None
    return int(records[record_index]["timestamp"])


def _uuid(value: object) -> str:
    parsed = UUID(str(value))
    if str(parsed) != value:
        raise ValueError("UUID is not canonical")
    return str(parsed)


def _hash(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("hash is invalid")
    return value


_task: asyncio.Task[None] | None = None


async def _run_worker(projector: EmbeddingsV2Projector) -> None:
    while True:
        try:
            await asyncio.sleep(0 if await projector.run_once(limit=1) else 0.5)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Embedding projector tick failed")
            await asyncio.sleep(1)


async def _run_forever(projector: EmbeddingsV2Projector, *, worker_count: int = PROJECTOR_WORKERS) -> None:
    await asyncio.gather(*(_run_worker(projector) for _ in range(max(1, worker_count))))


def start_embeddings_v2_projector() -> bool:
    global _task
    if _task is not None and not _task.done():
        return True
    from zerg.services.catalogd_supervisor import get_catalogd_projector_client
    from zerg.services.searchd_supervisor import get_searchd_projector_client

    catalog = get_catalogd_projector_client()
    search = get_searchd_projector_client()
    if catalog is None or search is None:
        return False
    _task = asyncio.create_task(
        _run_forever(EmbeddingsV2Projector(catalog=catalog, search=search, render_workers=get_render_object_worker_pool())),
        name="embeddings-v2-projector",
    )
    return True


async def stop_embeddings_v2_projector() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    _task = None
