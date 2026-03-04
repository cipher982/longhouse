"""Shared runner protocol and default factory for commis continuations."""

from __future__ import annotations

from typing import Any
from typing import Callable
from typing import Protocol
from typing import Sequence
from typing import runtime_checkable

from sqlalchemy.orm import Session


@runtime_checkable
class ContinuationRunner(Protocol):
    """Protocol for continuation-capable fiche runners."""

    usage_prompt_tokens: int | None
    usage_completion_tokens: int | None
    usage_total_tokens: int | None
    usage_reasoning_tokens: int | None

    async def run_continuation(
        self,
        db: Session,
        thread: Any,
        tool_call_id: str,
        tool_result: str,
        *,
        run_id: int | None = None,
        trace_id: str | None = None,
    ) -> Sequence[Any]: ...

    async def run_batch_continuation(
        self,
        db: Session,
        thread: Any,
        commis_results: list[dict],
        *,
        run_id: int | None = None,
        trace_id: str | None = None,
    ) -> Sequence[Any]: ...


RunnerFactory = Callable[..., ContinuationRunner]


def default_runner_factory(
    fiche: Any,
    *,
    model_override: str | None = None,
    reasoning_effort: str | None = None,
) -> ContinuationRunner:
    """Create the default continuation runner backed by FicheRunner."""
    from zerg.managers.fiche_runner import FicheRunner

    return FicheRunner(fiche, model_override=model_override, reasoning_effort=reasoning_effort)
