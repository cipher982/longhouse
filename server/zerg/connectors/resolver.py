"""Account-level credential resolver for built-in connector tools."""

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

CacheEntry = tuple[dict[str, Any] | None, Literal["account", "none"]]


class CredentialResolver:
    """Resolves account-level connector credentials for a user."""

    def __init__(
        self,
        db: Session,
        context_id: int | None = None,
        *,
        owner_id: int | None = None,
        prefetch: bool = True,
    ):
        _ = context_id
        self.owner_id = owner_id
        self.db = db
        self._cache: dict[str, CacheEntry] = {}
        self._prefetch_enabled = bool(prefetch)
        if self._prefetch_enabled:
            self._prefetch_all()

    def _prefetch_all(self) -> None:
        from zerg.models.models import AccountConnectorCredential

        if self.db is None or self.owner_id is None:
            self._prefetch_enabled = False
            return

        try:
            account_creds = self.db.query(AccountConnectorCredential).filter(AccountConnectorCredential.owner_id == self.owner_id).all()
        except Exception:
            logger.warning("Failed to prefetch account connector credentials", exc_info=True)
            self._prefetch_enabled = False
            return

        for cred in account_creds:
            try:
                decrypted = decrypt(cred.encrypted_value)
                self._cache[cred.connector_type] = (json.loads(decrypted), "account")
            except Exception:
                logger.warning(
                    "Failed to decrypt account credential owner_id=%d connector=%s during prefetch",
                    self.owner_id,
                    cred.connector_type,
                    exc_info=True,
                )

    def get(self, connector_type: ConnectorType | str) -> dict[str, Any] | None:
        """Get decrypted account credential for a connector type."""
        type_str = connector_type.value if isinstance(connector_type, ConnectorType) else connector_type

        if type_str in self._cache:
            cached_value, _source = self._cache[type_str]
            return cached_value

        if self._prefetch_enabled:
            self._cache[type_str] = (None, "none")
            return None

        value, source = self._resolve_account_credential(type_str)
        self._cache[type_str] = (value, source)
        return value

    def _resolve_account_credential(self, type_str: str) -> CacheEntry:
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
            return (json.loads(decrypt(cred.encrypted_value)), "account")
        except Exception as exc:
            logger.warning(
                "Failed to decrypt account credential owner_id=%d connector=%s: %s",
                self.owner_id,
                type_str,
                str(exc),
            )
            return (None, "none")

    def has(self, connector_type: ConnectorType | str) -> bool:
        """Return whether an account credential exists for this connector."""
        return self.get(connector_type) is not None
