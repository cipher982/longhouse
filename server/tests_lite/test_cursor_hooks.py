from __future__ import annotations

import json
from pathlib import Path

from zerg.services.cursor_hooks import install_cursor_hooks


def test_cursor_hook_install_preserves_user_hooks_and_uses_paired_engine(tmp_path: Path) -> None:
    cursor = tmp_path / ".cursor"
    cursor.mkdir()
    user = {"command": "./hooks/user.py", "timeout": 3}
    engine = tmp_path / "bin" / "longhouse-engine"
    engine.parent.mkdir()
    engine.write_text("native engine")
    (cursor / "hooks.json").write_text(
        json.dumps({"version": 1, "hooks": {"beforeShellExecution": [user]}})
    )

    install_cursor_hooks(cursor, engine_path=engine)
    first = (cursor / "hooks.json").read_text()
    install_cursor_hooks(cursor, engine_path=engine)
    config = json.loads((cursor / "hooks.json").read_text())

    assert (cursor / "hooks.json").read_text() == first
    shell_hooks = config["hooks"]["beforeShellExecution"]
    assert shell_hooks[0] == user
    lifecycle = next(item for item in shell_hooks if "cursor-lifecycle-hook" in item["command"])
    permission = next(item for item in shell_hooks if "cursor-permission-hook" in item["command"])
    assert lifecycle == {
        "command": f"{engine} cursor-lifecycle-hook beforeShellExecution",
        "timeout": 5,
        "failClosed": False,
    }
    assert permission == {
        "command": f"{engine} cursor-permission-hook beforeShellExecution",
        "timeout": 125,
        "failClosed": True,
    }
    assert not any("longhouse-cursor-hook.py" in item["command"] for item in shell_hooks)
    assert "afterAgentResponse" in config["hooks"]
