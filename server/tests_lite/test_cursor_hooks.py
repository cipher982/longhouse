from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from zerg.services.cursor_hooks import install_cursor_hooks


def test_cursor_hook_install_preserves_user_hooks_and_is_idempotent(tmp_path: Path) -> None:
    cursor = tmp_path / ".cursor"
    cursor.mkdir()
    user = {"command": "./hooks/user.py", "timeout": 3}
    (cursor / "hooks.json").write_text(json.dumps({"version": 1, "hooks": {"beforeShellExecution": [user]}}))

    install_cursor_hooks(cursor)
    first = (cursor / "hooks.json").read_text()
    install_cursor_hooks(cursor)
    config = json.loads((cursor / "hooks.json").read_text())

    assert (cursor / "hooks.json").read_text() == first
    assert config["hooks"]["beforeShellExecution"][0] == user
    assert sum("longhouse-cursor-hook.py" in item["command"] for item in config["hooks"]["beforeShellExecution"]) == 1
    assert "afterAgentResponse" in config["hooks"]


def test_cursor_permission_timeout_returns_to_local_prompt(tmp_path: Path) -> None:
    cursor = tmp_path / ".cursor"
    cursor.mkdir()
    install_cursor_hooks(cursor)
    script = cursor / "hooks" / "longhouse-cursor-hook.py"
    env = dict(os.environ)
    env.update(
        {
            "LONGHOUSE_SESSION_ID": "managed-session",
            "LONGHOUSE_HOME": str(tmp_path / "longhouse"),
            "LONGHOUSE_PERMISSION_HOOK_ENABLED": "1",
            "LONGHOUSE_PERMISSION_HOOK_TIMEOUT_S": "0",
            "LONGHOUSE_HOOK_URL": "http://127.0.0.1:1",
            "LONGHOUSE_HOOK_TOKEN": "test-token",
        }
    )
    result = subprocess.run(
        [str(script), "beforeShellExecution"],
        input=json.dumps(
            {
                "conversation_id": "cursor-id",
                "generation_id": "generation-id",
                "command": "pwd",
            }
        ),
        text=True,
        capture_output=True,
        env=env,
        timeout=5,
        check=True,
    )

    assert json.loads(result.stdout) == {
        "permission": "ask",
        "user_message": "Longhouse unavailable; decide in Cursor",
    }
