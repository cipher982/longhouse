"""Compatibility facade for commis resume flows.

Primary implementations now live in dedicated modules:
- `commis_batch_resume.py` for parallel/batch continuation
- `commis_single_resume.py` for single-result continuation
- `commis_inbox_trigger.py` for post-success inbox follow-up runs
"""

from __future__ import annotations

from typing import Any
from typing import Callable
from typing import Protocol
from typing import Sequence
from typing import runtime_checkable

from sqlalchemy.orm import Session


@runtime_checkable
class ContinuationRunner(Protocol):
    """Protocol for a runner that can execute continuations."""

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


def _default_runner_factory(
    fiche: Any,
    *,
    model_override: str | None = None,
    reasoning_effort: str | None = None,
) -> ContinuationRunner:
    """Create the default FicheRunner continuation runner."""
    from zerg.managers.fiche_runner import FicheRunner

    return FicheRunner(fiche, model_override=model_override, reasoning_effort=reasoning_effort)


async def resume_oikos_batch(
    db: Session,
    run_id: int,
    commis_results: list[dict[str, Any]],
    *,
    runner_factory: RunnerFactory = _default_runner_factory,
) -> dict[str, Any] | None:
    """Resume oikos with all commis results (batch continuation)."""
    from zerg.services.commis_batch_resume import resume_oikos_batch as _resume_oikos_batch

    return await _resume_oikos_batch(
        db=db,
        run_id=run_id,
        commis_results=commis_results,
        runner_factory=runner_factory,
    )


async def resume_oikos_with_commis_result(
    db: Session,
    run_id: int,
    commis_result: str,
    job_id: int | None = None,
    *,
    runner_factory: RunnerFactory = _default_runner_factory,
) -> dict[str, Any] | None:
    """Resume an interrupted oikos run with a single commis result."""
    from zerg.services.commis_single_resume import resume_oikos_with_commis_result as _resume_oikos_with_commis_result

    return await _resume_oikos_with_commis_result(
        db=db,
        run_id=run_id,
        commis_result=commis_result,
        job_id=job_id,
        runner_factory=runner_factory,
    )


async def trigger_commis_inbox_run(
    db: Session,
    original_run_id: int,
    commis_job_id: int,
    commis_result: str,
    commis_status: str,
    commis_error: str | None = None,
) -> dict[str, Any]:
    """Compatibility wrapper around extracted inbox trigger module."""
    from zerg.services.commis_inbox_trigger import trigger_commis_inbox_run as _trigger_commis_inbox_run

    return await _trigger_commis_inbox_run(
        db=db,
        original_run_id=original_run_id,
        commis_job_id=commis_job_id,
        commis_result=commis_result,
        commis_status=commis_status,
        commis_error=commis_error,
    )
