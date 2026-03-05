"""Shared ingress orchestrator for Oikos surface adapters."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from typing import Any
from typing import Callable

from zerg.database import db_session
from zerg.surfaces.base import SurfaceAdapter
from zerg.surfaces.base import SurfaceHandleResult
from zerg.surfaces.base import SurfaceHandleStatus
from zerg.surfaces.idempotency import SurfaceIdempotencyError
from zerg.surfaces.idempotency import SurfaceIngressClaimStore

if TYPE_CHECKING:
    from zerg.services.oikos_service import OikosService

logger = logging.getLogger(__name__)

_ALLOWED_RUN_KWARGS = {
    "run_id",
    "message_id",
    "trace_id",
    "timeout",
    "model_override",
    "reasoning_effort",
    "return_on_deferred",
}


class SurfaceOrchestrator:
    """Normalize + dedupe + run Oikos for inbound surface events."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any] = db_session,
        oikos_service_cls: type["OikosService"] | None = None,
    ) -> None:
        self._session_factory = session_factory
        if oikos_service_cls is None:
            from zerg.services.oikos_service import OikosService

            self._oikos_service_cls: type[Any] = OikosService
        else:
            self._oikos_service_cls = oikos_service_cls

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

            claim_store = SurfaceIngressClaimStore(db)
            try:
                claimed = claim_store.claim(
                    owner_id=owner_id,
                    surface_id=event.surface_id,
                    dedupe_key=event.dedupe_key,
                    conversation_id=event.conversation_id,
                    source_event_id=event.source_event_id,
                    source_message_id=event.source_message_id,
                )
            except SurfaceIdempotencyError:
                logger.exception(
                    "SurfaceOrchestrator: idempotency claim failed for %s key %s",
                    event.surface_id,
                    event.dedupe_key,
                )
                return SurfaceHandleResult(
                    status=SurfaceHandleStatus.REJECTED,
                    surface_id=event.surface_id,
                    owner_id=owner_id,
                    dedupe_key=event.dedupe_key,
                    message="idempotency claim failed",
                )

            if not claimed:
                return SurfaceHandleResult(
                    status=SurfaceHandleStatus.DUPLICATE,
                    surface_id=event.surface_id,
                    owner_id=owner_id,
                    dedupe_key=event.dedupe_key,
                )

            try:
                run_kwargs = adapter.build_run_kwargs(event) or {}
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "SurfaceOrchestrator: build_run_kwargs failed for %s key %s",
                    event.surface_id,
                    event.dedupe_key,
                )
                return SurfaceHandleResult(
                    status=SurfaceHandleStatus.REJECTED,
                    surface_id=event.surface_id,
                    owner_id=owner_id,
                    dedupe_key=event.dedupe_key,
                    message=f"build_run_kwargs failed: {exc}",
                )
            invalid = sorted(set(run_kwargs.keys()) - _ALLOWED_RUN_KWARGS)
            if invalid:
                return SurfaceHandleResult(
                    status=SurfaceHandleStatus.REJECTED,
                    surface_id=event.surface_id,
                    owner_id=owner_id,
                    dedupe_key=event.dedupe_key,
                    message=f"invalid run kwargs: {', '.join(invalid)}",
                )

            service = self._oikos_service_cls(db)
            try:
                result = await service.run_oikos(
                    owner_id=owner_id,
                    task=event.text,
                    source_surface_id=event.surface_id,
                    source_conversation_id=event.conversation_id,
                    source_message_id=event.source_message_id,
                    source_event_id=event.source_event_id,
                    source_idempotency_key=event.dedupe_key,
                    **run_kwargs,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "SurfaceOrchestrator: run_oikos failed for %s key %s",
                    event.surface_id,
                    event.dedupe_key,
                )
                return SurfaceHandleResult(
                    status=SurfaceHandleStatus.REJECTED,
                    surface_id=event.surface_id,
                    owner_id=owner_id,
                    dedupe_key=event.dedupe_key,
                    message=f"run_oikos failed: {exc}",
                )

            if adapter.mode == "push":
                text = result.result or "Done."
                try:
                    await adapter.deliver(owner_id=owner_id, text=text, event=event)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "SurfaceOrchestrator: delivery failed for %s key %s",
                        event.surface_id,
                        event.dedupe_key,
                    )
                    return SurfaceHandleResult(
                        status=SurfaceHandleStatus.DELIVERY_FAILED,
                        surface_id=event.surface_id,
                        owner_id=owner_id,
                        dedupe_key=event.dedupe_key,
                        run_id=result.run_id,
                    )

            return SurfaceHandleResult(
                status=SurfaceHandleStatus.PROCESSED,
                surface_id=event.surface_id,
                owner_id=owner_id,
                dedupe_key=event.dedupe_key,
                run_id=result.run_id,
            )
