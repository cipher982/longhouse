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
from zerg.services.session_processing.embeddings import embedding_to_bytes
from zerg.services.session_processing.embeddings import generate_embeddings
from zerg.services.session_processing.embeddings import iter_turn_chunks

logger = logging.getLogger(__name__)
PROJECTOR = "embeddings-v1"
PAGE_SIZE = 100


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
            await self._project(session_id=session_id, claimed_revision=revision)
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
            if isinstance(session_id, str):
                failures = int(state.get("failure_count", 0)) if isinstance(state, dict) else 0
                failed_at = datetime.now(UTC)
                await self.catalog.call(
                    "projector.state.fail.v2",
                    {
                        "projector": PROJECTOR,
                        "session_id": session_id,
                        "claim_token": claim_token,
                        "error_code": "embedding_projection_failed",
                        "error_message": str(exc)[:2048] or type(exc).__name__,
                        "failed_at": failed_at.isoformat(),
                        "retry_at": (failed_at + timedelta(seconds=min(300, 5 * 2 ** min(failures, 6)))).isoformat(),
                    },
                )
            logger.warning("Embedding projection failed session=%s error=%s", session_id, exc)

    async def _project(self, *, session_id: str, claimed_revision: int) -> None:
        from zerg.models_config import get_embedding_config

        config = get_embedding_config()
        if config is None:
            return
        generation_id: str | None = None
        after_object_id: str | None = None
        records: list[dict[str, object]] = []
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
                return
            if page.get("found") is not True or str(page.get("snapshot_revision")) != str(claimed_revision):
                raise ValueError("catalog render snapshot is unavailable or drifted")
            page_generation = _uuid(page.get("generation_id"))
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
                        "id": ordinal,
                    }
                    for ordinal, record in enumerate(decoded.spec.records, start=len(records))
                )
            if page.get("has_more") is not True:
                break
            if not objects:
                raise ValueError("catalog returned empty truncated page")
            after_object_id = _hash(objects[-1].get("object_id"))
        if generation_id is None:
            return
        # The shared chunker sorts datetime timestamps only. Order records here and
        # assign monotonic ids so its stable fallback preserves catalog ordering.
        records.sort(key=lambda record: (int(record["timestamp"]), int(record["id"])))
        for index, record in enumerate(records):
            record["id"] = index
        chunks = list(iter_turn_chunks(records))
        hashes_result = await self.search.call("search.embedding.hashes.v2", {"session_id": session_id, "model": config.model})
        hashes = {int(key): value for key, value in (hashes_result.get("hashes") or {}).items() if isinstance(value, str)}
        missing = [chunk for chunk in chunks if hashes.get(chunk.chunk_index) != chunk.content_hash][
            : max(1, EMBEDDING_MAX_CHUNKS_PER_PASS)
        ]
        for start in range(0, len(missing), max(1, EMBEDDING_BATCH_SIZE)):
            batch = missing[start : start + max(1, EMBEDDING_BATCH_SIZE)]
            vectors = await generate_embeddings([chunk.text for chunk in batch], config)
            await self.search.call(
                "search.embedding.write.v2",
                {
                    "session_id": session_id,
                    "generation_id": generation_id,
                    "model": config.model,
                    "dims": config.dims,
                    "episodes": [
                        {
                            "episode_ordinal": chunk.chunk_index,
                            "event_index_start": chunk.event_index_start,
                            "event_index_end": chunk.event_index_end,
                            "content_hash": chunk.content_hash,
                            "embedding": base64.b64encode(embedding_to_bytes(vector)).decode("ascii"),
                        }
                        for chunk, vector in zip(batch, vectors, strict=True)
                    ],
                },
            )


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


async def _run_forever(projector: EmbeddingsV2Projector) -> None:
    while True:
        try:
            await asyncio.sleep(0 if await projector.run_once() else 0.5)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Embedding projector tick failed")
            await asyncio.sleep(1)


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
