"""Compatibility facade for commis resume flows.

Primary implementations now live in dedicated modules:
- `commis_batch_resume.py` for parallel/batch continuation
- `commis_single_resume.py` for single-result continuation
- `commis_inbox_trigger.py` for post-success inbox follow-up runs
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from zerg.services.commis_runner import RunnerFactory
from zerg.services.commis_runner import default_runner_factory as _default_runner_factory


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
