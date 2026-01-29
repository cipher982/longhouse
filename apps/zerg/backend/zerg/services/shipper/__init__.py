"""Shipper module for syncing Claude Code sessions to Zerg.

This module provides tools to:
1. Parse Claude Code JSONL session files
2. Track shipped offsets for incremental sync
3. Ship sessions to Zerg's /api/agents/ingest endpoint

Usage:
    from zerg.services.shipper import SessionShipper

    shipper = SessionShipper()
    result = await shipper.scan_and_ship()
"""

from zerg.services.shipper.parser import ParsedEvent
from zerg.services.shipper.parser import parse_session_file
from zerg.services.shipper.shipper import SessionShipper
from zerg.services.shipper.shipper import ShipperConfig
from zerg.services.shipper.shipper import ShipResult
from zerg.services.shipper.state import ShippedSession
from zerg.services.shipper.state import ShipperState

__all__ = [
    "ParsedEvent",
    "parse_session_file",
    "SessionShipper",
    "ShipperConfig",
    "ShipResult",
    "ShippedSession",
    "ShipperState",
]
