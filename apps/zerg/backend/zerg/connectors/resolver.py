"""Credential resolver for built-in connector tools.

The CredentialResolver is instantiated per-request with an agent's ID and
provides a clean interface for tools to retrieve their required credentials.
It handles decryption and caches results for the lifetime of the request.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from zerg.connectors.registry import ConnectorType
from zerg.utils.crypto import decrypt

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class CredentialResolver:
    """Resolves and decrypts credentials for an agent's connector tools.

    Instantiated per-request with the agent's ID. Caches decrypted values
    for the lifetime of the request to avoid repeated DB queries and
    decryption operations.

    Usage:
        resolver = CredentialResolver(agent_id=42, db=session)
        creds = resolver.get(ConnectorType.SLACK)
        if creds:
            webhook_url = creds.get("webhook_url")
    """

    def __init__(self, agent_id: int, db: Session):
        """Initialize resolver for a specific agent.

        Args:
            agent_id: The ID of the agent whose credentials to resolve
            db: SQLAlchemy database session
        """
        self.agent_id = agent_id
        self.db = db
        self._cache: dict[str, dict[str, Any] | None] = {}

    def get(self, connector_type: ConnectorType | str) -> dict[str, Any] | None:
        """Get decrypted credential for a connector type.

        Args:
            connector_type: ConnectorType enum or string value

        Returns:
            dict with credential fields, or None if not configured.
            For single-field connectors (Slack), returns {"webhook_url": "..."}.
            For multi-field connectors (Jira), returns {"domain": "...", "email": "...", "api_token": "..."}.
        """
        # Import here to avoid circular dependency
        from zerg.models.models import ConnectorCredential

        type_str = connector_type.value if isinstance(connector_type, ConnectorType) else connector_type

        # Check cache first
        if type_str in self._cache:
            return self._cache[type_str]

        # Query database
        cred = (
            self.db.query(ConnectorCredential)
            .filter(
                ConnectorCredential.agent_id == self.agent_id,
                ConnectorCredential.connector_type == type_str,
            )
            .first()
        )

        if not cred:
            self._cache[type_str] = None
            return None

        # Decrypt and parse
        try:
            decrypted = decrypt(cred.encrypted_value)
            value = json.loads(decrypted)
            self._cache[type_str] = value
            return value
        except Exception as e:
            logger.warning(
                "Failed to decrypt credential for agent=%d connector=%s: %s",
                self.agent_id,
                type_str,
                str(e),
            )
            self._cache[type_str] = None
            return None

    def has(self, connector_type: ConnectorType | str) -> bool:
        """Check if a credential is configured (without decrypting).

        Args:
            connector_type: ConnectorType enum or string value

        Returns:
            True if a credential exists for this connector type
        """
        # Import here to avoid circular dependency
        from zerg.models.models import ConnectorCredential

        type_str = connector_type.value if isinstance(connector_type, ConnectorType) else connector_type

        # If we've already fetched it, check cache
        if type_str in self._cache:
            return self._cache[type_str] is not None

        # Otherwise do a count query (avoids decryption)
        count = (
            self.db.query(ConnectorCredential)
            .filter(
                ConnectorCredential.agent_id == self.agent_id,
                ConnectorCredential.connector_type == type_str,
            )
            .count()
        )
        return count > 0

    def get_all_configured(self) -> list[str]:
        """Get list of all connector types that are configured for this agent.

        Returns:
            List of connector type strings that have credentials configured
        """
        # Import here to avoid circular dependency
        from zerg.models.models import ConnectorCredential

        creds = (
            self.db.query(ConnectorCredential.connector_type)
            .filter(ConnectorCredential.agent_id == self.agent_id)
            .all()
        )
        return [c.connector_type for c in creds]

    def clear_cache(self) -> None:
        """Clear the credential cache.

        Useful if credentials may have been updated during the request.
        """
        self._cache.clear()


def create_resolver(agent_id: int, db: Session) -> CredentialResolver:
    """Factory function to create a CredentialResolver.

    Args:
        agent_id: The ID of the agent whose credentials to resolve
        db: SQLAlchemy database session

    Returns:
        CredentialResolver instance
    """
    return CredentialResolver(agent_id=agent_id, db=db)
