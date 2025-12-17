"""Account-level credential helpers for services (not tool context).

This module provides simple functions for retrieving account-level credentials
outside of the agent tool context. Use this for service-level operations like
syncing knowledge sources where there's no agent involved.

For agent tool credential resolution, use CredentialResolver from resolver.py.
"""

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from zerg.connectors.registry import ConnectorType
from zerg.utils.crypto import decrypt

logger = logging.getLogger(__name__)


def get_account_credential(
    db: Session,
    owner_id: int,
    connector_type: ConnectorType | str,
) -> dict[str, Any] | None:
    """Get decrypted account-level credential for a user.

    Mirrors the decrypt+JSON pattern from CredentialResolver._resolve_account_credential().
    Use this for service-level operations outside of agent tool context.

    Args:
        db: Database session
        owner_id: User ID
        connector_type: Connector type (enum or string)

    Returns:
        Decrypted credential dict (e.g., {"token": "..."} for GitHub), or None if not configured

    Example:
        creds = get_account_credential(db, user_id, ConnectorType.GITHUB)
        if creds:
            token = creds.get("token")
    """
    from zerg.models.models import AccountConnectorCredential

    type_str = connector_type.value if isinstance(connector_type, ConnectorType) else connector_type

    cred = (
        db.query(AccountConnectorCredential)
        .filter(
            AccountConnectorCredential.owner_id == owner_id,
            AccountConnectorCredential.connector_type == type_str,
        )
        .first()
    )

    if not cred:
        return None

    try:
        decrypted = decrypt(cred.encrypted_value)
        return json.loads(decrypted)
    except Exception as e:
        logger.warning(
            "Failed to decrypt account credential owner_id=%d connector=%s: %s",
            owner_id,
            type_str,
            str(e),
        )
        return None


def has_account_credential(
    db: Session,
    owner_id: int,
    connector_type: ConnectorType | str,
) -> bool:
    """Check if account-level credential exists (without decrypting).

    Use this for validation checks before attempting operations that require credentials.

    Args:
        db: Database session
        owner_id: User ID
        connector_type: Connector type (enum or string)

    Returns:
        True if a credential exists for this connector type
    """
    from zerg.models.models import AccountConnectorCredential

    type_str = connector_type.value if isinstance(connector_type, ConnectorType) else connector_type

    return (
        db.query(AccountConnectorCredential)
        .filter(
            AccountConnectorCredential.owner_id == owner_id,
            AccountConnectorCredential.connector_type == type_str,
        )
        .count()
        > 0
    )
