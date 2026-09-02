"""install_hooks must install the permission-gate hook alongside the lifecycle
hook on PreToolUse, dormant by default, and idempotently across re-runs."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from zerg.services.shipper.hooks import install_hooks

TRUE_BIN = shutil.which("true")
assert TRUE_BIN is not None


def _pre_tool_use_commands(claude_dir: Path) -> list[str]:
    settings = json.loads((claude_dir / "settings.json").read_text())
    cmds: list[str] = []
    for entry in settings["hooks"].get("PreToolUse", []):
        for hook in entry.get("hooks", []):
            cmds.append(hook.get("command", ""))
    return cmds


def test_install_writes_permission_gate_and_registers_pretooluse(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    claude_dir = tmp_path / ".claude"
    install_hooks("http://localhost:8080", token="zdt_x", claude_dir=str(claude_dir), engine_path=TRUE_BIN)

    cmds = _pre_tool_use_commands(claude_dir)
    assert any("claude-lifecycle-hook" in c for c in cmds), "native lifecycle hook missing"
    assert any("claude-permission-gate" in c for c in cmds), "native gate missing"
    assert not any("longhouse-hook.sh" in c for c in cmds), "legacy lifecycle hook still registered"
    assert not any("longhouse-permission-gate.py" in c for c in cmds), "legacy gate still registered"


def test_install_is_idempotent_keeps_both_pretooluse_hooks(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    claude_dir = tmp_path / ".claude"
    install_hooks("http://localhost:8080", token="zdt_x", claude_dir=str(claude_dir), engine_path=TRUE_BIN)
    install_hooks("http://localhost:8080", token="zdt_x", claude_dir=str(claude_dir), engine_path=TRUE_BIN)

    cmds = _pre_tool_use_commands(claude_dir)
    lifecycle = [c for c in cmds if "claude-lifecycle-hook" in c]
    gate = [c for c in cmds if "claude-permission-gate" in c]
    # Re-running must NOT clobber either, and must not duplicate them.
    assert len(lifecycle) == 1, f"expected one lifecycle hook, got {lifecycle}"
    assert len(gate) == 1, f"expected one gate hook, got {gate}"


def test_install_collapses_preexisting_duplicate_claude_hooks(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    claude_dir = tmp_path / ".claude"
    settings_path = claude_dir / "settings.json"
    claude_dir.mkdir()
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "/old/longhouse-hook.sh"}]},
                        {"hooks": [{"type": "command", "command": "/old/longhouse-hook.sh"}]},
                        {"hooks": [{"type": "command", "command": "/usr/local/bin/longhouse-helper.sh"}]},
                    ],
                    "PreToolUse": [
                        {"hooks": [{"type": "command", "command": "/old/longhouse-hook.sh"}]},
                        {"hooks": [{"type": "command", "command": "/old/longhouse-permission-gate.py"}]},
                        {"hooks": [{"type": "command", "command": "/old/longhouse-permission-gate.py"}]},
                    ],
                }
            }
        )
    )

    install_hooks("http://localhost:8080", claude_dir=str(claude_dir), engine_path=TRUE_BIN)

    settings = json.loads(settings_path.read_text())
    session_start = settings["hooks"]["SessionStart"]
    pre_tool = settings["hooks"]["PreToolUse"]
    assert sum("claude-lifecycle-hook" in entry["hooks"][0]["command"] for entry in session_start) == 1
    assert sum("claude-lifecycle-hook" in entry["hooks"][0]["command"] for entry in pre_tool) == 1
    assert sum("claude-permission-gate" in entry["hooks"][0]["command"] for entry in pre_tool) == 1
    assert any("/usr/local/bin/longhouse-helper.sh" in entry["hooks"][0]["command"] for entry in session_start)
