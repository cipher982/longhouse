from __future__ import annotations

import json
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
