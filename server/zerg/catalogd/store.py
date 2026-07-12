"""Synchronous catalog operations executed on catalogd's dedicated DB thread."""

from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from sqlalchemy import Engine
from sqlalchemy import func
from sqlalchemy import insert
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.orm import Session

from zerg.catalogd.schema import catalog_meta
from zerg.models.live_store import LiveArchiveOutbox
from zerg.models.live_store import LiveDeviceToken
from zerg.models.live_store import LiveHeartbeatStamp
from zerg.models.live_store import LiveLaunchReadiness
from zerg.models.live_store import LiveRefreshSession
from zerg.models.live_store import LiveRuntimeState
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.models.live_store import LiveSessionThreadAlias
from zerg.models.live_store import LiveTimelineCard
from zerg.models.live_store import LiveUser

DEVICE_TOKEN_LIMIT_PER_OWNER = 1_000
SESSION_READ_LIMIT = 100
MACHINE_ENROLLMENT_LIMIT = 1_000
WORKSPACE_CANDIDATE_LIMIT = 5_000
# The capability projector consumes only its highest-ranked connection.  The
# ordering below is deliberately identical to that projector, so returning the
# winner preserves semantics while keeping a 100-row page bounded.
SESSION_CONNECTION_LIMIT = 1
_CONTROL_LEASE_TTL = timedelta(minutes=15)
_EXCLUDED_WORKSPACE_ENVIRONMENTS = ("test", "e2e")
_RECENCY_BUCKETS: tuple[tuple[float, int], ...] = (
    (1.0, 100),
    (4.0, 70),
    (14.0, 50),
    (31.0, 30),
)


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

    def get_active_owner(self) -> dict[str, Any]:
        """Resolve the single-tenant owner without exposing a SQLite reader."""

        user_table = LiveUser.__table__
        with _read_snapshot(self.engine) as connection:
            owner_id = connection.execute(
                select(user_table.c.id).where(user_table.c.is_active.is_(True)).order_by(user_table.c.id.asc()).limit(1)
            ).scalar_one_or_none()
            return {
                "found": owner_id is not None,
                "owner_id": int(owner_id) if owner_id is not None else None,
                "commit_seq": str(_current_commit_seq(connection)),
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

    def apply_machine_heartbeat(
        self,
        *,
        heartbeat: dict[str, Any],
        managed_leases: list[dict[str, Any]],
        managed_leases_present: bool,
        owner_id: int | None,
    ) -> dict[str, Any]:
        """Atomically persist and reconcile one hosted Machine Agent heartbeat."""

        from zerg.services.live_session_state import mark_missing_live_sessions
        from zerg.services.live_session_state import upsert_live_sessions_from_managed_leases
        from zerg.services.managed_control_state import mark_missing_live_control_leases
        from zerg.services.managed_control_state import upsert_live_control_leases

        device_id = str(heartbeat["device_id"])
        received_at = heartbeat["received_at"]
        assert isinstance(received_at, datetime)
        request_key = _heartbeat_idempotency_key(device_id=device_id, received_at=received_at)
        request_sha256 = _heartbeat_request_sha256(
            heartbeat=heartbeat,
            managed_leases=managed_leases,
            managed_leases_present=managed_leases_present,
            owner_id=owner_id,
        )
        outbox = LiveArchiveOutbox.__table__
        stamp = LiveHeartbeatStamp.__table__
        with _write_transaction(self.engine) as connection:
            replay = connection.execute(select(outbox).where(outbox.c.idempotency_key == request_key)).mappings().first()
            if replay is not None:
                payload = _decode_json_object(replay["payload_json"])
                if payload.get("request_sha256") != request_sha256:
                    return {
                        "idempotency_conflict": True,
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                stored_result = payload.get("catalog_result")
                if not isinstance(stored_result, dict):
                    raise RuntimeError("heartbeat replay receipt is incomplete")
                return {**stored_result, "exact_replay": True}

            incoming_digest = str(heartbeat.get("sessions_digest") or "").strip() or None
            previous_sessions_digest: str | None = None
            if managed_leases_present and incoming_digest is not None:
                previous = connection.execute(
                    select(stamp.c.sessions_digest)
                    .where(stamp.c.device_id == device_id)
                    .order_by(stamp.c.received_at.desc(), stamp.c.id.desc())
                    .limit(1)
                ).scalar_one_or_none()
                previous_sessions_digest = str(previous or "").strip() or None

            cutoff = received_at - timedelta(days=30)
            connection.execute(stamp.delete().where(stamp.c.device_id == device_id, stamp.c.received_at < cutoff))
            connection.execute(insert(stamp).values(**heartbeat))
            provisional_payload = {
                "heartbeat": _jsonable_catalog_value(heartbeat),
                "request_sha256": request_sha256,
                "catalog_result": None,
            }
            outbox_id = connection.execute(
                insert(outbox)
                .values(
                    idempotency_key=request_key,
                    kind="heartbeat_stamp.v1",
                    payload_json=json.dumps(provisional_payload, sort_keys=True, separators=(",", ":")),
                    created_at=received_at,
                )
                .returning(outbox.c.id)
            ).scalar_one()

            lease_objects = [SimpleNamespace(**lease) for lease in managed_leases]
            touched: set[UUID] = set()
            orm = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            try:
                if lease_objects:
                    touched.update(
                        upsert_live_control_leases(
                            orm,
                            lease_objects,
                            device_id=device_id,
                            received_at=received_at,
                        )
                    )
                    touched.update(
                        upsert_live_sessions_from_managed_leases(
                            orm,
                            lease_objects,
                            device_id=device_id,
                            owner_id=owner_id,
                            received_at=received_at,
                        )
                    )
                if managed_leases_present:
                    touched.update(
                        mark_missing_live_control_leases(
                            orm,
                            lease_objects,
                            device_id=device_id,
                            received_at=received_at,
                        )
                    )
                    touched.update(
                        mark_missing_live_sessions(
                            orm,
                            {UUID(str(lease.session_id)) for lease in lease_objects},
                            device_id=device_id,
                            received_at=received_at,
                        )
                    )
                orm.commit()
            except BaseException:
                orm.rollback()
                raise
            finally:
                orm.close()

            commit_seq = _advance_commit_seq(connection, received_at)
            result = {
                "previous_sessions_digest": previous_sessions_digest,
                "commit_seq": str(commit_seq),
                "touched_session_ids": sorted(str(session_id) for session_id in touched),
                "exact_replay": False,
            }
            provisional_payload["catalog_result"] = result
            connection.execute(
                update(outbox)
                .where(outbox.c.id == outbox_id)
                .values(payload_json=json.dumps(provisional_payload, sort_keys=True, separators=(",", ":")))
            )
            return result

    def apply_session_runtime(self, *, events: list[Any]) -> dict[str, Any]:
        """Atomically reduce one bounded runtime batch and queue durable events."""

        from zerg.services.live_archive_outbox import enqueue_runtime_events_outbox
        from zerg.services.session_runtime import ingest_live_runtime_events

        observed_at = datetime.now(UTC)
        with _write_transaction(self.engine) as connection:
            orm = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            try:
                result = ingest_live_runtime_events(orm, events)
                updated_keys = set(result.updated_runtime_keys)
                resume_session_ids = {
                    str(event.session_id)
                    for event in events
                    if event.session_id is not None
                    and event.runtime_key in updated_keys
                    and event.kind == "phase_signal"
                    and event.phase in {"thinking", "running"}
                }
                if resume_session_ids:
                    orm.query(LiveSessionCatalog).filter(
                        LiveSessionCatalog.session_id.in_(resume_session_ids),
                        LiveSessionCatalog.user_state == "snoozed",
                    ).update(
                        {"user_state": "active", "user_state_at": observed_at},
                        synchronize_session=False,
                    )
                archive_events = [event for event in events if not _is_bridge_live_transcript_event(event)]
                enqueue_runtime_events_outbox(orm, archive_events)
                orm.commit()
            except BaseException:
                orm.rollback()
                raise
            finally:
                orm.close()
            commit_seq = _advance_commit_seq(connection, observed_at)
            return {
                **result.model_dump(mode="json"),
                "commit_seq": str(commit_seq),
            }

    def list_session_timeline(
        self,
        *,
        project: str | None,
        provider: str | None,
        environment: str | None,
        include_test: bool,
        hide_autonomous: bool,
        include_automation: bool,
        device_id: str | None,
        days_back: int,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        """Return one bounded timeline page and all raw facts in one snapshot."""

        observed_at = datetime.now(UTC)
        since = observed_at - timedelta(days=days_back)
        card = LiveTimelineCard.__table__
        catalog = LiveSessionCatalog.__table__
        with _read_snapshot(self.engine) as connection:
            where = [func.coalesce(card.c.last_activity_at, card.c.started_at) >= since]
            if project is not None:
                where.append(card.c.project == project)
            if provider is not None:
                where.append(card.c.provider == provider)
            if environment is not None:
                where.append(card.c.environment == environment)
            elif not include_test:
                where.append(card.c.environment.notin_(("test", "e2e")))
            if device_id is not None:
                where.append(card.c.device_id == device_id)
            if hide_autonomous:
                where.append(
                    or_(
                        card.c.user_messages > 0,
                        card.c.archive_state == "pending",
                        card.c.launch_actor == "human_ui",
                        card.c.launch_surface.in_(("web", "ios", "api")),
                    )
                )
            if not include_automation:
                where.append(or_(card.c.origin_kind.is_(None), card.c.origin_kind != "hatch_automation"))

            joined = card.join(catalog, catalog.c.session_id == card.c.session_id)
            total = int(connection.execute(select(func.count()).select_from(joined).where(*where)).scalar_one())
            session_ids = [
                str(value)
                for value in connection.execute(
                    select(card.c.session_id)
                    .select_from(joined)
                    .where(*where)
                    .order_by(
                        func.coalesce(card.c.last_activity_at, card.c.started_at).desc(),
                        card.c.session_id.desc(),
                    )
                    .limit(limit)
                    .offset(offset)
                ).scalars()
            ]
            facts = _assemble_session_facts(
                connection,
                session_ids=session_ids,
                observed_at=observed_at,
                compact=True,
            )
            has_real_sessions = total == 0 or any((item["catalog"].get("device_id") or "") != "demo-mac" for item in facts)
            return {
                "commit_seq": str(_current_commit_seq(connection)),
                "observed_at": observed_at.isoformat(),
                "rows": [
                    {
                        "thread_id": item["primary_thread"]["id"] if item["primary_thread"] is not None else None,
                        "facts": item,
                    }
                    for item in facts
                ],
                "total": total,
                "has_real_sessions": has_real_sessions,
            }

    def read_session(self, *, session_id: str) -> dict[str, Any]:
        observed_at = datetime.now(UTC)
        with _read_snapshot(self.engine) as connection:
            facts = _assemble_session_facts(
                connection,
                session_ids=[session_id],
                observed_at=observed_at,
                compact=False,
            )
            return {
                "commit_seq": str(_current_commit_seq(connection)),
                "observed_at": observed_at.isoformat(),
                "found": bool(facts),
                "facts": facts[0] if facts else None,
            }

    def resolve_session_prefix(self, *, prefix: str) -> dict[str, Any]:
        observed_at = datetime.now(UTC)
        catalog = LiveSessionCatalog.__table__
        user = LiveUser.__table__
        with _read_snapshot(self.engine) as connection:
            matches = list(
                connection.execute(
                    select(
                        catalog.c.session_id,
                        catalog.c.provider,
                        catalog.c.device_name,
                        catalog.c.started_at,
                        catalog.c.ended_at,
                    )
                    .where(catalog.c.session_id.like(f"{prefix}%"))
                    .order_by(catalog.c.session_id.asc())
                    .limit(2)
                ).mappings()
            )
            status = "unique" if len(matches) == 1 else "ambiguous" if len(matches) > 1 else "missing"
            session_preview: dict[str, Any] | None = None
            owner_preview: dict[str, str | None] | None = None
            if status == "unique":
                match = matches[0]
                session_preview = {
                    "session_id": str(match["session_id"]),
                    "provider": str(match["provider"]),
                    "device_name": match["device_name"],
                    "started_at": _encode_datetime(match["started_at"]),
                    "ended_at": _encode_datetime(match["ended_at"]),
                }
                owner_row = (
                    connection.execute(select(user.c.display_name, user.c.email).order_by(user.c.id.asc()).limit(1)).mappings().first()
                )
                if owner_row is not None:
                    display_name = str(owner_row["display_name"] or "").strip() or None
                    email = str(owner_row["email"] or "").strip()
                    email_local = email.split("@", 1)[0] or None if "@" in email else None
                    owner_preview = {"display_name": display_name, "email_local": email_local}
            return {
                "commit_seq": str(_current_commit_seq(connection)),
                "observed_at": observed_at.isoformat(),
                "status": status,
                "session_id": session_preview["session_id"] if session_preview is not None else None,
                "session": session_preview,
                "owner": owner_preview,
            }

    def list_machine_enrollments(self, *, owner_id: int) -> dict[str, Any]:
        observed_at = datetime.now(UTC)
        token = LiveDeviceToken.__table__
        with _read_snapshot(self.engine) as connection:
            rows = connection.execute(
                select(token.c.device_id, token.c.last_used_at, token.c.created_at)
                .where(token.c.owner_id == owner_id, token.c.revoked_at.is_(None))
                .order_by(token.c.device_id.asc(), token.c.last_used_at.desc(), token.c.created_at.desc())
                .limit(MACHINE_ENROLLMENT_LIMIT + 1)
            ).all()
            if len(rows) > MACHINE_ENROLLMENT_LIMIT:
                return {
                    "commit_seq": str(_current_commit_seq(connection)),
                    "observed_at": observed_at.isoformat(),
                    "enrollments": [],
                    "total": 0,
                    "limit_exceeded": True,
                }
            latest: dict[str, datetime | None] = {}
            created: dict[str, datetime | None] = {}
            for raw_device_id, last_used_at, created_at in rows:
                key = str(raw_device_id or "")
                if not key:
                    continue
                candidate = _as_aware_utc(last_used_at or created_at)
                if key not in latest or (candidate is not None and (latest[key] is None or candidate > latest[key])):
                    latest[key] = candidate
                    created[key] = _as_aware_utc(created_at)
            enrollments = [
                {
                    "device_id": key,
                    "last_used_at": _encode_datetime(latest[key]),
                    "created_at": _encode_datetime(created[key]),
                }
                for key in sorted(latest)
            ]
            return {
                "commit_seq": str(_current_commit_seq(connection)),
                "observed_at": observed_at.isoformat(),
                "enrollments": enrollments,
                "total": len(enrollments),
                "limit_exceeded": False,
            }

    def list_machine_workspaces(
        self,
        *,
        owner_id: int,
        device_id: str,
        limit: int,
        days_back: int,
    ) -> dict[str, Any]:
        observed_at = datetime.now(UTC)
        since = observed_at - timedelta(days=days_back)
        token = LiveDeviceToken.__table__
        catalog = LiveSessionCatalog.__table__
        with _read_snapshot(self.engine) as connection:
            enrolled = connection.execute(
                select(token.c.id)
                .where(
                    token.c.owner_id == owner_id,
                    token.c.device_id == device_id,
                    token.c.revoked_at.is_(None),
                )
                .limit(1)
            ).first()
            rows = []
            if enrolled is not None:
                rows = connection.execute(
                    select(
                        catalog.c.cwd,
                        catalog.c.git_repo,
                        catalog.c.git_branch,
                        catalog.c.last_activity_at,
                        catalog.c.started_at,
                    )
                    .where(
                        catalog.c.device_id == device_id,
                        catalog.c.cwd.is_not(None),
                        catalog.c.cwd.like("/%"),
                        catalog.c.environment.notin_(_EXCLUDED_WORKSPACE_ENVIRONMENTS),
                        func.coalesce(catalog.c.last_activity_at, catalog.c.started_at) >= since,
                    )
                    .order_by(func.coalesce(catalog.c.last_activity_at, catalog.c.started_at).desc())
                    .limit(WORKSPACE_CANDIDATE_LIMIT + 1)
                ).all()
            limit_exceeded = len(rows) > WORKSPACE_CANDIDATE_LIMIT
            if limit_exceeded:
                rows = rows[:WORKSPACE_CANDIDATE_LIMIT]
            groups: dict[str, dict[str, Any]] = {}
            for cwd, git_repo, git_branch, last_activity_at, started_at in rows:
                used_at = _as_aware_utc(last_activity_at or started_at)
                path = str(cwd or "")
                if used_at is None or not path:
                    continue
                group = groups.setdefault(
                    path,
                    {"score": 0.0, "session_count": 0, "last_used_at": None, "git_repo": None, "git_branch": None},
                )
                group["score"] += _recency_weight(max(0.0, (observed_at - used_at).total_seconds() / 86400.0))
                group["session_count"] += 1
                if group["last_used_at"] is None or used_at > group["last_used_at"]:
                    group.update(last_used_at=used_at, git_repo=git_repo, git_branch=git_branch)
            workspaces = [
                {
                    "path": path,
                    "label": _workspace_label(path, group["git_repo"], group["git_branch"]),
                    "git_repo": group["git_repo"],
                    "git_branch": group["git_branch"],
                    "score": group["score"],
                    "last_used_at": _encode_datetime(group["last_used_at"]),
                    "session_count": group["session_count"],
                }
                for path, group in groups.items()
            ]
            workspaces.sort(key=lambda item: (item["score"], item["last_used_at"] or ""), reverse=True)
            return {
                "commit_seq": str(_current_commit_seq(connection)),
                "observed_at": observed_at.isoformat(),
                "device_id": device_id,
                "workspaces": workspaces[:limit],
                "limit_exceeded": limit_exceeded,
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


def _assemble_session_facts(
    connection,
    *,
    session_ids: list[str],
    observed_at: datetime,
    compact: bool,
) -> list[dict[str, Any]]:
    """Bulk-load response-relevant session facts without presentation inference."""

    if not session_ids:
        return []
    catalog_table = LiveSessionCatalog.__table__
    card_table = LiveTimelineCard.__table__
    runtime_table = LiveRuntimeState.__table__
    readiness_table = LiveLaunchReadiness.__table__
    thread_table = LiveSessionThread.__table__
    run_table = LiveSessionRun.__table__
    connection_table = LiveSessionConnection.__table__
    alias_table = LiveSessionThreadAlias.__table__

    catalogs = {
        str(row["session_id"]): row
        for row in connection.execute(select(catalog_table).where(catalog_table.c.session_id.in_(session_ids))).mappings()
    }
    cards = {
        str(row["session_id"]): row
        for row in connection.execute(select(card_table).where(card_table.c.session_id.in_(session_ids))).mappings()
    }
    runtime_by_session: dict[str, Any] = {}
    for row in connection.execute(
        select(runtime_table)
        .where(runtime_table.c.session_id.in_(session_ids))
        .order_by(
            runtime_table.c.updated_at.desc(),
            runtime_table.c.runtime_version.desc(),
            runtime_table.c.runtime_key.desc(),
        )
    ).mappings():
        runtime_by_session.setdefault(str(row["session_id"]), row)
    readiness_by_session = {
        str(row["session_id"]): row
        for row in connection.execute(select(readiness_table).where(readiness_table.c.session_id.in_(session_ids))).mappings()
        if _as_aware_utc(row["expires_at"]) is None or _as_aware_utc(row["expires_at"]) > observed_at
    }

    thread_rows = list(
        connection.execute(
            select(thread_table)
            .where(thread_table.c.session_id.in_(session_ids))
            .order_by(thread_table.c.created_at.asc(), thread_table.c.id.asc())
        ).mappings()
    )
    threads_by_session: dict[str, list[Any]] = {}
    for row in thread_rows:
        threads_by_session.setdefault(str(row["session_id"]), []).append(row)
    primary_by_session: dict[str, Any] = {}
    for session_id, rows in threads_by_session.items():
        requested = catalogs.get(session_id, {}).get("primary_thread_id")
        primary_by_session[session_id] = next(
            (row for row in rows if requested is not None and str(row["id"]) == str(requested)),
            next((row for row in rows if int(row["is_primary"] or 0) == 1), rows[0]),
        )

    thread_ids = [str(row["id"]) for row in primary_by_session.values()]
    latest_run_by_thread: dict[str, Any] = {}
    if thread_ids:
        for row in connection.execute(
            select(run_table).where(run_table.c.thread_id.in_(thread_ids)).order_by(run_table.c.started_at.desc(), run_table.c.id.desc())
        ).mappings():
            latest_run_by_thread.setdefault(str(row["thread_id"]), row)
    run_ids = [str(row["id"]) for row in latest_run_by_thread.values()]
    connections_by_run: dict[str, list[Any]] = {}
    if run_ids:
        for row in connection.execute(
            select(connection_table)
            .where(connection_table.c.run_id.in_(run_ids))
            .order_by(connection_table.c.acquired_at.asc(), connection_table.c.id.asc())
        ).mappings():
            connections_by_run.setdefault(str(row["run_id"]), []).append(row)

    provider_alias_by_thread: dict[str, Any] = {}
    if thread_ids:
        for row in connection.execute(
            select(alias_table)
            .where(
                alias_table.c.thread_id.in_(thread_ids),
                alias_table.c.alias_kind == "provider_session_id",
            )
            .order_by(alias_table.c.last_seen_at.desc(), alias_table.c.id.desc())
        ).mappings():
            provider_alias_by_thread.setdefault(str(row["thread_id"]), row)

    result: list[dict[str, Any]] = []
    for session_id in session_ids:
        catalog = catalogs.get(session_id)
        card = cards.get(session_id)
        if catalog is None:
            continue
        primary_thread = primary_by_session.get(session_id)
        thread_id = str(primary_thread["id"]) if primary_thread is not None else None
        latest_run = latest_run_by_thread.get(thread_id) if thread_id is not None else None
        run_id = str(latest_run["id"]) if latest_run is not None else None
        result.append(
            {
                "catalog": _row_dto(catalog, fields=_CATALOG_FIELDS, text_limits=_CATALOG_TEXT_LIMITS),
                "card": _row_dto(card, fields=_CARD_FIELDS, text_limits=_CARD_TEXT_LIMITS),
                "runtime": _runtime_dto(runtime_by_session.get(session_id), compact=compact),
                "readiness": _row_dto(
                    readiness_by_session.get(session_id),
                    fields=_READINESS_FIELDS,
                    text_limits={"error_message": 256},
                ),
                "primary_thread": _row_dto(primary_thread, fields=_THREAD_FIELDS),
                "latest_run": _row_dto(latest_run, fields=_RUN_FIELDS, text_limits=_RUN_TEXT_LIMITS),
                "connections": [
                    _row_dto(row, fields=_CONNECTION_FIELDS, text_limits=_CONNECTION_TEXT_LIMITS)
                    for row in _bounded_connections(connections_by_run.get(run_id, []), observed_at=observed_at)
                ],
                "provider_alias": (
                    _truncate_utf8(str(provider_alias_by_thread[thread_id]["alias_value"]), 512)
                    if not compact and thread_id in provider_alias_by_thread
                    else None
                ),
            }
        )
    return result


_CATALOG_TEXT_LIMITS = {
    "provider": 64,
    "environment": 32,
    "project": 255,
    "device_id": 255,
    "device_name": 255,
    "cwd": 512,
    "git_repo": 512,
    "git_branch": 255,
    "summary": 768,
    "summary_title": 255,
    "anchor_title": 255,
    "first_user_message_preview": 384,
}
_CARD_TEXT_LIMITS = {
    "summary_title": 255,
    "first_user_message_preview": 384,
    "archive_state": 32,
    "origin_kind": 64,
    "launch_actor": 32,
    "launch_surface": 32,
}

_CATALOG_FIELDS = frozenset(
    {
        "session_id",
        "provider",
        "environment",
        "project",
        "device_id",
        "device_name",
        "cwd",
        "git_repo",
        "git_branch",
        "started_at",
        "ended_at",
        "closed_at",
        "close_reason",
        "last_activity_at",
        "user_messages",
        "assistant_messages",
        "tool_calls",
        "summary",
        "summary_title",
        "anchor_title",
        "first_user_message_preview",
        "transcript_revision",
        "summary_revision",
        "user_state",
        "user_state_at",
        "primary_thread_id",
        "loop_mode",
        "notification_muted",
        "origin_kind",
        "hidden_from_default_timeline",
        "launch_actor",
        "launch_surface",
        "permission_mode",
    }
)
_CARD_FIELDS = frozenset(
    {
        "session_id",
        "last_activity_at",
        "summary_title",
        "first_user_message_preview",
        "user_messages",
        "assistant_messages",
        "tool_calls",
        "transcript_revision",
        "archive_state",
    }
)
_RUNTIME_FIELDS = frozenset(
    {
        "runtime_key",
        "session_id",
        "thread_id",
        "run_id",
        "provider",
        "device_id",
        "phase",
        "phase_source",
        "active_tool",
        "phase_started_at",
        "execution_started_at",
        "last_runtime_signal_at",
        "last_progress_at",
        "last_live_at",
        "timeline_anchor_at",
        "freshness_expires_at",
        "terminal_state",
        "terminal_reason",
        "terminal_source",
        "terminal_at",
        "pending_interaction_id",
        "pending_interaction_kind",
        "pending_interaction_opened_at",
        "pending_interaction_updated_at",
        "pending_interaction_projection_json",
        "pending_interaction_can_respond",
        "runtime_version",
        "updated_at",
    }
)
_READINESS_FIELDS = frozenset(
    {
        "session_id",
        "owner_id",
        "client_request_id",
        "provider",
        "device_id",
        "machine_id",
        "project",
        "execution_lifetime",
        "state",
        "command_id",
        "error_code",
        "error_message",
        "expires_at",
        "created_at",
        "updated_at",
    }
)
_THREAD_FIELDS = frozenset(
    {
        "id",
        "session_id",
        "provider",
        "parent_thread_id",
        "parent_event_id",
        "branch_kind",
        "origin_kind",
        "hidden_from_default_timeline",
        "is_primary",
        "created_at",
        "updated_at",
    }
)
_RUN_FIELDS = frozenset(
    {
        "id",
        "thread_id",
        "provider",
        "host_id",
        "boot_id",
        "pid",
        "process_start_time",
        "cwd",
        "launch_origin",
        "started_at",
        "ended_at",
        "exit_status",
    }
)
_CONNECTION_FIELDS = frozenset(
    {
        "id",
        "run_id",
        "control_plane",
        "acquisition_kind",
        "state",
        "device_id",
        "can_send_input",
        "can_interrupt",
        "can_terminate",
        "can_tail_output",
        "can_resume",
        "acquired_at",
        "released_at",
        "last_health_at",
    }
)
_RUNTIME_TEXT_LIMITS = {
    "runtime_key": 255,
    "provider": 64,
    "device_id": 255,
    "phase": 32,
    "phase_source": 32,
    "active_tool": 128,
    "terminal_state": 32,
    "terminal_reason": 64,
    "terminal_source": 64,
    "pending_interaction_id": 255,
    "pending_interaction_kind": 32,
}
_RUN_TEXT_LIMITS = {
    "provider": 64,
    "host_id": 255,
    "boot_id": 64,
    "cwd": 512,
    "launch_origin": 32,
    "exit_status": 64,
}
_CONNECTION_TEXT_LIMITS = {
    "control_plane": 64,
    "acquisition_kind": 32,
    "state": 32,
    "device_id": 255,
}


def _row_dto(
    row,
    *,
    fields: frozenset[str] | None = None,
    text_limits: dict[str, int] | None = None,
) -> dict[str, Any] | None:
    if row is None:
        return None
    limits = text_limits or {}
    result: dict[str, Any] = {}
    for key, value in row.items():
        if fields is not None and key not in fields:
            continue
        if isinstance(value, datetime):
            result[key] = _encode_datetime(value)
        elif isinstance(value, UUID):
            result[key] = str(value)
        elif isinstance(value, str):
            result[key] = _truncate_utf8(value, limits.get(key, 255))
        else:
            result[key] = value
    return result


def _runtime_dto(row, *, compact: bool) -> dict[str, Any] | None:
    result = _row_dto(row, fields=_RUNTIME_FIELDS, text_limits=_RUNTIME_TEXT_LIMITS)
    if result is not None:
        result["pending_interaction_projection_json"] = _bounded_pause_projection(
            result.get("pending_interaction_projection_json"),
            compact=compact,
        )
    return result


def _bounded_pause_projection(value: object, *, compact: bool) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    string_limits = {
        "id": 128,
        "request_key": 255,
        "session_id": 64,
        "runtime_key": 255,
        "kind": 64,
        "status": 32,
        "provider": 64,
        "title": 96 if compact else 160,
        "summary": 128 if compact else 256,
        "tool_name": 128,
        "occurred_at": 64,
        "last_seen_at": 64,
        "resolved_at": 64,
        "expires_at": 64,
    }
    for key, maximum in string_limits.items():
        raw = value.get(key)
        result[key] = _truncate_utf8(str(raw), maximum) if raw is not None else None
    result["can_respond"] = bool(value.get("can_respond"))
    questions: list[dict[str, Any]] = []
    for raw_question in value.get("questions", [])[:3] if isinstance(value.get("questions"), list) else []:
        if not isinstance(raw_question, dict):
            continue
        options: list[dict[str, str | None]] = []
        for raw_option in raw_question.get("options", [])[:4] if isinstance(raw_question.get("options"), list) else []:
            if not isinstance(raw_option, dict):
                continue
            options.append(
                {
                    "label": _truncate_utf8(str(raw_option.get("label") or ""), 32 if compact else 48),
                    "description": (
                        _truncate_utf8(
                            str(raw_option["description"]),
                            32 if compact else 64,
                        )
                        if raw_option.get("description")
                        else None
                    ),
                    "value": (
                        _truncate_utf8(str(raw_option["value"]), 32 if compact else 48) if raw_option.get("value") is not None else None
                    ),
                }
            )
        questions.append(
            {
                "id": _truncate_utf8(str(raw_question.get("id") or ""), 128),
                "header": (_truncate_utf8(str(raw_question["header"]), 48 if compact else 64) if raw_question.get("header") else None),
                "question": _truncate_utf8(
                    str(raw_question.get("question") or "Answer required"),
                    128 if compact else 192,
                ),
                "multi_select": bool(raw_question.get("multi_select")),
                "options": options,
            }
        )
    result["questions"] = questions
    return result


def _bounded_connections(rows: list[Any], *, observed_at: datetime) -> list[Any]:
    state_priority = {"attached": 5, "degraded": 4, "detached": 3, "released": 2, "ended": 1}

    def key(row) -> tuple[Any, ...]:
        state = str(row["state"] or "")
        last_health = _as_aware_utc(row["last_health_at"])
        if state in {"attached", "degraded"} and (last_health is None or observed_at - last_health > _CONTROL_LEASE_TTL):
            state = "detached"
        capabilities = sum(bool(row[field]) for field in ("can_send_input", "can_interrupt", "can_terminate", "can_tail_output"))
        return (
            state_priority.get(state, 0),
            capabilities,
            last_health or datetime.min.replace(tzinfo=UTC),
            int(row["id"] or 0),
        )

    return sorted(rows, key=key, reverse=True)[:SESSION_CONNECTION_LIMIT]


def _truncate_utf8(value: str, maximum_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return value
    return encoded[:maximum_bytes].decode("utf-8", errors="ignore")


def _heartbeat_idempotency_key(*, device_id: str, received_at: datetime) -> str:
    return f"heartbeat_stamp.v1:{device_id}:{received_at.isoformat()}"


def _heartbeat_request_sha256(
    *,
    heartbeat: dict[str, Any],
    managed_leases: list[dict[str, Any]],
    managed_leases_present: bool,
    owner_id: int | None,
) -> str:
    payload = {
        "heartbeat": _jsonable_catalog_value(heartbeat),
        "managed_leases": _jsonable_catalog_value(managed_leases),
        "managed_leases_present": managed_leases_present,
        "owner_id": owner_id,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _jsonable_catalog_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _encode_datetime(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable_catalog_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable_catalog_value(item) for item in value]
    return value


def _decode_json_object(value: object) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value or "{}"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("catalog receipt JSON is invalid") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("catalog receipt JSON is not an object")
    return decoded


def _is_bridge_live_transcript_event(event: Any) -> bool:
    payload = event.payload if isinstance(event.payload, dict) else {}
    return (
        str(event.provider or "").strip().lower() == "codex"
        and str(event.source or "").strip().lower() == "codex_bridge_live"
        and event.kind == "progress_signal"
        and payload.get("progress_kind") == "bridge_live_transcript_delta"
    )


def _recency_weight(age_days: float) -> int:
    for threshold, weight in _RECENCY_BUCKETS:
        if age_days <= threshold:
            return weight
    return 10


def _workspace_label(path: str, git_repo: str | None, git_branch: str | None) -> str:
    if git_repo:
        name = str(git_repo).rstrip("/").rsplit("/", 1)[-1]
        if name.endswith(".git"):
            name = name[:-4]
        if name:
            return f"{name} ({git_branch})" if git_branch else name
    parts = path.split("/")
    if len(parts) >= 3 and parts[1] == "Users":
        return "~/" + "/".join(parts[3:]) if len(parts) > 3 else "~"
    return path


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
