"""Shipper module for syncing Claude Code sessions to Zerg.

This module provides tools to:
1. Parse Claude Code JSONL session files
2. Track shipped offsets for incremental sync
3. Ship sessions to Zerg's /api/agents/ingest endpoint
4. Watch for real-time file changes (sub-second sync)
5. Spool payloads locally when API unreachable

Usage:
    from zerg.services.shipper import SessionShipper, SessionWatcher

    # One-shot ship
    shipper = SessionShipper()
    result = await shipper.scan_and_ship()

    # Real-time watching
    watcher = SessionWatcher(shipper)
    await watcher.start()
"""

from zerg.services.shipper.parser import ParsedEvent
from zerg.services.shipper.parser import parse_session_file
from zerg.services.shipper.shipper import SessionShipper
from zerg.services.shipper.shipper import ShipperConfig
from zerg.services.shipper.shipper import ShipResult
from zerg.services.shipper.spool import OfflineSpool
from zerg.services.shipper.spool import SpooledPayload
from zerg.services.shipper.state import ShippedSession
from zerg.services.shipper.state import ShipperState
from zerg.services.shipper.watcher import SessionWatcher

__all__ = [
    "OfflineSpool",
    "ParsedEvent",
    "parse_session_file",
    "SessionShipper",
    "SessionWatcher",
    "ShipperConfig",
    "ShipResult",
    "ShippedSession",
    "ShipperState",
    "SpooledPayload",
]
