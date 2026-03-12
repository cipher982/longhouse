"""Tests for workspace-scoped Longhouse MCP injection."""

import json
import tomllib

from zerg.services.workspace_manager import inject_codex_mcp_settings
from zerg.services.workspace_manager import inject_mcp_settings


def test_inject_mcp_settings_writes_workspace_local_claude_config(tmp_path):
    workspace_path = tmp_path / "workspace"

    settings_path = inject_mcp_settings(workspace_path, api_url="https://control.longhouse.ai")

    assert settings_path == workspace_path / ".claude" / "settings.json"

    data = json.loads(settings_path.read_text(encoding="utf-8"))
    assert data["mcpServers"]["longhouse"] == {
        "type": "stdio",
        "command": "longhouse",
        "args": ["mcp-server", "--url", "https://control.longhouse.ai"],
    }


def test_inject_codex_mcp_settings_writes_workspace_local_codex_config(tmp_path):
    workspace_path = tmp_path / "workspace"

    config_path = inject_codex_mcp_settings(workspace_path, api_url="https://control.longhouse.ai")

    assert config_path == workspace_path / ".codex" / "config.toml"

    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert data["mcp_servers"]["longhouse"] == {
        "command": "longhouse",
        "args": ["mcp-server", "--url", "https://control.longhouse.ai"],
    }
