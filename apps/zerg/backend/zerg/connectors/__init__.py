"""Connector credentials management for built-in tools.

This package provides:
- ConnectorType enum and CONNECTOR_REGISTRY for defining connector metadata
- CredentialResolver for resolving and decrypting credentials at runtime
- Connector testers for validating credentials before saving
"""

from zerg.connectors.registry import CONNECTOR_REGISTRY
from zerg.connectors.registry import ConnectorType

__all__ = [
    "ConnectorType",
    "CONNECTOR_REGISTRY",
]
