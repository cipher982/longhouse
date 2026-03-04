"""Helpers for queueing commis update context into oikos threads."""

from __future__ import annotations

from sqlalchemy.orm import Session


def build_commis_update_content(
    *,
    commis_job_id: int,
    commis_task: str,
    commis_status: str,
    commis_result: str,
    commis_error: str | None,
) -> str:
    """Build normalized thread context content for a commis job update."""
    if commis_status == "failed":
        summary = f"Error: {commis_error or 'Unknown error'}"
    else:
        summary = f"Result: {commis_result[:500]}"
    return (
        "[Commis update]\n"
        f"Job ID: {commis_job_id}\n"
        f"Status: {commis_status}\n"
        f"Task: {commis_task[:200]}\n"
        f"{summary}\n\n"
        "If you need full details, call get_commis_evidence(job_id=...)."
    )


def queue_commis_update(
    *,
    db: Session,
    thread_id: int,
    commis_job_id: int,
    commis_task: str,
    commis_status: str,
    commis_result: str,
    commis_error: str | None,
) -> None:
    """Insert an internal user-role message so next continuation sees the update."""
    from zerg.crud import crud

    context_content = build_commis_update_content(
        commis_job_id=commis_job_id,
        commis_task=commis_task,
        commis_status=commis_status,
        commis_result=commis_result,
        commis_error=commis_error,
    )

    crud.create_thread_message(
        db=db,
        thread_id=thread_id,
        role="user",  # Use "user" role so Runner includes it
        content=context_content,
        processed=False,  # Unprocessed so it gets picked up
        internal=True,  # Internal flag for UI filtering
    )
