"""The shipped Antigravity hook script and the inbox message intents.

Runs the real hook script (not a copy of its logic) against queued inbox
messages: a send or steer message becomes `injectSteps`, an interrupt message
ends the turn at the next invocation boundary with `terminationBehavior`.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest

from zerg.provider_contract.antigravity_hook_inbox import _ANTIGRAVITY_HOOK_SCRIPT

SESSION_ID = "11111111-1111-4111-8111-111111111111"


def _run_hook(tmp_path: Path, event: str, intent: str | None, text: str = "queued text") -> tuple[dict, dict | None]:
    script = tmp_path / "hook.sh"
    script.write_text(_ANTIGRAVITY_HOOK_SCRIPT.replace("__LONGHOUSE_HOME__", str(tmp_path)), encoding="utf-8")
    script.chmod(0o755)
    inbox = tmp_path / "managed-local" / "antigravity" / "inbox" / SESSION_ID
    inbox.mkdir(parents=True)
    inbox.chmod(0o700)
    message = None
    if intent is not None:
        message = {
            "id": "msg1",
            "session_id": SESSION_ID,
            "text": text,
            "intent": intent,
            "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        }
        path = inbox / "msg-msg1.json"
        path.write_text(json.dumps(message), encoding="utf-8")
        path.chmod(0o600)
    env = os.environ.copy()
    env.update(
        LONGHOUSE_MANAGED_SESSION_ID=SESSION_ID,
        LONGHOUSE_MANAGED_PROVIDER="antigravity",
        LONGHOUSE_HOME=str(tmp_path),
    )
    completed = subprocess.run(
        [str(script), event],
        input=json.dumps({"conversationId": "22222222-2222-4222-8222-222222222222", "workspacePaths": [str(tmp_path)]}),
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
        check=True,
    )
    claimed = inbox / "claimed" / "claimed-msg-msg1.json"
    receipt = json.loads(claimed.read_text(encoding="utf-8")) if claimed.exists() else None
    return json.loads(completed.stdout.strip().splitlines()[-1]), receipt


@pytest.mark.parametrize("intent", ["send", "steer"])
def test_send_and_steer_inject_the_message_at_pre_invocation(tmp_path: Path, intent: str) -> None:
    response, receipt = _run_hook(tmp_path, "PreInvocation", intent)
    assert response == {"injectSteps": [{"userMessage": "queued text"}]}
    assert receipt is not None and receipt["intent"] == intent


@pytest.mark.parametrize("event", ["PreInvocation", "PostInvocation"])
def test_interrupt_terminates_the_turn_at_the_next_boundary(tmp_path: Path, event: str) -> None:
    response, receipt = _run_hook(tmp_path, event, "interrupt", text="Longhouse interrupt")
    assert response == {"injectSteps": [], "terminationBehavior": "terminate"}
    assert receipt is not None and receipt["claimed_at"]


def test_interrupt_text_is_never_injected_as_a_user_message(tmp_path: Path) -> None:
    response, _ = _run_hook(tmp_path, "PreInvocation", "interrupt", text="Longhouse interrupt")
    assert "Longhouse interrupt" not in json.dumps(response)


def test_no_message_leaves_the_default_response(tmp_path: Path) -> None:
    response, receipt = _run_hook(tmp_path, "PreInvocation", None)
    assert response == {"injectSteps": []}
    assert receipt is None
