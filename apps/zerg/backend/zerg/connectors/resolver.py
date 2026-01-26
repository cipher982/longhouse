"""Credential resolver for built-in connector tools.

The CredentialResolver is instantiated per-request with a fiche's ID and
provides a clean interface for tools to retrieve their required credentials.
It handles decryption and caches results for the lifetime of the request.

Resolution order (v2 architecture):
1. Fiche-level override (connector_credentials table)
2. Account-level credential (account_connector_credentials table)
3. None if neither exists
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING
from typing import Any
from typing import Literal

from zerg.connectors.registry import ConnectorType
from zerg.utils.crypto import decrypt

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# Cache entry with source tracking for observability
CacheEntry = tuple[dict[str, Any] | None, Literal["fiche", "account", "none"]]


class CredentialResolver:
    """Resolves and decrypts credentials for a fiche's connector tools.

    Instantiated per-request with fiche_id and owner_id. Resolves in order:
    1. Fiche-level override (connector_credentials)
    2. Account-level credential (account_connector_credentials)

    Caches decrypted values for the lifetime of the request to avoid
    repeated DB queries and decryption operations.

    Usage:
        resolver = CredentialResolver(fiche_id=42, owner_id=1, db=session)
        creds = resolver.get(ConnectorType.SLACK)
        if creds:
            webhook_url = creds.get("webhook_url")
    """

    def __init__(self, fiche_id: int, db: Session, *, owner_id: int | None = None, prefetch: bool = True):
        """Initialize resolver for a specific fiche.

        Args:
            fiche_id: The ID of the fiche whose credentials to resolve
            db: SQLAlchemy database session
            owner_id: The ID of the fiche's owner (for account-level fallback).
                      If None, account-level lookup is skipped.
        """
        self.fiche_id = fiche_id
        self.owner_id = owner_id
        self.db = db
        self._cache: dict[str, CacheEntry] = {}
        self._prefetch_enabled = bool(prefetch)

        # Concurrency safety: tools may run in threads (asyncio.to_thread) and inherit
        # this resolver via contextvars. SQLAlchemy Sessions are not thread-safe, so
        # in normal operation we prefetch all credentials once (in the caller thread)
        # and then serve lookups from the in-memory cache without touching `self.db`.
        if self._prefetch_enabled:
            self._prefetch_all()

    def _prefetch_all(self) -> None:
        """Populate the cache with all configured credentials (best-effort).

        This avoids hitting `self.db` during tool execution, which may run in
        background threads and/or concurrently.
        """
        from zerg.models.models import AccountConnectorCredential
        from zerg.models.models import ConnectorCredential

        if self.db is None:
            self._prefetch_enabled = False
            return

        # Fiche-level overrides first.
        try:
            fiche_creds = self.db.query(ConnectorCredential).filter(ConnectorCredential.fiche_id == self.fiche_id).all()
        except Exception:
            logger.warning("Failed to prefetch fiche connector credentials", exc_info=True)
            self._prefetch_enabled = False
            return

        for cred in fiche_creds:
            try:
                decrypted = decrypt(cred.encrypted_value)
                value = json.loads(decrypted)
                self._cache[cred.connector_type] = (value, "fiche")
            except Exception:
                logger.warning(
                    "Failed to decrypt fiche credential fiche_id=%d connector=%s during prefetch",
                    self.fiche_id,
                    cred.connector_type,
                    exc_info=True,
                )

        # Account-level fallbacks (only for types not overridden at fiche level).
        if self.owner_id is not None:
            try:
                account_creds = self.db.query(AccountConnectorCredential).filter(AccountConnectorCredential.owner_id == self.owner_id).all()
            except Exception:
                logger.warning("Failed to prefetch account connector credentials", exc_info=True)
                self._prefetch_enabled = False
                return

            for cred in account_creds:
                if cred.connector_type in self._cache:
                    continue
                try:
                    decrypted = decrypt(cred.encrypted_value)
                    value = json.loads(decrypted)
                    self._cache[cred.connector_type] = (value, "account")
                except Exception:
                    logger.warning(
                        "Failed to decrypt account credential owner_id=%d connector=%s during prefetch",
                        self.owner_id,
                        cred.connector_type,
                        exc_info=True,
                    )

    def get(self, connector_type: ConnectorType | str) -> dict[str, Any] | None:
        """Get decrypted credential for a connector type.

        Resolution order:
        1. Fiche-level override (connector_credentials table)
        2. Account-level credential (account_connector_credentials table)
        3. None if neither exists

        Args:
            connector_type: ConnectorType enum or string value

        Returns:
            dict with credential fields, or None if not configured.
            For single-field connectors (Slack), returns {"webhook_url": "..."}.
            For multi-field connectors (Jira), returns {"domain": "...", "email": "...", "api_token": "..."}.
        """
        type_str = connector_type.value if isinstance(connector_type, ConnectorType) else connector_type

        # Check cache first
        if type_str in self._cache:
            cached_value, _source = self._cache[type_str]
            return cached_value

        # If we've prefetched, never touch the DB again. Negative-cache the miss.
        if self._prefetch_enabled:
            self._cache[type_str] = (None, "none")
            return None

        # Try fiche-level override first
        value, source = self._resolve_fiche_credential(type_str)
        if value is not None:
            self._cache[type_str] = (value, source)
            logger.debug(
                "credential_resolver.resolve fiche_id=%d owner_id=%s connector_type=%s source=%s cache_hit=False",
                self.fiche_id,
                self.owner_id,
                type_str,
                source,
            )
            return value

        # Fallback to account-level credential if owner_id is available
        if self.owner_id is not None:
            value, source = self._resolve_account_credential(type_str)
            if value is not None:
                self._cache[type_str] = (value, source)
                logger.debug(
                    "credential_resolver.resolve fiche_id=%d owner_id=%s connector_type=%s source=%s cache_hit=False",
                    self.fiche_id,
                    self.owner_id,
                    type_str,
                    source,
                )
                return value

        # No credential found
        self._cache[type_str] = (None, "none")
        logger.debug(
            "credential_resolver.resolve fiche_id=%d owner_id=%s connector_type=%s source=none cache_hit=False",
            self.fiche_id,
            self.owner_id,
            type_str,
        )
        return None

    def _resolve_fiche_credential(self, type_str: str) -> CacheEntry:
        """Resolve credential from fiche-level overrides."""
        from zerg.models.models import ConnectorCredential

        cred = (
            self.db.query(ConnectorCredential)
            .filter(
                ConnectorCredential.fiche_id == self.fiche_id,
                ConnectorCredential.connector_type == type_str,
            )
            .first()
        )

        if not cred:
            return (None, "none")

        try:
            decrypted = decrypt(cred.encrypted_value)
            value = json.loads(decrypted)
            return (value, "fiche")
        except Exception as e:
            logger.warning(
                "Failed to decrypt fiche credential fiche_id=%d connector=%s: %s",
                self.fiche_id,
                type_str,
                str(e),
            )
            return (None, "none")

    def _resolve_account_credential(self, type_str: str) -> CacheEntry:
        """Resolve credential from account-level credentials."""
        from zerg.models.models import AccountConnectorCredential

        if self.owner_id is None:
            return (None, "none")

        cred = (
            self.db.query(AccountConnectorCredential)
            .filter(
                AccountConnectorCredential.owner_id == self.owner_id,
                AccountConnectorCredential.connector_type == type_str,
            )
            .first()
        )

        if not cred:
            return (None, "none")

        try:
            decrypted = decrypt(cred.encrypted_value)
            value = json.loads(decrypted)
            return (value, "account")
        except Exception as e:
            logger.warning(
                "Failed to decrypt account credential owner_id=%d connector=%s: %s",
                self.owner_id,
                type_str,
                str(e),
            )
            return (None, "none")

    def has(self, connector_type: ConnectorType | str) -> bool:
        """Check if a credential is configured (without decrypting).

        Checks both fiche-level and account-level credentials.

        Args:
            connector_type: ConnectorType enum or string value

        Returns:
            True if a credential exists for this connector type
        """
        from zerg.models.models import AccountConnectorCredential
        from zerg.models.models import ConnectorCredential

        type_str = connector_type.value if isinstance(connector_type, ConnectorType) else connector_type

        # If we've already fetched it, check cache
        if type_str in self._cache:
            cached_value, _source = self._cache[type_str]
            return cached_value is not None

        # If we've prefetched, never touch the DB again. Negative-cache the miss.
        if self._prefetch_enabled:
            self._cache[type_str] = (None, "none")
            return False

        # Check fiche-level first (count query avoids decryption)
        fiche_count = (
            self.db.query(ConnectorCredential)
            .filter(
                ConnectorCredential.fiche_id == self.fiche_id,
                ConnectorCredential.connector_type == type_str,
            )
            .count()
        )
        if fiche_count > 0:
            return True

        # Check account-level if owner_id is available
        if self.owner_id is not None:
            account_count = (
                self.db.query(AccountConnectorCredential)
                .filter(
                    AccountConnectorCredential.owner_id == self.owner_id,
                    AccountConnectorCredential.connector_type == type_str,
                )
                .count()
            )
            return account_count > 0

        return False

    def get_all_configured(self) -> list[str]:
        """Get list of all connector types that are configured for this fiche.

        Returns both fiche-level overrides and account-level credentials.

        Returns:
            List of unique connector type strings that have credentials configured
        """
        if self._prefetch_enabled:
            return [connector_type for connector_type, (value, _source) in self._cache.items() if value is not None]

        from zerg.models.models import AccountConnectorCredential
        from zerg.models.models import ConnectorCredential

        # Get fiche-level credentials
        fiche_creds = self.db.query(ConnectorCredential.connector_type).filter(ConnectorCredential.fiche_id == self.fiche_id).all()
        types = {c.connector_type for c in fiche_creds}

        # Get account-level credentials if owner_id is available
        if self.owner_id is not None:
            account_creds = (
                self.db.query(AccountConnectorCredential.connector_type).filter(AccountConnectorCredential.owner_id == self.owner_id).all()
            )
            types.update(c.connector_type for c in account_creds)

        return list(types)

    def get_resolution_source(self, connector_type: ConnectorType | str) -> Literal["fiche", "account", "none"]:
        """Get the source of a credential (for debugging/observability).

        Args:
            connector_type: ConnectorType enum or string value

        Returns:
            'fiche' if resolved from fiche override, 'account' if from account-level,
            'none' if not configured.
        """
        type_str = connector_type.value if isinstance(connector_type, ConnectorType) else connector_type

        # Ensure credential is resolved (populates cache)
        self.get(type_str)

        if type_str in self._cache:
            _value, source = self._cache[type_str]
            return source
        return "none"

    def clear_cache(self) -> None:
        """Clear the credential cache.

        Useful if credentials may have been updated during the request.
        """
        self._cache.clear()
        if self._prefetch_enabled:
            self._prefetch_all()


def create_resolver(fiche_id: int, db: Session, *, owner_id: int | None = None) -> CredentialResolver:
    """Factory function to create a CredentialResolver.

    Args:
        fiche_id: The ID of the fiche whose credentials to resolve
        db: SQLAlchemy database session
        owner_id: Optional owner ID for account-level credential fallback

    Returns:
        CredentialResolver instance
    """
    return CredentialResolver(fiche_id=fiche_id, db=db, owner_id=owner_id)
