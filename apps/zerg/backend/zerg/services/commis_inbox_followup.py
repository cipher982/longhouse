"""Follow-up scheduling helpers for commis inbox continuations."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any

from zerg.models.enums import RunStatus
from zerg.models.models import Run

logger = logging.getLogger(__name__)

# Sentinel used when follow-up continuations should read queued commis updates from thread.
INBOX_QUEUED_RESULT = "(Queued commis updates available in thread)"

# Follow-up wait tuning (best-effort, avoids infinite background tasks)
INBOX_FOLLOWUP_TIMEOUT_S = 300
INBOX_FOLLOWUP_SLEEP_S = 0.5
INBOX_FOLLOWUP_MAX_SLEEP_S = 2.0

TriggerFollowupFn = Callable[..., Awaitable[dict[str, Any] | None]]


async def run_inbox_followup_after_run(
    *,
    run_id: int,
    commis_job_id: int,
    commis_status: str,
    commis_error: str | None,
    trigger_followup: TriggerFollowupFn,
    timeout_s: int = INBOX_FOLLOWUP_TIMEOUT_S,
) -> dict[str, Any] | None:
    """Wait for a run to finish, then trigger a continuation for queued commis updates."""
    from zerg.database import get_session_factory

    start = time.monotonic()
    sleep_s = INBOX_FOLLOWUP_SLEEP_S
    session_factory = get_session_factory()
    db = session_factory()
    try:
        while True:
            run = db.query(Run).filter(Run.id == run_id).first()
            if not run:
                return None
            if run.status in (RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.CANCELLED):
                break
            if (time.monotonic() - start) >= timeout_s:
                logger.info("Inbox follow-up timed out waiting for run %s to finish", run_id)
                return None
            await asyncio.sleep(sleep_s)
            sleep_s = min(sleep_s * 1.5, INBOX_FOLLOWUP_MAX_SLEEP_S)
            db.expire_all()

        # Trigger continuation using queued updates already in the thread.
        return await trigger_followup(
            db=db,
            original_run_id=run_id,
            commis_job_id=commis_job_id,
            commis_result=INBOX_QUEUED_RESULT,
            commis_status=commis_status,
            commis_error=commis_error,
        )
    finally:
        db.close()


def schedule_inbox_followup_after_run(
    *,
    run_id: int,
    commis_job_id: int,
    commis_status: str,
    commis_error: str | None,
    trigger_followup: TriggerFollowupFn,
) -> None:
    """Fire-and-forget scheduling for follow-up continuations."""
    coro = run_inbox_followup_after_run(
        run_id=run_id,
        commis_job_id=commis_job_id,
        commis_status=commis_status,
        commis_error=commis_error,
        trigger_followup=trigger_followup,
    )
    try:
        asyncio.create_task(coro, context=contextvars.Context())
    except Exception:
        coro.close()
        raise
