"""Synchronous catalog operations executed on catalogd's dedicated DB thread."""

from __future__ import annotations

import hmac
from datetime import UTC
from datetime import datetime
from typing import Any

from sqlalchemy import Engine
from sqlalchemy import select

from zerg.catalogd.schema import catalog_meta
from zerg.models.live_store import LiveDeviceToken


class CatalogStore:
    """Small product operations over the bounded catalog.

    Methods are deliberately synchronous: the daemon invokes them only on its
    single dedicated executor, keeping SQLite work off the asyncio socket loop.
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def authenticate_device(self, *, token_hash: str) -> dict[str, Any]:
        """Validate one machine credential without turning auth into a write."""

        token_table = LiveDeviceToken.__table__
        with self.engine.connect() as connection:
            row = connection.execute(select(token_table).where(token_table.c.token_hash == token_hash)).mappings().first()
            if row is None:
                hmac.compare_digest(token_hash, "0" * 64)
                return {"valid": False, "commit_seq": str(_current_commit_seq(connection))}
            if not hmac.compare_digest(token_hash, str(row["token_hash"])) or row["revoked_at"] is not None:
                return {"valid": False, "commit_seq": str(_current_commit_seq(connection))}

            commit_seq = _current_commit_seq(connection)
            return {
                "valid": True,
                "commit_seq": str(commit_seq),
                "token": {
                    "id": str(row["id"]),
                    "owner_id": row["owner_id"],
                    "device_id": row["device_id"],
                    "created_at": _encode_datetime(row["created_at"]),
                    "last_used_at": _encode_datetime(row["last_used_at"]),
                    "revoked_at": None,
                },
            }

    def checkpoint_passive(self) -> dict[str, int]:
        """Run a non-blocking WAL checkpoint owned by catalogd."""

        with self.engine.connect() as connection:
            busy, log_frames, checkpointed_frames = connection.exec_driver_sql("PRAGMA wal_checkpoint(PASSIVE)").one()
        return {
            "busy": int(busy),
            "log_frames": int(log_frames),
            "checkpointed_frames": int(checkpointed_frames),
        }


def _current_commit_seq(connection) -> int:
    value = connection.execute(select(catalog_meta.c.commit_seq).where(catalog_meta.c.singleton == 1)).scalar_one()
    if type(value) is not int or value < 0:
        raise RuntimeError("catalog commit_seq is invalid")
    return value


def _as_aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _encode_datetime(value: datetime | None) -> str | None:
    normalized = _as_aware_utc(value)
    return normalized.isoformat() if normalized is not None else None


__all__ = ["CatalogStore"]
