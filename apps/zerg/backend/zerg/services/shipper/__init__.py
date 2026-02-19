"""Shipper module for syncing AI agent sessions to Longhouse.

The session shipping daemon is the Rust engine (longhouse-engine).
This module provides the supporting Python infrastructure:
- parser.py: JSONL parsing (used by commis_job_processor)
- hooks.py: Claude Code hook + MCP server installation
- token.py: device token / URL persistence
- service.py: launchd/systemd service management for longhouse-engine

Usage:
    # Service management
    from zerg.services.shipper import install_service, get_service_status
    install_service(url="https://api.longhouse.ai")
    status = get_service_status()  # "running", "stopped", "not-installed"
"""

from zerg.services.shipper.hooks import install_codex_mcp_server
from zerg.services.shipper.hooks import install_hooks
from zerg.services.shipper.hooks import install_mcp_server
from zerg.services.shipper.hooks import upsert_codex_mcp_toml
from zerg.services.shipper.parser import ParsedEvent
from zerg.services.shipper.parser import parse_session_file
from zerg.services.shipper.parser import parse_session_file_full
from zerg.services.shipper.service import get_service_info
from zerg.services.shipper.service import get_service_status
from zerg.services.shipper.service import install_service
from zerg.services.shipper.service import uninstall_service
from zerg.services.shipper.token import clear_token
from zerg.services.shipper.token import clear_zerg_url
from zerg.services.shipper.token import get_token_path
from zerg.services.shipper.token import get_zerg_url
from zerg.services.shipper.token import load_token
from zerg.services.shipper.token import save_token
from zerg.services.shipper.token import save_zerg_url

__all__ = [
    "clear_token",
    "clear_zerg_url",
    "get_service_info",
    "get_service_status",
    "get_token_path",
    "get_zerg_url",
    "install_codex_mcp_server",
    "install_hooks",
    "install_mcp_server",
    "install_service",
    "load_token",
    "ParsedEvent",
    "parse_session_file",
    "parse_session_file_full",
    "save_token",
    "save_zerg_url",
    "uninstall_service",
    "upsert_codex_mcp_toml",
]
