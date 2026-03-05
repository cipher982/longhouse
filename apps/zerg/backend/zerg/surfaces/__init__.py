"""Surface adapter contracts and shared Oikos ingress orchestration."""

from zerg.surfaces.base import SurfaceAdapter
from zerg.surfaces.base import SurfaceHandleResult
from zerg.surfaces.base import SurfaceHandleStatus
from zerg.surfaces.base import SurfaceInboundEvent
from zerg.surfaces.base import SurfaceMode
from zerg.surfaces.idempotency import SurfaceIdempotencyError
from zerg.surfaces.idempotency import SurfaceIngressClaimStore
from zerg.surfaces.orchestrator import SurfaceOrchestrator
from zerg.surfaces.registry import SurfaceRegistry
from zerg.surfaces.registry import SurfaceRegistryError

__all__ = [
    "SurfaceAdapter",
    "SurfaceHandleResult",
    "SurfaceHandleStatus",
    "SurfaceInboundEvent",
    "SurfaceMode",
    "SurfaceIdempotencyError",
    "SurfaceIngressClaimStore",
    "SurfaceOrchestrator",
    "SurfaceRegistry",
    "SurfaceRegistryError",
]
