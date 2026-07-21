"""Tests for Claude hook installation and session binding behavior."""

import json
import os
import shutil
import subprocess
import uuid

import pytest

from zerg.services.shipper.hooks import HOOK_SCRIPT
from zerg.services.shipper.hooks import _make_hook_entries


def test_claude_hook_seeds_session_binding_on_stop():
    """Hook seeds session_binding via engine bind instead of shipping directly."""
    assert "__ENGINE_PATH__" in HOOK_SCRIPT
    assert 'bind --path "$TRANSCRIPT" --session-id "$MANAGED_SESSION_ID"' in HOOK_SCRIPT
    # No direct shipping — daemon handles it
    assert "ship --file" not in HOOK_SCRIPT
    assert "nohup" not in HOOK_SCRIPT


def test_claude_hook_writes_presence_to_outbox():
    assert 'LONGHOUSE_HOME="${LONGHOUSE_HOME:-__LONGHOUSE_HOME__}"' in HOOK_SCRIPT
    assert 'OUTBOX="$LONGHOUSE_HOME/agent/outbox"' in HOOK_SCRIPT
    assert 'OUTBOX="$LONGHOUSE_HOME/agent/runtime-events-outbox"' in HOOK_SCRIPT
    assert "transcript_path: $transcript" in HOOK_SCRIPT
    assert "find_provider_pid()" in HOOK_SCRIPT
    assert "control_path: $control_path" in HOOK_SCRIPT
    assert "provider_pid" in HOOK_SCRIPT
    assert 'write_presence_outbox "$PAYLOAD" >/dev/null 2>&1 || true' in HOOK_SCRIPT
    assert "write_runtime_event_outbox()" in HOOK_SCRIPT


def test_claude_hook_leaves_elicitation_questions_to_transcript_ingest():
    assert '-n "$MANAGED_SESSION_ID"' in HOOK_SCRIPT
    assert "idle_prompt|elicitation_dialog) STATE=\"needs_user\"" in HOOK_SCRIPT
    assert 'kind: "pause_request"' not in HOOK_SCRIPT
    assert 'tool_name: "AskUserQuestion"' not in HOOK_SCRIPT
    assert "permission_prompt)              STATE=\"blocked\"" in HOOK_SCRIPT


def test_claude_hook_does_not_fetch_dynamic_startup_context():
    # The coordination bootstrap is static and local. Startup continuity's
    # hosted project-summary fetch remains lab-only.
    assert "/api/agents/sessions/startup-context" not in HOOK_SCRIPT
    assert "LONGHOUSE_HOOK_URL" not in HOOK_SCRIPT
    assert "LONGHOUSE_HOOK_TOKEN" not in HOOK_SCRIPT
    assert "LONGHOUSE_COORDINATION_BOOTSTRAP" in HOOK_SCRIPT


def test_claude_hook_hot_path_stays_local_only():
    assert 'PRESENCE_MODE="${LONGHOUSE_HOOK_PRESENCE_MODE:-auto}"' not in HOOK_SCRIPT
    assert "/api/agents/presence" not in HOOK_SCRIPT
    assert "emit_presence()" not in HOOK_SCRIPT
    assert "LONGHOUSE_MANAGED_SESSION_ID" in HOOK_SCRIPT
    assert "write_presence_outbox()" in HOOK_SCRIPT


def test_claude_stop_hook_forces_sidechain_for_hindsight_workspace():
    assert 'FORCE_SIDECHAIN="${LONGHOUSE_IS_SIDECHAIN:-0}"' in HOOK_SCRIPT
    assert 'HINDSIGHT_ROOT="__HINDSIGHT_ROOT__"' in HOOK_SCRIPT
    assert 'case "$CWD" in' in HOOK_SCRIPT


def test_claude_stop_hook_entry_is_sync(tmp_path):
    """Stop hook is now sync (no shipping, just outbox write + binding seed)."""
    stop_entry, _lifecycle_entry = _make_hook_entries(tmp_path)
    hook = stop_entry["hooks"][0]
    assert hook["async"] is False
    assert hook["timeout"] == 5


def _run_hook(tmp_path, event, *, managed_session_id=None):
    if shutil.which("jq") is None:
        pytest.skip("jq is required to execute Claude hook fixture")
    script = tmp_path / "longhouse-hook.sh"
    script.write_text(
        HOOK_SCRIPT.replace("__LONGHOUSE_HOME__", str(tmp_path / "lh"))
        .replace("__HINDSIGHT_ROOT__", str(tmp_path / "hindsight"))
        .replace("__ENGINE_PATH__", "/bin/true")
    )
    script.chmod(0o755)
    env = os.environ.copy()
    env.pop("LONGHOUSE_MANAGED_SESSION_ID", None)
    env.pop("LONGHOUSE_IS_SIDECHAIN", None)
    if managed_session_id is not None:
        env["LONGHOUSE_MANAGED_SESSION_ID"] = managed_session_id
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
