"""Tests for commis inbox synthetic prompt helper."""

from zerg.services.commis_inbox_prompt import build_commis_inbox_synthetic_task


def test_build_commis_inbox_synthetic_task_for_queued_updates():
    task = build_commis_inbox_synthetic_task(
        commis_result="__QUEUED__",
        commis_status="success",
        commis_task="ignored",
        commis_error=None,
        queued_result_sentinel="__QUEUED__",
    )

    assert "background commiss completed while another response was running" in task
    assert "latest internal commis updates" in task


def test_build_commis_inbox_synthetic_task_for_failed_commis():
    task = build_commis_inbox_synthetic_task(
        commis_result="ignored",
        commis_status="failed",
        commis_task="collect outage details",
        commis_error="timeout",
        queued_result_sentinel="__QUEUED__",
    )

    assert "A background commis failed" in task
    assert "Original task: collect outage details" in task
    assert "Error: timeout" in task


def test_build_commis_inbox_synthetic_task_for_successful_commis():
    task = build_commis_inbox_synthetic_task(
        commis_result="Key findings here",
        commis_status="success",
        commis_task="analyze logs",
        commis_error=None,
        queued_result_sentinel="__QUEUED__",
    )

    assert "A background commis has completed and returned results" in task
    assert "Original task: analyze logs" in task
    assert "Commis result:\nKey findings here" in task
