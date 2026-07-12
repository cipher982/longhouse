"""Synchronous catalog operations executed on catalogd's dedicated DB thread."""

from __future__ import annotations

import hmac
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from typing import Any

from sqlalchemy import Engine
from sqlalchemy import func
from sqlalchemy import insert
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import update

from zerg.catalogd.schema import catalog_meta
from zerg.models.live_store import LiveDeviceToken
from zerg.models.live_store import LiveRefreshSession
from zerg.models.live_store import LiveUser

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

    def get_user(self, *, user_id: int, touch_last_login: bool) -> dict[str, Any]:
        """Resolve one user, optionally recording their first successful login."""

        user_table = LiveUser.__table__
        now = datetime.now(UTC)
        with _write_transaction(self.engine) if touch_last_login else _read_snapshot(self.engine) as connection:
            row = connection.execute(select(user_table).where(user_table.c.id == user_id)).mappings().first()
            if row is None:
                return {"found": False, "changed": False, "commit_seq": str(_current_commit_seq(connection))}
            changed = touch_last_login and row["last_login"] is None
            if changed:
                connection.execute(update(user_table).where(user_table.c.id == user_id).values(last_login=now, updated_at=now))
                commit_seq = _advance_commit_seq(connection, now)
                row = connection.execute(select(user_table).where(user_table.c.id == user_id)).mappings().one()
            else:
                commit_seq = _current_commit_seq(connection)
            return {
                "found": True,
                "changed": changed,
                "user": _user_dto(row),
                "commit_seq": str(commit_seq),
            }

    def resolve_device(
        self,
        *,
        token_hash: str,
        touch_last_used: bool,
        touch_interval_seconds: int,
    ) -> dict[str, Any]:
        """Resolve an active machine credential and its active owner atomically."""

        token_table = LiveDeviceToken.__table__
        user_table = LiveUser.__table__
        now = datetime.now(UTC)
        context = _write_transaction(self.engine) if touch_last_used else _read_snapshot(self.engine)
        with context as connection:
            row = (
                connection.execute(
                    select(
                        user_table,
                        token_table.c.id.label("device_token_id"),
                        token_table.c.owner_id.label("device_owner_id"),
                        token_table.c.device_id.label("device_id"),
                        token_table.c.token_hash.label("device_token_hash"),
                        token_table.c.created_at.label("device_created_at"),
                        token_table.c.last_used_at.label("device_last_used_at"),
                    )
                    .select_from(token_table.join(user_table, token_table.c.owner_id == user_table.c.id))
                    .where(
                        token_table.c.token_hash == token_hash,
                        token_table.c.revoked_at.is_(None),
                        user_table.c.is_active.is_(True),
                    )
                )
                .mappings()
                .first()
            )
            if row is None or not hmac.compare_digest(token_hash, str(row["device_token_hash"])):
                hmac.compare_digest(token_hash, "0" * 64)
                return {"valid": False, "changed": False, "commit_seq": str(_current_commit_seq(connection))}

            last_used_at = _as_aware_utc(row["device_last_used_at"])
            changed = bool(touch_last_used and (last_used_at is None or (now - last_used_at).total_seconds() >= touch_interval_seconds))
            if changed:
                connection.execute(update(token_table).where(token_table.c.id == row["device_token_id"]).values(last_used_at=now))
                commit_seq = _advance_commit_seq(connection, now)
                last_used_at = now
            else:
                commit_seq = _current_commit_seq(connection)
            return {
                "valid": True,
                "changed": changed,
                "token": {
                    "id": str(row["device_token_id"]),
                    "owner_id": row["device_owner_id"],
                    "device_id": row["device_id"],
                    "created_at": _encode_datetime(row["device_created_at"]),
                    "last_used_at": _encode_datetime(last_used_at),
                    "revoked_at": None,
                },
                "user": _user_dto(row),
                "commit_seq": str(commit_seq),
            }

    def resolve_cp_user(
        self,
        *,
        cp_user_id: int,
        email: str,
        email_verified: bool,
        display_name: str | None,
        avatar_url: str | None,
    ) -> dict[str, Any]:
        """Resolve/link one control-plane identity using the established conflict rules."""

        user_table = LiveUser.__table__
        now = datetime.now(UTC)
        with _write_transaction(self.engine) as connection:
            user = connection.execute(select(user_table).where(user_table.c.cp_user_id == cp_user_id)).mappings().first()
            changed = False
            if user is None:
                existing = connection.execute(select(user_table).where(user_table.c.email == email)).mappings().first()
                if existing is not None:
                    if not email_verified:
                        return {
                            "conflict": "email_unverified_link",
                            "commit_seq": str(_current_commit_seq(connection)),
                        }
                    if existing["cp_user_id"] not in (None, cp_user_id):
                        return {
                            "conflict": "account_link_conflict",
                            "commit_seq": str(_current_commit_seq(connection)),
                        }
                    user = existing
                else:
                    user_id = connection.execute(
                        insert(user_table)
                        .values(
                            provider="control-plane",
                            provider_user_id=f"cp:{cp_user_id}",
                            email=email,
                            cp_user_id=cp_user_id,
                            email_verified=email_verified,
                            is_active=True,
                            role="USER",
                            display_name=display_name,
                            avatar_url=avatar_url,
                            prefs={},
                            context={},
                            last_login=now,
                            created_at=now,
                            updated_at=now,
                        )
                        .returning(user_table.c.id)
                    ).scalar_one()
                    user = connection.execute(select(user_table).where(user_table.c.id == user_id)).mappings().one()
                    changed = True

            values: dict[str, Any] = {}
            if user["cp_user_id"] != cp_user_id:
                values["cp_user_id"] = cp_user_id
            if user["provider"] != "control-plane":
                values["provider"] = "control-plane"
            provider_user_id = f"cp:{cp_user_id}"
            if user["provider_user_id"] != provider_user_id:
                values["provider_user_id"] = provider_user_id
            if user["email"] != email:
                collision = connection.execute(
                    select(user_table.c.id).where(user_table.c.email == email, user_table.c.id != user["id"])
                ).first()
                if collision is None:
                    values["email"] = email
            desired_display_name = display_name or user["display_name"]
            desired_avatar_url = avatar_url or user["avatar_url"]
            if user["display_name"] != desired_display_name:
                values["display_name"] = desired_display_name
            if user["avatar_url"] != desired_avatar_url:
                values["avatar_url"] = desired_avatar_url
            if user["email_verified"] != email_verified:
                values["email_verified"] = email_verified
            if user["is_active"] is not True:
                values["is_active"] = True
            if user["last_login"] is None:
                values["last_login"] = now
            if values:
                values["updated_at"] = now
                connection.execute(update(user_table).where(user_table.c.id == user["id"]).values(**values))
                changed = True
            if changed:
                commit_seq = _advance_commit_seq(connection, now)
                user = connection.execute(select(user_table).where(user_table.c.id == user["id"])).mappings().one()
            else:
                commit_seq = _current_commit_seq(connection)
            return {"changed": changed, "user": _user_dto(user), "commit_seq": str(commit_seq)}

    def resolve_local_user(
        self,
        *,
        email: str,
        provider: str,
        provider_user_id: str | None,
        role: str,
        adopt_existing: bool,
        require_email_match: bool,
        max_users: int | None,
        promote_role: bool,
    ) -> dict[str, Any]:
        """Resolve, create, or explicitly adopt a self-hosted local owner."""

        user_table = LiveUser.__table__
        now = datetime.now(UTC)
        with _write_transaction(self.engine) as connection:
            user = connection.execute(select(user_table).where(user_table.c.email == email)).mappings().first()
            adopted = False
            if user is None:
                existing = (
                    connection.execute(
                        select(user_table)
                        .where(or_(user_table.c.provider != "service", user_table.c.provider.is_(None)))
                        .order_by(user_table.c.id.asc())
                        .limit(1)
                    )
                    .mappings()
                    .first()
                )
                if existing is not None and require_email_match:
                    return {"conflict": "owner_email_mismatch", "commit_seq": str(_current_commit_seq(connection))}
                if existing is not None and adopt_existing:
                    user = existing
                    adopted = True
                else:
                    if max_users is not None:
                        count = connection.execute(select(func.count()).select_from(user_table)).scalar_one()
                        if count >= max_users:
                            return {
                                "conflict": "user_limit_reached",
                                "commit_seq": str(_current_commit_seq(connection)),
                            }
                    user_id = connection.execute(
                        insert(user_table)
                        .values(
                            provider=provider,
                            provider_user_id=provider_user_id,
                            email=email,
                            email_verified=True,
                            is_active=True,
                            role=role,
                            prefs={},
                            context={},
                            created_at=now,
                            updated_at=now,
                        )
                        .returning(user_table.c.id)
                    ).scalar_one()
                    user = connection.execute(select(user_table).where(user_table.c.id == user_id)).mappings().one()
                    commit_seq = _advance_commit_seq(connection, now)
                    return {
                        "created": True,
                        "adopted": False,
                        "changed": True,
                        "user": _user_dto(user),
                        "commit_seq": str(commit_seq),
                    }
            changed = False
            if promote_role and user["role"] != role:
                connection.execute(update(user_table).where(user_table.c.id == user["id"]).values(role=role, updated_at=now))
                changed = True
            commit_seq = _advance_commit_seq(connection, now) if changed else _current_commit_seq(connection)
            if changed:
                user = connection.execute(select(user_table).where(user_table.c.id == user["id"])).mappings().one()
            return {
                "created": False,
                "adopted": adopted,
                "changed": changed,
                "user": _user_dto(user),
                "commit_seq": str(commit_seq),
            }

    def create_refresh_session(
        self,
        *,
        user_id: int,
        token_hash: str,
        family_id: str,
        parent_id: int | None,
        created_at: datetime,
        absolute_expires_at: datetime,
        idle_expires_at: datetime,
    ) -> dict[str, Any]:
        """Create one refresh lineage row with exact replay by token hash."""

        table = LiveRefreshSession.__table__
        now = datetime.now(UTC)
        with _write_transaction(self.engine) as connection:
            existing = connection.execute(select(table).where(table.c.token_hash == token_hash)).mappings().first()
            if existing is not None:
                exact = (
                    existing["user_id"] == user_id
                    and existing["family_id"] == family_id
                    and existing["parent_id"] == parent_id
                    and _as_aware_utc(existing["created_at"]) == created_at
                    and _as_aware_utc(existing["absolute_expires_at"]) == absolute_expires_at
                    and _as_aware_utc(existing["idle_expires_at"]) == idle_expires_at
                )
                return {
                    "created": False,
                    "exact_replay": exact,
                    "session_id": existing["id"],
                    "family_id": existing["family_id"],
                    "commit_seq": str(_current_commit_seq(connection)),
                }
            user_exists = connection.execute(select(LiveUser.id).where(LiveUser.id == user_id)).first()
            if user_exists is None:
                return {"not_found": "user", "commit_seq": str(_current_commit_seq(connection))}
            session_id = connection.execute(
                insert(table)
                .values(
                    token_hash=token_hash,
                    user_id=user_id,
                    family_id=family_id,
                    parent_id=parent_id,
                    created_at=created_at,
                    absolute_expires_at=absolute_expires_at,
                    idle_expires_at=idle_expires_at,
                )
                .returning(table.c.id)
            ).scalar_one()
            commit_seq = _advance_commit_seq(connection, now)
            return {
                "created": True,
                "exact_replay": False,
                "session_id": session_id,
                "family_id": family_id,
                "commit_seq": str(commit_seq),
            }

    def rotate_refresh_session(
        self,
        *,
        token_hash: str,
        next_token_hash: str,
        now: datetime,
        idle_expires_at: datetime,
        reuse_grace_seconds: int,
    ) -> dict[str, Any]:
        """Rotate once; a caller can replay the same next hash after an unknown outcome."""

        table = LiveRefreshSession.__table__
        with _write_transaction(self.engine) as connection:
            parent = connection.execute(select(table).where(table.c.token_hash == token_hash)).mappings().first()
            if parent is None or parent["revoked_at"] is not None:
                return {"status": "invalid", "commit_seq": str(_current_commit_seq(connection))}
            if now > _as_aware_utc(parent["absolute_expires_at"]) or now > _as_aware_utc(parent["idle_expires_at"]):
                return {"status": "invalid", "commit_seq": str(_current_commit_seq(connection))}
            user = connection.execute(select(LiveUser.__table__).where(LiveUser.id == parent["user_id"])).mappings().first()
            if user is None or user["is_active"] is not True:
                count = connection.execute(
                    update(table).where(table.c.family_id == parent["family_id"], table.c.revoked_at.is_(None)).values(revoked_at=now)
                ).rowcount
                commit_seq = _advance_commit_seq(connection, now) if count else _current_commit_seq(connection)
                return {
                    "status": "family_revoked" if count else "invalid",
                    "revoked_count": count,
                    "commit_seq": str(commit_seq),
                }

            if parent["used_at"] is not None:
                child = (
                    connection.execute(select(table).where(table.c.parent_id == parent["id"], table.c.revoked_at.is_(None)))
                    .mappings()
                    .first()
                )
                if child is not None and hmac.compare_digest(str(child["token_hash"]), next_token_hash):
                    return {
                        "status": "exact_replay",
                        "session_id": child["id"],
                        "user_id": parent["user_id"],
                        "family_id": parent["family_id"],
                        "user": _user_dto(user),
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                elapsed = (now - _as_aware_utc(parent["used_at"])).total_seconds()
                if elapsed <= reuse_grace_seconds:
                    return {"status": "invalid", "commit_seq": str(_current_commit_seq(connection))}
                count = connection.execute(
                    update(table).where(table.c.family_id == parent["family_id"], table.c.revoked_at.is_(None)).values(revoked_at=now)
                ).rowcount
                commit_seq = _advance_commit_seq(connection, now) if count else _current_commit_seq(connection)
                return {
                    "status": "family_revoked",
                    "revoked_count": count,
                    "commit_seq": str(commit_seq),
                }

            collision = connection.execute(select(table).where(table.c.token_hash == next_token_hash)).first()
            if collision is not None:
                return {"conflict": "next_token_hash", "commit_seq": str(_current_commit_seq(connection))}
            connection.execute(update(table).where(table.c.id == parent["id"]).values(used_at=now))
            child_id = connection.execute(
                insert(table)
                .values(
                    token_hash=next_token_hash,
                    user_id=parent["user_id"],
                    family_id=parent["family_id"],
                    parent_id=parent["id"],
                    created_at=now,
                    absolute_expires_at=parent["absolute_expires_at"],
                    idle_expires_at=idle_expires_at,
                )
                .returning(table.c.id)
            ).scalar_one()
            commit_seq = _advance_commit_seq(connection, now)
            return {
                "status": "rotated",
                "session_id": child_id,
                "user_id": parent["user_id"],
                "family_id": parent["family_id"],
                "user": _user_dto(user),
                "commit_seq": str(commit_seq),
            }

    def revoke_refresh_family(self, *, token_hash: str, now: datetime) -> dict[str, Any]:
        """Find a cookie's family and revoke every still-active member."""

        table = LiveRefreshSession.__table__
        with _write_transaction(self.engine) as connection:
            row = connection.execute(select(table.c.family_id).where(table.c.token_hash == token_hash)).first()
            if row is None:
                return {
                    "found": False,
                    "changed": False,
                    "revoked_count": 0,
                    "commit_seq": str(_current_commit_seq(connection)),
                }
            count = connection.execute(
                update(table).where(table.c.family_id == row.family_id, table.c.revoked_at.is_(None)).values(revoked_at=now)
            ).rowcount
            commit_seq = _advance_commit_seq(connection, now) if count else _current_commit_seq(connection)
            return {
                "found": True,
                "changed": bool(count),
                "revoked_count": count,
                "commit_seq": str(commit_seq),
            }

    def update_user(
        self,
        *,
        user_id: int,
        display_name: str | None,
        avatar_url: str | None,
        prefs: dict[str, Any] | None,
        update_mask: list[str],
    ) -> dict[str, Any]:
        """Update the bounded user profile without conflating omitted and null."""

        table = LiveUser.__table__
        now = datetime.now(UTC)
        requested = {"display_name": display_name, "avatar_url": avatar_url, "prefs": prefs}
        with _write_transaction(self.engine) as connection:
            row = connection.execute(select(table).where(table.c.id == user_id)).mappings().first()
            if row is None:
                return {"found": False, "changed": False, "commit_seq": str(_current_commit_seq(connection))}
            values = {field: requested[field] for field in update_mask if row[field] != requested[field]}
            if values:
                values["updated_at"] = now
                connection.execute(update(table).where(table.c.id == user_id).values(**values))
                commit_seq = _advance_commit_seq(connection, now)
                row = connection.execute(select(table).where(table.c.id == user_id)).mappings().one()
            else:
                commit_seq = _current_commit_seq(connection)
            return {
                "found": True,
                "changed": bool(values),
                "user": _user_dto(row),
                "commit_seq": str(commit_seq),
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


def _advance_commit_seq(connection, now: datetime) -> int:
    return connection.execute(
        update(catalog_meta)
        .where(catalog_meta.c.singleton == 1)
        .values(commit_seq=catalog_meta.c.commit_seq + 1, updated_at=now.isoformat())
        .returning(catalog_meta.c.commit_seq)
    ).scalar_one()


def _user_dto(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "email": row["email"],
        "provider": row["provider"],
        "provider_user_id": row["provider_user_id"],
        "cp_user_id": row["cp_user_id"],
        "email_verified": bool(row["email_verified"]),
        "is_active": bool(row["is_active"]),
        "role": str(row["role"]),
        "display_name": row["display_name"],
        "avatar_url": row["avatar_url"],
        "prefs": row["prefs"],
        "context": row["context"] or {},
        "last_login": _encode_datetime(row["last_login"]),
        "created_at": _encode_datetime(row["created_at"]),
        "updated_at": _encode_datetime(row["updated_at"]),
    }


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
