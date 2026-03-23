"""Prompt helpers for commis inbox continuation runs."""

from __future__ import annotations


def build_commis_inbox_synthetic_task(
    *,
    commis_result: str,
    commis_status: str,
    commis_task: str,
    commis_error: str | None,
    queued_result_sentinel: str,
) -> str:
    """Build synthetic task text consumed by oikos inbox continuation runs."""
    if commis_result == queued_result_sentinel:
        return (
            "[Commis inbox] One or more background commiss completed while another response was running.\n\n"
            "Please review the latest internal commis updates in the thread and summarize them clearly for the user."
        )

    if commis_status == "failed":
        return (
            "[Commis inbox] A background commis failed.\n\n"
            f"Original task: {commis_task[:200]}\n\n"
            f"Error: {commis_error or 'Unknown error'}\n\n"
            "Please acknowledge the failure and explain what happened to the user."
        )

    return (
        "[Commis inbox] A background commis has completed and returned results.\n\n"
        f"Original task: {commis_task[:200]}\n\n"
        f"Commis result:\n{commis_result}\n\n"
        "Please synthesize these findings and present them clearly to the user."
    )
