"""Shipper module for syncing Claude Code sessions to Zerg.

This module provides tools to:
1. Parse Claude Code JSONL session files
2. Track shipped offsets for incremental sync
3. Ship sessions to Zerg's /api/agents/ingest endpoint
4. Watch for real-time file changes (sub-second sync)
5. Spool payloads locally when API unreachable
6. Install/manage shipper as a system service

Usage:
    from zerg.services.shipper import SessionShipper, SessionWatcher

    # One-shot ship
    shipper = SessionShipper()
    result = await shipper.scan_and_ship()

    # Real-time watching
    watcher = SessionWatcher(shipper)
    await watcher.start()

    # Service management
    from zerg.services.shipper import install_service, get_service_status
    install_service(url="https://api.longhouse.ai", token="xxx")
    status = get_service_status()  # "running", "stopped", "not-installed"
"""

from zerg.services.shipper.parser import ParsedEvent
from zerg.services.shipper.parser import parse_session_file
from zerg.services.shipper.service import get_service_info
from zerg.services.shipper.service import get_service_status
from zerg.services.shipper.service import install_service
from zerg.services.shipper.service import uninstall_service
from zerg.services.shipper.shipper import SessionShipper
from zerg.services.shipper.shipper import ShipperConfig
from zerg.services.shipper.shipper import ShipResult
from zerg.services.shipper.spool import OfflineSpool
from zerg.services.shipper.spool import SpooledPayload
from zerg.services.shipper.state import ShippedSession
from zerg.services.shipper.state import ShipperState
from zerg.services.shipper.token import clear_token
from zerg.services.shipper.token import clear_zerg_url
from zerg.services.shipper.token import get_token_path
from zerg.services.shipper.token import get_zerg_url
from zerg.services.shipper.token import load_token
from zerg.services.shipper.token import save_token
from zerg.services.shipper.token import save_zerg_url
from zerg.services.shipper.watcher import SessionWatcher

__all__ = [
    "clear_token",
    "clear_zerg_url",
    "get_service_info",
    "get_service_status",
    "get_token_path",
    "get_zerg_url",
    "install_service",
    "load_token",
    "OfflineSpool",
    "ParsedEvent",
    "parse_session_file",
    "save_token",
    "save_zerg_url",
    "SessionShipper",
    "SessionWatcher",
    "ShipperConfig",
    "ShipResult",
    "ShippedSession",
    "ShipperState",
    "SpooledPayload",
    "uninstall_service",
]
