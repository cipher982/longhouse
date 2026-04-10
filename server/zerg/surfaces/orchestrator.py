"""Shared ingress orchestrator for surface adapters (Oikos removed)."""

from __future__ import annotations

import logging
from typing import Any
from typing import Callable

from zerg.database import db_session
from zerg.surfaces.base import SurfaceAdapter
from zerg.surfaces.base import SurfaceHandleResult
from zerg.surfaces.base import SurfaceHandleStatus

logger = logging.getLogger(__name__)

_ALLOWED_RUN_KWARGS = {
    "run_id",
    "message_id",
    "trace_id",
    "timeout",
    "model_override",
    "reasoning_effort",
    "return_on_deferred",
    "operator_capability_ceiling",
    "operator_target_session_id",
}


class SurfaceOrchestrator:
    """Normalize + dedupe for inbound surface events (Oikos backend removed)."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any] = db_session,
    ) -> None:
        self._session_factory = session_factory

    async def handle_inbound(self, adapter: SurfaceAdapter, raw_input: Any) -> SurfaceHandleResult:
        try:
            event = await adapter.normalize_inbound(raw_input)
        except Exception as exc:  # noqa: BLE001
            logger.exception("SurfaceOrchestrator: normalize_inbound failed for %s", adapter.surface_id)
            return SurfaceHandleResult(
                status=SurfaceHandleStatus.REJECTED,
                surface_id=adapter.surface_id,
                message=f"normalize failed: {exc}",
            )

        if event is None:
            return SurfaceHandleResult(status=SurfaceHandleStatus.IGNORED, surface_id=adapter.surface_id)

        if event.surface_id != adapter.surface_id:
            return SurfaceHandleResult(
                status=SurfaceHandleStatus.REJECTED,
                surface_id=adapter.surface_id,
                dedupe_key=event.dedupe_key,
                message="event surface mismatch",
            )

        if not event.dedupe_key:
            return SurfaceHandleResult(
                status=SurfaceHandleStatus.REJECTED,
                surface_id=adapter.surface_id,
                message="missing dedupe key",
            )

        if not event.text.strip():
            return SurfaceHandleResult(
                status=SurfaceHandleStatus.IGNORED,
                surface_id=event.surface_id,
                dedupe_key=event.dedupe_key,
            )

        with self._session_factory() as db:
            try:
                owner_id = await adapter.resolve_owner_id(event, db)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "SurfaceOrchestrator: resolve_owner_id failed for %s key %s",
                    event.surface_id,
                    event.dedupe_key,
                )
                return SurfaceHandleResult(
                    status=SurfaceHandleStatus.REJECTED,
                    surface_id=event.surface_id,
                    dedupe_key=event.dedupe_key,
                    message=f"resolve_owner failed: {exc}",
                )
            if owner_id is None:
                unresolved_handler = getattr(adapter, "handle_unresolved_owner", None)
                if callable(unresolved_handler):
                    await unresolved_handler(event)
                return SurfaceHandleResult(
                    status=SurfaceHandleStatus.UNRESOLVED_OWNER,
                    surface_id=event.surface_id,
                    dedupe_key=event.dedupe_key,
                    message="owner unresolved",
                )

            # Oikos backend removed — reject without claiming the dedupe key
            # so retries are not burned.
            return SurfaceHandleResult(
                status=SurfaceHandleStatus.REJECTED,
                surface_id=event.surface_id,
                owner_id=owner_id,
                dedupe_key=event.dedupe_key,
                message="Oikos backend removed",
            )
