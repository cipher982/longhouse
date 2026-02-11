"""Integration tests for commis hook ingestion endpoint."""

from __future__ import annotations

import json

from fastapi import status

from zerg.config import get_settings
from zerg.dependencies import auth as auth_deps
from zerg.models.enums import RunStatus
from zerg.models.enums import RunTrigger
from zerg.models.models import CommisJob
from zerg.models.run import Run
from zerg.models.run_event import RunEvent


def _create_run_and_job(db_session, test_user, sample_fiche, sample_thread, *, status_value: str = "running"):
    run = Run(
        fiche_id=sample_fiche.id,
        thread_id=sample_thread.id,
        status=RunStatus.RUNNING,
        trigger=RunTrigger.MANUAL,
        trace_id="trace-123",
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    job = CommisJob(
        owner_id=test_user.id,
        oikos_run_id=run.id,
        task="Test task",
        status=status_value,
        commis_id="commis-123",
        trace_id="trace-123",
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return run, job


def _latest_run_event(db_session, run_id: int) -> RunEvent | None:
    return db_session.query(RunEvent).filter(RunEvent.run_id == run_id).order_by(RunEvent.id.desc()).first()


def test_commis_tool_event_requires_internal_token(
    monkeypatch,
    unauthenticated_client,
    db_session,
    test_user,
    sample_fiche,
    sample_thread,
):
    settings = get_settings()
    prev_auth_disabled = settings.auth_disabled
    prev_secret = settings.internal_api_secret
    settings.override(auth_disabled=False, internal_api_secret="secret-token-123456")
    monkeypatch.setattr(auth_deps, "get_settings", lambda: settings)

    run, job = _create_run_and_job(db_session, test_user, sample_fiche, sample_thread)

    payload = {
        "job_id": job.id,
        "event_type": "PreToolUse",
        "tool_name": "bash",
        "tool_input": {"cmd": "echo hi"},
        "tool_use_id": "tool-use-1",
    }

    resp = unauthenticated_client.post("/api/internal/commis/tool_event", json=payload)
    assert resp.status_code == status.HTTP_403_FORBIDDEN

    resp_ok = unauthenticated_client.post(
        "/api/internal/commis/tool_event",
        headers={"X-Internal-Token": "secret-token-123456"},
        json=payload,
    )
    assert resp_ok.status_code == status.HTTP_200_OK
    assert resp_ok.json().get("status") == "ok"

    event = _latest_run_event(db_session, run.id)
    assert event is not None
    assert event.event_type == "commis_tool_started"

    settings.override(auth_disabled=prev_auth_disabled, internal_api_secret=prev_secret)


def test_commis_tool_event_started_persists_payload(client, db_session, test_user, sample_fiche, sample_thread):
    run, job = _create_run_and_job(db_session, test_user, sample_fiche, sample_thread)

    tool_input = {"path": "README.md", "preview": True}
    payload = {
        "job_id": job.id,
        "event_type": "PreToolUse",
        "tool_name": "read_file",
        "tool_input": tool_input,
        "tool_use_id": "tool-use-2",
    }

    resp = client.post("/api/internal/commis/tool_event", json=payload)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json().get("status") == "ok"

    event = _latest_run_event(db_session, run.id)
    assert event is not None
    assert event.event_type == "commis_tool_started"
    assert event.payload["job_id"] == job.id
    assert event.payload["commis_id"] == job.commis_id
    assert event.payload["owner_id"] == job.owner_id
    assert event.payload["run_id"] == run.id
    assert event.payload["tool_name"] == "read_file"
    assert event.payload["tool_call_id"] == "tool-use-2"
    assert event.payload["tool_args"] == tool_input
    assert "tool_args_preview" in event.payload
    assert event.payload.get("tool_args_truncated") is None


def test_commis_tool_event_completed_truncates_large_payload(
    client, db_session, test_user, sample_fiche, sample_thread
):
    run, job = _create_run_and_job(db_session, test_user, sample_fiche, sample_thread)

    huge_response = "x" * 12050
    payload = {
        "job_id": job.id,
        "event_type": "PostToolUse",
        "tool_name": "shell",
        "tool_input": {"cmd": "ls -la"},
        "tool_use_id": "tool-use-3",
        "tool_response": huge_response,
    }

    resp = client.post("/api/internal/commis/tool_event", json=payload)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json().get("status") == "ok"

    event = _latest_run_event(db_session, run.id)
    assert event is not None
    assert event.event_type == "commis_tool_completed"
    payload = event.payload
    assert payload["job_id"] == job.id
    assert payload["tool_name"] == "shell"
    assert payload["tool_call_id"] == "tool-use-3"
    assert "result_preview" in payload
    assert payload.get("result_truncated") is True
    assert "[truncated]" in payload.get("result", "")

    # Ensure we didn't store the raw oversized payload unbounded
    serialized = json.dumps(payload.get("result"), ensure_ascii=True)
    assert len(serialized) <= 10150


def test_commis_tool_event_failed_emits_error(client, db_session, test_user, sample_fiche, sample_thread):
    run, job = _create_run_and_job(db_session, test_user, sample_fiche, sample_thread)

    payload = {
        "job_id": job.id,
        "event_type": "PostToolUseFailure",
        "tool_name": "shell",
        "tool_input": {"cmd": "false"},
        "tool_use_id": "tool-use-4",
        "error": "command failed: exit code 2",
    }

    resp = client.post("/api/internal/commis/tool_event", json=payload)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json().get("status") == "ok"

    event = _latest_run_event(db_session, run.id)
    assert event is not None
    assert event.event_type == "commis_tool_failed"
    assert event.payload["error"].startswith("command failed")


def test_commis_tool_event_ignored_when_not_running(client, db_session, test_user, sample_fiche, sample_thread):
    _, job = _create_run_and_job(db_session, test_user, sample_fiche, sample_thread, status_value="success")

    payload = {
        "job_id": job.id,
        "event_type": "PreToolUse",
        "tool_name": "read_file",
        "tool_input": {"path": "README.md"},
        "tool_use_id": "tool-use-5",
    }

    resp = client.post("/api/internal/commis/tool_event", json=payload)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json().get("status") == "ignored"

    events = db_session.query(RunEvent).filter(RunEvent.run_id == job.oikos_run_id).all()
    assert events == []
