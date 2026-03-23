"""Resolve declared secrets for job execution: DB first, env var fallback.

Self-hosted users who only use env vars don't need to change anything.
Hosted users can store secrets via the API and they take precedence.
"""

from __future__ import annotations

import logging
import os

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def resolve_secrets(owner_id: int, declared_keys: list[str], db: Session) -> dict[str, str]:
    """Resolve declared secrets: DB first, env var fallback.

    Args:
        owner_id: User who owns the job (for DB lookup).
        declared_keys: Secret keys the job declared in ``JobConfig.secrets``.
        db: SQLAlchemy session.

    Returns:
        Dict mapping key -> plaintext value for all resolved secrets.
        Keys not found in either DB or env are omitted (not an error here;
        the job can decide via ``require_secret`` vs ``get_secret``).
    """
    if not declared_keys:
        return {}

    from zerg.models.models import JobSecret
    from zerg.utils.crypto import decrypt

    # Fetch from DB
    rows = (
        db.query(JobSecret)
        .filter(
            JobSecret.owner_id == owner_id,
            JobSecret.key.in_(declared_keys),
        )
        .all()
    )
    secrets: dict[str, str] = {}
    for row in rows:
        try:
            secrets[row.key] = decrypt(row.encrypted_value)
        except Exception:
            logger.warning("Failed to decrypt secret %s for owner %d", row.key, owner_id)

    # Env var fallback for keys not in DB
    for key in declared_keys:
        if key not in secrets:
            env_val = os.environ.get(key)
            if env_val:
                secrets[key] = env_val

    return secrets


__all__ = ["resolve_secrets"]
