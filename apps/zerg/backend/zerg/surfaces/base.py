"""Core types and contracts for Oikos surface adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any
from typing import Literal
from typing import Protocol

from sqlalchemy.orm import Session

SurfaceMode = Literal["inline", "push"]


@dataclass(frozen=True)
class SurfaceInboundEvent:
    """Normalized inbound message from any surface transport."""

    surface_id: str
    conversation_id: str
    dedupe_key: str
    owner_hint: str | None
    source_message_id: str | None
    source_event_id: str | None
    text: str
    timestamp_utc: datetime
    raw: dict[str, Any]


class SurfaceHandleStatus(str, Enum):
    """Result status for one handled ingress event."""

    IGNORED = "ignored"
    REJECTED = "rejected"
    UNRESOLVED_OWNER = "unresolved_owner"
    DUPLICATE = "duplicate"
    PROCESSED = "processed"
    DELIVERY_FAILED = "delivery_failed"


@dataclass(frozen=True)
class SurfaceHandleResult:
    """Outcome of orchestrating one inbound surface event."""

    status: SurfaceHandleStatus
    surface_id: str
    dedupe_key: str | None = None
    owner_id: int | None = None
    run_id: int | None = None
    thread_id: int | None = None
    run_status: str | None = None
    response_text: str | None = None
    message: str | None = None


class SurfaceAdapter(Protocol):
    """Surface adapter contract for Oikos ingress and optional delivery."""

    surface_id: str
    mode: SurfaceMode

    async def normalize_inbound(self, raw_input: Any) -> SurfaceInboundEvent | None:
        """Validate + normalize transport payload into canonical event format."""

    async def resolve_owner_id(self, event: SurfaceInboundEvent, db: Session) -> int | None:
        """Map surface event context to a Longhouse owner ID."""

    def build_run_kwargs(self, event: SurfaceInboundEvent) -> dict[str, Any]:
        """Return additional kwargs for OikosService.run_oikos()."""

    async def deliver(self, *, owner_id: int, text: str, event: SurfaceInboundEvent) -> None:
        """Deliver assistant output for push surfaces (inline surfaces can no-op)."""
