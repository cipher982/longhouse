"""Workspace-local MCP configuration helpers."""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def inject_mcp_settings(workspace_path: Path, api_url: str | None = None) -> Path | None:
    """Inject Longhouse MCP server config into workspace .claude/settings.json."""
    workspace_path = Path(workspace_path)
    claude_dir = workspace_path / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path = claude_dir / "settings.json"

    settings: dict = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    mcp_args = ["mcp-server"]
    if api_url:
        mcp_args.extend(["--url", api_url])

    mcp_config = {
        "type": "stdio",
        "command": "longhouse",
        "args": mcp_args,
    }

    if "mcpServers" not in settings:
        settings["mcpServers"] = {}
    settings["mcpServers"]["longhouse"] = mcp_config

    try:
        settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        logger.info("Injected MCP settings into %s", settings_path)
        return settings_path
    except OSError as e:
        logger.warning("Failed to write MCP settings to %s: %s", settings_path, e)
        return None


def inject_codex_mcp_settings(workspace_path: Path, api_url: str | None = None) -> Path | None:
    """Inject Longhouse MCP server config into workspace .codex/config.toml."""
    from zerg.services.shipper.hooks import upsert_codex_mcp_toml

    workspace_path = Path(workspace_path)
    config_path = workspace_path / ".codex" / "config.toml"

    try:
        upsert_codex_mcp_toml(config_path, api_url=api_url, strict=False)
        logger.info("Injected Codex MCP settings into %s", config_path)
        return config_path
    except OSError as e:
        logger.warning("Failed to write Codex MCP settings to %s: %s", config_path, e)
        return None
