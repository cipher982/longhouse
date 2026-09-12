"""Behavioral checks for Claude lifecycle hook events."""

import json
import os
import shutil
import subprocess
import uuid

import pytest

from zerg.services.shipper.hooks import HOOK_SCRIPT


def _run_hook(
    tmp_path,
    event,
    *,
    managed_session_id=None,
    managed_provider=None,
):
    if shutil.which("jq") is None:
        pytest.skip("jq is required to execute Claude hook fixture")
    script = tmp_path / "longhouse-hook.sh"
    script.write_text(
        HOOK_SCRIPT.replace("__LONGHOUSE_HOME__", str(tmp_path / "lh")).replace("__HINDSIGHT_ROOT__", str(tmp_path / "hindsight"))
    )
    script.chmod(0o755)
    env = os.environ.copy()
    env["LONGHOUSE_HOME"] = str(tmp_path / "lh")
    env.pop("LONGHOUSE_MANAGED_SESSION_ID", None)
    env.pop("LONGHOUSE_MANAGED_PROVIDER", None)
    env.pop("LONGHOUSE_IS_SIDECHAIN", None)
    if managed_session_id is not None:
        env["LONGHOUSE_MANAGED_SESSION_ID"] = managed_session_id
    if managed_provider is not None:
        env["LONGHOUSE_MANAGED_PROVIDER"] = managed_provider
    completed = subprocess.run(
        ["/bin/bash", str(script)],
        input=json.dumps(event),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    return tmp_path / "lh"


@pytest.mark.skipif(shutil.which("jq") is None, reason="hook script requires jq")
def test_claude_hook_rejects_another_providers_managed_session(tmp_path):
    native_session = str(uuid.uuid4())
    managed_session = str(uuid.uuid4())
    longhouse_home = _run_hook(
        tmp_path,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": native_session,
            "transcript_path": str(tmp_path / "transcript.jsonl"),
            "cwd": str(tmp_path),
        },
        managed_session_id=managed_session,
        managed_provider="codex",
    )

    payload = json.loads(next((longhouse_home / "agent" / "outbox").glob("prs.*.json")).read_text())
    assert payload["session_id"] == native_session
    assert payload["control_path"] == "unmanaged"
    assert "provider_session_id" not in payload


def _runtime_event_files(longhouse_home):
    outbox = longhouse_home / "agent" / "runtime-events-outbox"
    if not outbox.exists():
        return []
    return sorted(outbox.glob("rte.*.json"))


def test_claude_managed_elicitation_notification_does_not_write_pause_event(tmp_path):
    session_id = str(uuid.uuid4())
    provider_session_id = str(uuid.uuid4())

    longhouse_home = _run_hook(
        tmp_path,
        {
            "hook_event_name": "Notification",
            "session_id": provider_session_id,
            "transcript_path": str(tmp_path / "transcript.jsonl"),
            "cwd": str(tmp_path),
            "notification_type": "elicitation_dialog",
            "title": "Question needed",
            "message": "Which direction should I take?",
        },
        managed_session_id=session_id,
    )

    assert _runtime_event_files(longhouse_home) == []


def test_claude_unmanaged_elicitation_notification_does_not_write_pause_event(tmp_path):
    longhouse_home = _run_hook(
        tmp_path,
        {
            "hook_event_name": "Notification",
            "session_id": str(uuid.uuid4()),
            "transcript_path": str(tmp_path / "transcript.jsonl"),
            "cwd": str(tmp_path),
            "notification_type": "elicitation_dialog",
            "title": "Question needed",
            "message": "Which direction should I take?",
        },
    )

    assert _runtime_event_files(longhouse_home) == []


@pytest.mark.parametrize("notification_type", ["idle_prompt", "permission_prompt"])
def test_claude_non_elicitation_notifications_do_not_write_pause_event(tmp_path, notification_type):
    longhouse_home = _run_hook(
        tmp_path,
        {
            "hook_event_name": "Notification",
            "session_id": str(uuid.uuid4()),
            "transcript_path": str(tmp_path / "transcript.jsonl"),
            "cwd": str(tmp_path),
            "notification_type": notification_type,
            "title": "Needs attention",
            "message": "Provider notification",
        },
        managed_session_id=str(uuid.uuid4()),
    )

    assert _runtime_event_files(longhouse_home) == []
