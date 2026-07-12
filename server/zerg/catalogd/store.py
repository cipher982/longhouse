"""Synchronous catalog operations executed on catalogd's dedicated DB thread."""

from __future__ import annotations

import hmac
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from typing import Any

from sqlalchemy import Engine
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import update

from zerg.catalogd.schema import catalog_meta
from zerg.models.live_store import LiveDeviceToken

DEVICE_TOKEN_LIMIT_PER_OWNER = 1_000


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
        with _read_snapshot(self.engine) as connection:
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

    def list_devices(self, *, owner_id: int, include_revoked: bool) -> dict[str, Any]:
        """Return one owner's machine credentials from a single snapshot."""

        token_table = LiveDeviceToken.__table__
        with _read_snapshot(self.engine) as connection:
            commit_seq = _current_commit_seq(connection)
            statement = select(token_table).where(token_table.c.owner_id == owner_id)
            if not include_revoked:
                statement = statement.where(token_table.c.revoked_at.is_(None))
            rows = (
                connection.execute(
                    statement.order_by(token_table.c.created_at.desc(), token_table.c.id).limit(DEVICE_TOKEN_LIMIT_PER_OWNER + 1)
                )
                .mappings()
                .all()
            )
            if len(rows) > DEVICE_TOKEN_LIMIT_PER_OWNER:
                return {
                    "commit_seq": str(commit_seq),
                    "tokens": [],
                    "total": 0,
                    "limit_exceeded": True,
                }
            return {
                "commit_seq": str(commit_seq),
                "tokens": [
                    {
                        "id": str(row["id"]),
                        "device_id": str(row["device_id"]),
                        "created_at": _encode_datetime(row["created_at"]),
                        "last_used_at": _encode_datetime(row["last_used_at"]),
                        "revoked_at": _encode_datetime(row["revoked_at"]),
                        "is_valid": row["revoked_at"] is None,
                    }
                    for row in rows
                ],
                "total": len(rows),
                "limit_exceeded": False,
            }

    def create_device(
        self,
        *,
        owner_id: int,
        token_id: str,
        device_id: str,
        token_hash: str,
    ) -> dict[str, Any]:
        """Create one machine credential, idempotently keyed by token_id."""

        token_table = LiveDeviceToken.__table__
        now = datetime.now(UTC)
        with _write_transaction(self.engine) as connection:
            existing = connection.execute(select(token_table).where(token_table.c.id == token_id)).mappings().first()
            if existing is not None:
                exact_replay = (
                    existing["owner_id"] == owner_id
                    and existing["device_id"] == device_id
                    and hmac.compare_digest(str(existing["token_hash"]), token_hash)
                )
                return {
                    "created": False,
                    "exact_replay": exact_replay,
                    "limit_exceeded": False,
                    "token_id": str(existing["id"]),
                    "device_id": str(existing["device_id"]),
                    "created_at": _encode_datetime(existing["created_at"]),
                    "commit_seq": str(_current_commit_seq(connection)),
                }

            token_count = connection.execute(
                select(func.count()).select_from(token_table).where(token_table.c.owner_id == owner_id)
            ).scalar_one()
            if token_count >= DEVICE_TOKEN_LIMIT_PER_OWNER:
                return {
                    "created": False,
                    "exact_replay": False,
                    "limit_exceeded": True,
                    "commit_seq": str(_current_commit_seq(connection)),
                }

            connection.execute(
                token_table.insert().values(
                    id=token_id,
                    owner_id=owner_id,
                    device_id=device_id,
                    token_hash=token_hash,
                    created_at=now,
                )
            )
            commit_seq = connection.execute(
                update(catalog_meta)
                .where(catalog_meta.c.singleton == 1)
                .values(
                    commit_seq=catalog_meta.c.commit_seq + 1,
                    updated_at=now.isoformat(),
                )
                .returning(catalog_meta.c.commit_seq)
            ).scalar_one()
            return {
                "created": True,
                "exact_replay": False,
                "limit_exceeded": False,
                "token_id": token_id,
                "device_id": device_id,
                "created_at": now.isoformat(),
                "commit_seq": str(commit_seq),
            }

    def revoke_device(self, *, owner_id: int, token_id: str) -> dict[str, Any]:
        """Idempotently revoke one machine credential in a single commit.

        A replay after a lost response returns the durable revocation without
        allocating another commit sequence number. Its ``commit_seq`` is the
        current catalog sequence, not necessarily the original revoke's seq.
        """

        token_table = LiveDeviceToken.__table__
        now = datetime.now(UTC)
        with _write_transaction(self.engine) as connection:
            row = (
                connection.execute(
                    select(token_table.c.id, token_table.c.revoked_at).where(
                        token_table.c.id == token_id,
                        token_table.c.owner_id == owner_id,
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return {
                    "found": False,
                    "changed": False,
                    "commit_seq": str(_current_commit_seq(connection)),
                }

            revoked_at = _as_aware_utc(row["revoked_at"])
            if revoked_at is not None:
                return {
                    "found": True,
                    "changed": False,
                    "token_id": str(row["id"]),
                    "revoked_at": _encode_datetime(revoked_at),
                    "commit_seq": str(_current_commit_seq(connection)),
                }

            connection.execute(
                update(token_table).where(token_table.c.id == token_id, token_table.c.owner_id == owner_id).values(revoked_at=now)
            )
            commit_seq = connection.execute(
                update(catalog_meta)
                .where(catalog_meta.c.singleton == 1)
                .values(
                    commit_seq=catalog_meta.c.commit_seq + 1,
                    updated_at=now.isoformat(),
                )
                .returning(catalog_meta.c.commit_seq)
            ).scalar_one()
            return {
                "found": True,
                "changed": True,
                "token_id": str(row["id"]),
                "revoked_at": now.isoformat(),
                "commit_seq": str(commit_seq),
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


@contextmanager
def _read_snapshot(engine: Engine):
    """Open a real SQLite read transaction under pysqlite legacy mode."""

    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN")
        try:
            yield connection
        finally:
            connection.rollback()


@contextmanager
def _write_transaction(engine: Engine):
    """Acquire SQLite's write reservation before mutation read-checks."""

    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


def _as_aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _encode_datetime(value: datetime | None) -> str | None:
    normalized = _as_aware_utc(value)
    return normalized.isoformat() if normalized is not None else None


__all__ = ["CatalogStore", "DEVICE_TOKEN_LIMIT_PER_OWNER"]
