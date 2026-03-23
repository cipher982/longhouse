"""Tests for commis update helper module."""

from zerg.services.commis_updates import build_commis_update_content
from zerg.services.commis_updates import queue_commis_update


def test_build_commis_update_content_success():
    content = build_commis_update_content(
        commis_job_id=10,
        commis_task="summarize recent failures",
        commis_status="success",
        commis_result="all good",
        commis_error=None,
    )

    assert "Job ID: 10" in content
    assert "Status: success" in content
    assert "Task: summarize recent failures" in content
    assert "Result: all good" in content


def test_queue_commis_update_creates_internal_thread_message(monkeypatch):
    calls = []

    def _fake_create_thread_message(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("zerg.services.commis_updates.create_thread_message", _fake_create_thread_message)

    db = object()
    queue_commis_update(
        db=db,
        thread_id=77,
        commis_job_id=22,
        commis_task="collect evidence",
        commis_status="failed",
        commis_result="",
        commis_error="timeout",
    )

    assert len(calls) == 1
    assert calls[0]["db"] is db
    assert calls[0]["thread_id"] == 77
    assert calls[0]["role"] == "user"
    assert calls[0]["processed"] is False
    assert calls[0]["internal"] is True
    assert "Status: failed" in calls[0]["content"]
    assert "Error: timeout" in calls[0]["content"]
