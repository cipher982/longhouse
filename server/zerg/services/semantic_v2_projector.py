"""Durable semantic convergence for legacy storage-v2 sessions."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import uuid4

from zerg.catalogd.client import CatalogClient
from zerg.runtime_boot import RUNTIME_BOOT_ID
from zerg.services.raw_object_workers import RawObjectWorkerPool
from zerg.services.raw_object_workers import get_raw_object_worker_pool
from zerg.services.render_object_workers import RenderObjectWorkerPool
from zerg.services.render_object_workers import get_render_object_worker_pool
from zerg.services.storage_v2_semantics import StorageV2SemanticRecoveryPermanentError
from zerg.services.storage_v2_semantics import repair_storage_session_semantic_projection

logger = logging.getLogger(__name__)

PROJECTOR = "semantic-v2"
PROJECTOR_IDLE_POLL_SECONDS = 5.0
PROJECTOR_LEASE_SECONDS = 3_600


class SemanticV2Projector:
    def __init__(
        self,
        *,
        catalog: CatalogClient,
        render_workers: RenderObjectWorkerPool,
        raw_workers: RawObjectWorkerPool,
        worker_id: str | None = None,
    ) -> None:
        self.catalog = catalog
        self.render_workers = render_workers
        self.raw_workers = raw_workers
        self.worker_id = worker_id or f"semantic-v2:{RUNTIME_BOOT_ID}"

    async def run_once(self, *, limit: int = 4, now: datetime | None = None) -> int:
        observed_at = now or datetime.now(UTC)
        claim_token = str(uuid4())
        result = await self.catalog.call(
            "projector.state.claim.v2",
            {
                "projector": PROJECTOR,
                "worker_id": self.worker_id,
                "claim_token": claim_token,
                "now": observed_at.isoformat(),
                "lease_seconds": PROJECTOR_LEASE_SECONDS,
                "limit": limit,
            },
        )
        states = result.get("claimed")
        if not isinstance(states, list):
            raise RuntimeError("catalog returned an invalid semantic projector claim")
        await asyncio.gather(*(self._run_claim(state, claim_token=claim_token) for state in states))
        return len(states)

    async def _run_claim(self, state: object, *, claim_token: str) -> None:
        session_id = str(state.get("session_id") or "") if isinstance(state, dict) else ""
        try:
            if not isinstance(state, dict) or not session_id:
                raise RuntimeError("catalog returned an invalid semantic projector row")
            claimed_revision = int(state["claimed_revision"])
            await self._project(session_id=session_id)
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
        except Exception as exc:  # noqa: BLE001 - failure is persisted on the claim
            failed_at = datetime.now(UTC)
            permanent = isinstance(exc, StorageV2SemanticRecoveryPermanentError)
            retry_seconds = 3_600 if permanent else 60
            if session_id:
                await self.catalog.call(
                    "projector.state.fail.v2",
                    {
                        "projector": PROJECTOR,
                        "session_id": session_id,
                        "claim_token": claim_token,
                        "error_code": "semantic_recovery_permanent" if permanent else "semantic_recovery_pending",
                        "error_message": (str(exc) or type(exc).__name__)[:2_048],
                        "failed_at": failed_at.isoformat(),
                        "retry_at": (failed_at + timedelta(seconds=retry_seconds)).isoformat(),
                    },
                )
            logger.warning("Semantic-v2 projection failed session=%s error=%s", session_id, exc)

    async def _project(self, *, session_id: str) -> None:
        response = await self.catalog.call("storage.session.read.v2", {"session_id": session_id})
        if response.get("found") is not True:
            # Deleted and retired sessions have no semantic work left. Their
            # ledger still converges so a stale claim cannot spin forever.
            return
        session = response.get("session")
        if not isinstance(session, dict):
            raise RuntimeError("catalog omitted storage session facts")
        if str(session.get("provider") or "").lower() != "claude":
            return
        if int(session.get("semantic_projection_version") or 0) >= 1:
            return
        owner_id = session.get("owner_id")
        generation_id = session.get("current_render_generation")
        if owner_id is None or not generation_id:
            raise RuntimeError("Claude semantic projection is waiting for its current render generation")
        repaired = await repair_storage_session_semantic_projection(
            catalog=self.catalog,
            render_workers=self.render_workers,
            raw_workers=self.raw_workers,
            session_id=session_id,
            owner_id=str(owner_id),
            generation_id=str(generation_id),
        )
        if repaired.get("complete") is not True:
            raise RuntimeError("semantic projection repair did not converge")


_task: asyncio.Task[None] | None = None


async def _run_worker(projector: SemanticV2Projector) -> None:
    while True:
        try:
            claimed = await projector.run_once()
            await asyncio.sleep(0 if claimed else PROJECTOR_IDLE_POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Semantic-v2 projector tick failed")
            await asyncio.sleep(1.0)


def start_semantic_v2_projector() -> bool:
    global _task
    if _task is not None and not _task.done():
        return True
    from zerg.services.catalogd_supervisor import get_catalogd_projector_client

    catalog = get_catalogd_projector_client()
    if catalog is None:
        return False
    projector = SemanticV2Projector(
        catalog=catalog,
        render_workers=get_render_object_worker_pool(),
        raw_workers=get_raw_object_worker_pool(),
    )
    _task = asyncio.create_task(_run_worker(projector), name="semantic-v2-projector")
    return True


async def stop_semantic_v2_projector() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        await asyncio.gather(_task, return_exceptions=True)
    _task = None


__all__ = ["SemanticV2Projector", "start_semantic_v2_projector", "stop_semantic_v2_projector"]
