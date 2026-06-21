"""install_hooks must install the permission-gate hook alongside the lifecycle
hook on PreToolUse, dormant by default, and idempotently across re-runs."""

from __future__ import annotations

import json
from pathlib import Path

from zerg.services.shipper.hooks import install_hooks


def _pre_tool_use_commands(claude_dir: Path) -> list[str]:
    settings = json.loads((claude_dir / "settings.json").read_text())
    cmds: list[str] = []
    for entry in settings["hooks"].get("PreToolUse", []):
        for hook in entry.get("hooks", []):
            cmds.append(hook.get("command", ""))
    return cmds


def test_install_writes_permission_gate_and_registers_pretooluse(tmp_path):
    claude_dir = tmp_path / ".claude"
    install_hooks("http://localhost:8080", token="zdt_x", claude_dir=str(claude_dir), engine_path="/bin/true")

    gate = claude_dir / "hooks" / "longhouse-permission-gate.py"
    assert gate.is_file(), "permission gate script must be installed"
    # Dormant by default: the script defaults LONGHOUSE_PERMISSION_HOOK_ENABLED off.
    assert 'get("LONGHOUSE_PERMISSION_HOOK_ENABLED", "0")' in gate.read_text()

    cmds = _pre_tool_use_commands(claude_dir)
    assert any("longhouse-hook.sh" in c for c in cmds), "lifecycle hook still on PreToolUse"
    assert any("longhouse-permission-gate.py" in c for c in cmds), "gate registered on PreToolUse"


def test_install_is_idempotent_keeps_both_pretooluse_hooks(tmp_path):
    claude_dir = tmp_path / ".claude"
    install_hooks("http://localhost:8080", token="zdt_x", claude_dir=str(claude_dir), engine_path="/bin/true")
    install_hooks("http://localhost:8080", token="zdt_x", claude_dir=str(claude_dir), engine_path="/bin/true")

    cmds = _pre_tool_use_commands(claude_dir)
    lifecycle = [c for c in cmds if "longhouse-hook.sh" in c]
    gate = [c for c in cmds if "longhouse-permission-gate.py" in c]
    # Re-running must NOT clobber either, and must not duplicate them.
    assert len(lifecycle) == 1, f"expected one lifecycle hook, got {lifecycle}"
    assert len(gate) == 1, f"expected one gate hook, got {gate}"
