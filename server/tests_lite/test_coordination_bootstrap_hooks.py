from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from zerg.services.shipper.hooks import CODEX_HOOK_SCRIPT
from zerg.services.shipper.hooks import COORDINATION_BOOTSTRAP
from zerg.services.shipper.hooks import HOOK_SCRIPT


@pytest.mark.parametrize("script", [HOOK_SCRIPT, CODEX_HOOK_SCRIPT])
@pytest.mark.parametrize(
    ("managed", "enabled", "expects_context"),
    [
        (True, True, True),
        (True, False, False),
        (False, True, False),
    ],
)
def test_static_coordination_bootstrap_is_flagged_and_managed_only(
    tmp_path,
    script,
    managed,
    enabled,
    expects_context,
):
    if shutil.which("jq") is None:
        pytest.skip("jq is required to execute provider hook fixtures")

    hook = tmp_path / "longhouse-hook.sh"
    hook.write_text(
        script.replace("__LONGHOUSE_HOME__", str(tmp_path / "longhouse"))
        .replace("__HINDSIGHT_ROOT__", str(tmp_path / "hindsight"))
        .replace("__ENGINE_PATH__", "/bin/true")
    )
    hook.chmod(0o755)
    env = os.environ.copy()
    env.pop("LONGHOUSE_MANAGED_SESSION_ID", None)
    env.pop("LONGHOUSE_COORDINATION_BOOTSTRAP", None)
    if managed:
        env["LONGHOUSE_MANAGED_SESSION_ID"] = "11111111-1111-1111-1111-111111111111"
    if enabled:
        env["LONGHOUSE_COORDINATION_BOOTSTRAP"] = "1"

    completed = subprocess.run(
        ["/bin/bash", str(hook)],
        input=json.dumps(
            {
                "hook_event_name": "SessionStart",
                "session_id": "provider-session-id",
                "cwd": str(tmp_path),
                "transcript_path": str(tmp_path / "transcript.jsonl"),
            }
        ),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    assert completed.returncode == 0, completed.stderr
    if not expects_context:
        assert completed.stdout == ""
        return

    output = json.loads(completed.stdout)
    assert output == {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": COORDINATION_BOOTSTRAP,
        }
    }


@pytest.mark.parametrize("script", [HOOK_SCRIPT, CODEX_HOOK_SCRIPT])
def test_static_coordination_bootstrap_has_no_hosted_dependency(script):
    assert "/api/agents/sessions/startup-context" not in script
    assert "LONGHOUSE_HOOK_URL" not in script
    assert "LONGHOUSE_HOOK_TOKEN" not in script
    assert "curl " not in script
