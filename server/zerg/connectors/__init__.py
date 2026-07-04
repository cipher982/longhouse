"""Connector credentials management for built-in tools.

This package provides:
- ConnectorType enum and CONNECTOR_REGISTRY for defining connector metadata
"""

from zerg.connectors.registry import CONNECTOR_REGISTRY
from zerg.connectors.registry import ConnectorType

__all__ = [
    "ConnectorType",
    "CONNECTOR_REGISTRY",
]
