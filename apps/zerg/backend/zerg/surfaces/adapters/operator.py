"""Operator-mode implementation of the SurfaceAdapter contract."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from typing import Any

from zerg.surfaces.base import SurfaceInboundEvent


class OperatorSurfaceAdapter:
    """Adapter for proactive operator-mode wakeups."""

    surface_id = "operator"
    mode = "inline"

    def __init__(self, *, owner_id: int, conversation_id: str = "operator:main") -> None:
        self._owner_id = owner_id
        self._conversation_id = conversation_id

    async def normalize_inbound(self, raw_input: Any) -> SurfaceInboundEvent | None:
        payload = raw_input if isinstance(raw_input, dict) else {}
        text = str(payload.get("message", "") or "").strip()
        if not text:
            return None

        message_id = str(payload.get("message_id", "") or "").strip()
        if not message_id:
            raise ValueError("missing message_id")

        owner_hint = str(payload.get("owner_id", "") or "").strip()
        if not owner_hint:
            raise ValueError("missing owner_id")

        conversation_id = str(payload.get("conversation_id", "") or self._conversation_id)
        dedupe_key = f"operator:{owner_hint}:{message_id}"

        return SurfaceInboundEvent(
            surface_id=self.surface_id,
            conversation_id=conversation_id,
            dedupe_key=dedupe_key,
            owner_hint=owner_hint,
            source_message_id=message_id,
            source_event_id=None,
            text=text,
            timestamp_utc=datetime.now(timezone.utc),
            raw=dict(payload),
        )

    async def resolve_owner_id(self, event: SurfaceInboundEvent, _db) -> int | None:
        if not event.owner_hint:
            raise ValueError("missing owner_hint")
        try:
            owner_id = int(event.owner_hint)
        except ValueError as exc:
            raise ValueError("invalid owner_hint") from exc
        if owner_id != self._owner_id:
            raise ValueError("owner mismatch")
        return owner_id

    def build_run_kwargs(self, event: SurfaceInboundEvent) -> dict[str, Any]:
        run_id_raw = (event.raw or {}).get("run_id")
        if run_id_raw is None:
            raise ValueError("missing run_id")
        try:
            run_id = int(run_id_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid run_id") from exc

        timeout_raw = (event.raw or {}).get("timeout", 600)
        try:
            timeout = int(timeout_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid timeout") from exc

        run_kwargs: dict[str, Any] = {
            "run_id": run_id,
            "message_id": event.source_message_id,
            "timeout": timeout,
            "return_on_deferred": bool((event.raw or {}).get("return_on_deferred", False)),
        }
        trace_id = (event.raw or {}).get("trace_id")
        if trace_id:
            run_kwargs["trace_id"] = str(trace_id)
        model_override = (event.raw or {}).get("model_override")
        if model_override:
            run_kwargs["model_override"] = str(model_override)
        reasoning_effort = (event.raw or {}).get("reasoning_effort")
        if reasoning_effort:
            run_kwargs["reasoning_effort"] = str(reasoning_effort)
        return run_kwargs

    async def deliver(self, *, owner_id: int, text: str, event: SurfaceInboundEvent) -> None:
        del owner_id
        del text
        del event
