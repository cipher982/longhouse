"""CatalogStore: users, owners, device tokens, refresh sessions, APNs registrations and presence."""

from __future__ import annotations

import hmac
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

from sqlalchemy import delete
from sqlalchemy import func
from sqlalchemy import insert
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import update

from zerg.catalogd.fact_reducer import _advance_commit_seq
from zerg.catalogd.fact_reducer import _current_commit_seq
from zerg.catalogd.schema import catalog_meta

# store.py imports this module after defining these, at its end.
from zerg.catalogd.store import DEVICE_TOKEN_LIMIT_PER_OWNER
from zerg.catalogd.store import _as_aware_utc
from zerg.catalogd.store import _decode_json_object
from zerg.catalogd.store import _encode_datetime
from zerg.catalogd.store import _is_oss_trial_owner
from zerg.catalogd.store import _read_snapshot
from zerg.catalogd.store import _user_dto
from zerg.catalogd.store import _write_transaction
from zerg.models.live_store import LiveAPNSDeviceRegistration
from zerg.models.live_store import LiveAPNSLiveActivityRegistration
from zerg.models.live_store import LiveDeviceToken
from zerg.models.live_store import LiveMachinePresence
from zerg.models.live_store import LiveNotificationClientPresence
from zerg.models.live_store import LiveRefreshSession
from zerg.models.live_store import LiveUser


class AccountsMixin:
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
        """Resolve one user, reporting whether a first-login stamp is still owed.

        Read-only regardless of `touch_last_login`. The stamp is written by
        `touch_user_login` only when one is actually due, because the previous
        shape opened `BEGIN IMMEDIATE` on the strength of the *flag* rather than
        the *need*: `last_login` is set once and never again, so every browser
        request from an established user took SQLite's write lock and wrote
        nothing.
        """

        user_table = LiveUser.__table__
        with _read_snapshot(self.engine) as connection:
            row = connection.execute(select(user_table).where(user_table.c.id == user_id)).mappings().first()
            if row is None:
                return {"found": False, "changed": False, "touch_due": False, "commit_seq": str(_current_commit_seq(connection))}
            return {
                "found": True,
                "changed": False,
                "touch_due": bool(touch_last_login and row["last_login"] is None),
                "user": _user_dto(row),
                "commit_seq": str(_current_commit_seq(connection)),
            }

    def touch_user_login(self, *, user_id: int) -> dict[str, Any]:
        """Stamp a first successful login. Runs on the writer, only when owed.

        Re-checks the condition inside the write transaction: the read that
        decided a stamp was due happened in an earlier snapshot, so a concurrent
        login may already have taken it.
        """

        user_table = LiveUser.__table__
        now = datetime.now(UTC)
        with _write_transaction(self.engine) as connection:
            updated = connection.execute(
                update(user_table)
                .where(user_table.c.id == user_id, user_table.c.last_login.is_(None))
                .values(last_login=now, updated_at=now)
            ).rowcount
            if not updated:
                return {"changed": False, "commit_seq": str(_current_commit_seq(connection))}
            commit_seq = _advance_commit_seq(connection, now)
            row = connection.execute(select(user_table).where(user_table.c.id == user_id)).mappings().one()
            return {"changed": True, "user": _user_dto(row), "commit_seq": str(commit_seq)}

    def get_active_owner(self) -> dict[str, Any]:
        """Resolve the single-tenant owner without exposing a SQLite reader.

        The single-row assumption is load-bearing: with more than one active
        non-service user this silently promotes the lowest id to owner. Only an
        auth-disabled host reaches it today. Anything authenticated must carry
        the owner on its credential instead of asking here.
        """

        user_table = LiveUser.__table__
        with _read_snapshot(self.engine) as connection:
            owner_id = connection.execute(
                select(user_table.c.id)
                .where(
                    user_table.c.is_active.is_(True),
                    or_(user_table.c.provider != "service", user_table.c.provider.is_(None)),
                )
                .order_by(user_table.c.id.asc())
                .limit(1)
            ).scalar_one_or_none()
            return {
                "found": owner_id is not None,
                "owner_id": int(owner_id) if owner_id is not None else None,
                "commit_seq": str(_current_commit_seq(connection)),
            }

    def ensure_single_tenant_owner(
        self,
        *,
        email: str,
        provider: str,
        provider_user_id: str | None,
    ) -> dict[str, Any]:
        user_table = LiveUser.__table__
        now = datetime.now(UTC)
        with _write_transaction(self.engine) as connection:
            owners = (
                connection.execute(
                    select(user_table)
                    .where(or_(user_table.c.provider != "service", user_table.c.provider.is_(None)))
                    .order_by(user_table.c.id.asc())
                    .limit(2)
                )
                .mappings()
                .all()
            )
            if len(owners) > 1:
                return {"conflict": "multiple_owners", "commit_seq": str(_current_commit_seq(connection))}
            if owners:
                owner = owners[0]
                if str(owner["email"]).casefold() != email.casefold():
                    if not _is_oss_trial_owner(owner):
                        return {"conflict": "owner_email_mismatch", "commit_seq": str(_current_commit_seq(connection))}
                    # The auth-disabled trial owner is a placeholder with no credential. Turning
                    # auth on for the same database (the README's trial-to-self-host step) makes it
                    # the configured owner in place: sessions belong to the user id, so history
                    # follows. Without this the server refused to start on the trial's own data.
                    connection.execute(
                        update(user_table)
                        .where(user_table.c.id == owner["id"])
                        .values(
                            email=email,
                            provider=provider,
                            provider_user_id=provider_user_id,
                            role="ADMIN",
                            updated_at=now,
                        )
                    )
                    commit_seq = _advance_commit_seq(connection, now)
                    owner = connection.execute(select(user_table).where(user_table.c.id == owner["id"])).mappings().one()
                    return {
                        "created": False,
                        "user": _user_dto(owner),
                        "commit_seq": str(commit_seq),
                    }
                if owner["role"] != "ADMIN":
                    connection.execute(update(user_table).where(user_table.c.id == owner["id"]).values(role="ADMIN", updated_at=now))
                    commit_seq = _advance_commit_seq(connection, now)
                    owner = connection.execute(select(user_table).where(user_table.c.id == owner["id"])).mappings().one()
                else:
                    commit_seq = _current_commit_seq(connection)
                return {
                    "created": False,
                    "user": _user_dto(owner),
                    "commit_seq": str(commit_seq),
                }
            user_id = connection.execute(
                insert(user_table)
                .values(
                    provider=provider,
                    provider_user_id=provider_user_id,
                    email=email,
                    email_verified=True,
                    is_active=True,
                    role="ADMIN",
                    prefs={},
                    context={},
                    created_at=now,
                    updated_at=now,
                )
                .returning(user_table.c.id)
            ).scalar_one()
            commit_seq = _advance_commit_seq(connection, now)
            owner = connection.execute(select(user_table).where(user_table.c.id == user_id)).mappings().one()
            return {
                "created": True,
                "user": _user_dto(owner),
                "commit_seq": str(commit_seq),
            }

    def upsert_notification_presence(
        self,
        *,
        owner_id: int,
        client_id: str,
        client_type: str,
        visible: bool,
        route: str | None,
        session_id: str | None,
        observed_at: datetime,
    ) -> dict[str, Any]:
        table = LiveNotificationClientPresence.__table__
        with _write_transaction(self.engine) as connection:
            row = connection.execute(select(table).where(table.c.owner_id == owner_id, table.c.client_id == client_id)).mappings().first()
            if row is not None:
                durable_observed_at = _as_aware_utc(row["last_seen_at"]) or observed_at
                same_payload = (
                    str(row["client_type"]) == client_type
                    and bool(row["visible"]) == visible
                    and row["route"] == route
                    and row["session_id"] == session_id
                )
                if observed_at == durable_observed_at and not same_payload:
                    return {
                        "idempotency_conflict": True,
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                if observed_at <= durable_observed_at:
                    return {
                        "idempotency_conflict": False,
                        "stale": observed_at < durable_observed_at,
                        "presence": {
                            "client_id": str(row["client_id"]),
                            "client_type": str(row["client_type"]),
                            "visible": bool(row["visible"]),
                            "route": row["route"],
                            "session_id": row["session_id"],
                            "last_seen_at": durable_observed_at.isoformat(),
                        },
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
            values = {
                "client_type": client_type,
                "visible": visible,
                "route": route,
                "session_id": session_id,
                "last_seen_at": observed_at,
                "updated_at": observed_at,
            }
            if row is None:
                connection.execute(
                    insert(table).values(
                        owner_id=owner_id,
                        client_id=client_id,
                        created_at=observed_at,
                        **values,
                    )
                )
            else:
                connection.execute(update(table).where(table.c.owner_id == owner_id, table.c.client_id == client_id).values(**values))
            commit_seq = _advance_commit_seq(connection, observed_at)
            return {
                "idempotency_conflict": False,
                "stale": False,
                "presence": {
                    "client_id": client_id,
                    "client_type": client_type,
                    "visible": visible,
                    "route": route,
                    "session_id": session_id,
                    "last_seen_at": observed_at.isoformat(),
                },
                "commit_seq": str(commit_seq),
            }

    def recent_visible_web_presence(self, *, owner_id: int, threshold: datetime) -> dict[str, Any]:
        table = LiveNotificationClientPresence.__table__
        with _read_snapshot(self.engine) as connection:
            found = connection.execute(
                select(table.c.id)
                .where(
                    table.c.owner_id == owner_id,
                    table.c.client_type == "web",
                    table.c.visible.is_(True),
                    table.c.last_seen_at >= threshold,
                )
                .limit(1)
            ).first()
            return {
                "visible": found is not None,
                "commit_seq": str(_current_commit_seq(connection)),
            }

    def read_machine_presence_policy(self, *, owner_id: int) -> dict[str, Any]:
        user_table = LiveUser.__table__
        with _read_snapshot(self.engine) as connection:
            prefs = connection.execute(select(user_table.c.prefs).where(user_table.c.id == owner_id)).scalar_one_or_none()
            decoded = _decode_json_object(prefs) if prefs is not None else {}
            enabled = decoded.get("machine_presence_enabled")
            return {
                "found": prefs is not None,
                "enabled": enabled if isinstance(enabled, bool) else True,
                "commit_seq": str(_current_commit_seq(connection)),
            }

    def upsert_machine_presence(
        self,
        *,
        owner_id: int,
        device_id: str,
        state: str,
        source: str,
        idle_seconds: int | None,
        measured_at: datetime,
        received_at: datetime,
    ) -> dict[str, Any]:
        table = LiveMachinePresence.__table__
        values = {
            "state": state,
            "source": source,
            "idle_seconds": idle_seconds,
            "measured_at": measured_at,
            "received_at": received_at,
            "updated_at": received_at,
        }
        with _write_transaction(self.engine) as connection:
            row = connection.execute(select(table.c.id).where(table.c.owner_id == owner_id, table.c.device_id == device_id)).first()
            if row is None:
                connection.execute(
                    insert(table).values(
                        owner_id=owner_id,
                        device_id=device_id,
                        created_at=received_at,
                        **values,
                    )
                )
            else:
                connection.execute(update(table).where(table.c.owner_id == owner_id, table.c.device_id == device_id).values(**values))
            commit_seq = _advance_commit_seq(connection, received_at)
            return {
                "presence": {
                    "owner_id": owner_id,
                    "device_id": device_id,
                    "state": state,
                    "source": source,
                    "idle_seconds": idle_seconds,
                    "measured_at": measured_at.isoformat(),
                    "received_at": received_at.isoformat(),
                },
                "commit_seq": str(commit_seq),
            }

    def upsert_apns_device(
        self,
        *,
        registration_id: str,
        owner_id: int,
        platform: str,
        device_token: str,
        push_environment: str,
        app_build_id: str | None,
        observed_at: datetime,
    ) -> dict[str, Any]:
        table = LiveAPNSDeviceRegistration.__table__
        with _write_transaction(self.engine) as connection:
            row = (
                connection.execute(select(table).where(table.c.owner_id == owner_id, table.c.device_token == device_token))
                .mappings()
                .first()
            )
            stored_id = str(row["id"]) if row is not None else registration_id
            values = {
                "platform": platform,
                "push_environment": push_environment,
                "app_build_id": app_build_id,
                "last_seen_at": observed_at,
                "updated_at": observed_at,
                "revoked_at": None,
            }
            if row is None:
                connection.execute(
                    insert(table).values(
                        id=stored_id,
                        owner_id=owner_id,
                        device_token=device_token,
                        created_at=observed_at,
                        **values,
                    )
                )
            else:
                connection.execute(update(table).where(table.c.id == stored_id).values(**values))
            commit_seq = _advance_commit_seq(connection, observed_at)
            return {
                "registration": {
                    "id": stored_id,
                    "platform": platform,
                    "push_environment": push_environment,
                    "app_build_id": app_build_id,
                    "last_seen_at": observed_at.isoformat(),
                },
                "commit_seq": str(commit_seq),
            }

    def upsert_apns_live_activity(
        self,
        *,
        registration_id: str,
        owner_id: int,
        session_id: str,
        activity_id: str,
        push_token: str,
        push_environment: str,
        app_build_id: str | None,
        observed_at: datetime,
    ) -> dict[str, Any]:
        table = LiveAPNSLiveActivityRegistration.__table__
        with _write_transaction(self.engine) as connection:
            row = (
                connection.execute(select(table).where(table.c.owner_id == owner_id, table.c.activity_id == activity_id)).mappings().first()
            )
            if row is None:
                row = (
                    connection.execute(select(table).where(table.c.owner_id == owner_id, table.c.push_token == push_token))
                    .mappings()
                    .first()
                )
            stored_id = str(row["id"]) if row is not None else registration_id
            values = {
                "session_id": session_id,
                "activity_id": activity_id,
                "push_token": push_token,
                "push_environment": push_environment,
                "app_build_id": app_build_id,
                "last_seen_at": observed_at,
                "updated_at": observed_at,
                "ended_at": None,
            }
            if row is None:
                connection.execute(
                    insert(table).values(
                        id=stored_id,
                        owner_id=owner_id,
                        created_at=observed_at,
                        **values,
                    )
                )
            else:
                connection.execute(update(table).where(table.c.id == stored_id).values(**values))
            commit_seq = _advance_commit_seq(connection, observed_at)
            return {
                "registration": {
                    "id": stored_id,
                    "session_id": session_id,
                    "activity_id": activity_id,
                    "push_environment": push_environment,
                    "app_build_id": app_build_id,
                    "last_seen_at": observed_at.isoformat(),
                },
                "commit_seq": str(commit_seq),
            }

    def end_apns_live_activity(self, *, owner_id: int, activity_id: str, ended_at: datetime) -> dict[str, Any]:
        table = LiveAPNSLiveActivityRegistration.__table__
        with _write_transaction(self.engine) as connection:
            count = connection.execute(
                update(table)
                .where(table.c.owner_id == owner_id, table.c.activity_id == activity_id, table.c.ended_at.is_(None))
                .values(ended_at=ended_at, updated_at=ended_at)
            ).rowcount
            commit_seq = _advance_commit_seq(connection, ended_at) if count else _current_commit_seq(connection)
            return {"found": bool(count), "commit_seq": str(commit_seq)}

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
        # Read-only regardless of `touch_last_used`. The throttle means most
        # calls are not due, and opening a write transaction for them took
        # SQLite's write lock to write nothing. `touch_device_token` performs
        # the stamp when one is genuinely owed.
        with _read_snapshot(self.engine) as connection:
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
            touch_due = bool(touch_last_used and (last_used_at is None or (now - last_used_at).total_seconds() >= touch_interval_seconds))
            commit_seq = _current_commit_seq(connection)
            return {
                "valid": True,
                "changed": False,
                "touch_due": touch_due,
                "device_token_id": str(row["device_token_id"]),
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

    def touch_device_token(self, *, token_id: str, touch_interval_seconds: int) -> dict[str, Any]:
        """Stamp a device credential's last use. Runs on the writer, only when owed.

        Re-checks revocation and the throttle inside the write transaction. The
        read that decided a stamp was due ran in an earlier snapshot, so the
        credential may have been revoked or already stamped since.
        """

        token_table = LiveDeviceToken.__table__
        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=touch_interval_seconds)
        with _write_transaction(self.engine) as connection:
            updated = connection.execute(
                update(token_table)
                .where(
                    token_table.c.id == token_id,
                    token_table.c.revoked_at.is_(None),
                    or_(token_table.c.last_used_at.is_(None), token_table.c.last_used_at <= cutoff),
                )
                .values(last_used_at=now)
            ).rowcount
            if not updated:
                return {"changed": False, "commit_seq": str(_current_commit_seq(connection))}
            return {
                "changed": True,
                "last_used_at": _encode_datetime(now),
                "commit_seq": str(_advance_commit_seq(connection, now)),
            }

    def get_cp_user(
        self,
        *,
        cp_user_id: int,
        email: str,
        email_verified: bool,
        display_name: str | None,
        avatar_url: str | None,
    ) -> dict[str, Any]:
        """Read an established CP identity without taking the writer."""
        user_table = LiveUser.__table__
        with _read_snapshot(self.engine) as connection:
            user = connection.execute(select(user_table).where(user_table.c.cp_user_id == cp_user_id)).mappings().first()
            commit_seq = _current_commit_seq(connection)
            if user is None:
                return {"found": False, "sync_due": True, "commit_seq": str(commit_seq)}

            desired_display_name = display_name or user["display_name"]
            desired_avatar_url = avatar_url or user["avatar_url"]
            email_collision = connection.execute(
                select(user_table.c.id).where(user_table.c.email == email, user_table.c.id != user["id"])
            ).first()
            sync_due = any(
                (
                    user["provider"] != "control-plane",
                    user["provider_user_id"] != f"cp:{cp_user_id}",
                    user["email"] != email and email_collision is None,
                    user["display_name"] != desired_display_name,
                    user["avatar_url"] != desired_avatar_url,
                    user["email_verified"] != email_verified,
                    user["is_active"] is not True,
                    user["last_login"] is None,
                )
            )
            return {
                "found": True,
                "sync_due": sync_due,
                "user": _user_dto(user),
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
                elapsed = (now - _as_aware_utc(parent["used_at"])).total_seconds()
                if elapsed > reuse_grace_seconds:
                    count = connection.execute(
                        update(table).where(table.c.family_id == parent["family_id"], table.c.revoked_at.is_(None)).values(revoked_at=now)
                    ).rowcount
                    commit_seq = _advance_commit_seq(connection, now) if count else _current_commit_seq(connection)
                    return {
                        "status": "family_revoked",
                        "revoked_count": count,
                        "commit_seq": str(commit_seq),
                    }

                child = (
                    connection.execute(select(table).where(table.c.parent_id == parent["id"], table.c.revoked_at.is_(None)))
                    .mappings()
                    .first()
                )
                if child is None or not hmac.compare_digest(str(child["token_hash"]), next_token_hash):
                    return {"status": "invalid", "commit_seq": str(_current_commit_seq(connection))}

                if child["used_at"] is None:
                    return {
                        "status": "exact_replay",
                        "session_id": child["id"],
                        "user_id": parent["user_id"],
                        "family_id": parent["family_id"],
                        "user": _user_dto(user),
                        "commit_seq": str(_current_commit_seq(connection)),
                    }

                # A delayed request can arrive after two tabs have already
                # rotated again. Do not clear the browser's valid cookies for
                # that race. Return the current generation so the HTTP layer
                # can deterministically reconstruct it from the presented raw
                # token; a replay outside grace still revokes the family.
                current = child
                while current["used_at"] is not None:
                    child_elapsed = (now - _as_aware_utc(current["used_at"])).total_seconds()
                    if child_elapsed > reuse_grace_seconds:
                        count = connection.execute(
                            update(table)
                            .where(table.c.family_id == parent["family_id"], table.c.revoked_at.is_(None))
                            .values(revoked_at=now)
                        ).rowcount
                        commit_seq = _advance_commit_seq(connection, now) if count else _current_commit_seq(connection)
                        return {
                            "status": "family_revoked",
                            "revoked_count": count,
                            "commit_seq": str(commit_seq),
                        }
                    descendant = (
                        connection.execute(
                            select(table).where(
                                table.c.parent_id == current["id"],
                                table.c.revoked_at.is_(None),
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if descendant is None:
                        return {"status": "invalid", "commit_seq": str(_current_commit_seq(connection))}
                    current = descendant
                return {
                    "status": "stale_replay",
                    "session_id": current["id"],
                    "user_id": parent["user_id"],
                    "family_id": parent["family_id"],
                    "current_token_hash": current["token_hash"],
                    "user": _user_dto(user),
                    "commit_seq": str(_current_commit_seq(connection)),
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
                        "machine_name": row["machine_name"],
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

            active_token_count = connection.execute(
                select(func.count()).select_from(token_table).where(token_table.c.owner_id == owner_id, token_table.c.revoked_at.is_(None))
            ).scalar_one()
            if active_token_count >= DEVICE_TOKEN_LIMIT_PER_OWNER:
                return {
                    "created": False,
                    "exact_replay": False,
                    "limit_exceeded": True,
                    "commit_seq": str(_current_commit_seq(connection)),
                }

            total_token_count = connection.execute(
                select(func.count()).select_from(token_table).where(token_table.c.owner_id == owner_id)
            ).scalar_one()
            rows_to_prune = max(0, int(total_token_count) - DEVICE_TOKEN_LIMIT_PER_OWNER + 1)
            if rows_to_prune:
                revoked_ids = list(
                    connection.execute(
                        select(token_table.c.id)
                        .where(token_table.c.owner_id == owner_id, token_table.c.revoked_at.is_not(None))
                        .order_by(token_table.c.created_at, token_table.c.id)
                        .limit(rows_to_prune)
                    ).scalars()
                )
                if len(revoked_ids) != rows_to_prune:
                    return {
                        "created": False,
                        "exact_replay": False,
                        "limit_exceeded": True,
                        "commit_seq": str(_current_commit_seq(connection)),
                    }
                connection.execute(delete(token_table).where(token_table.c.id.in_(revoked_ids)))

            connection.execute(
                token_table.insert().values(
                    id=token_id,
                    owner_id=owner_id,
                    device_id=device_id,
                    machine_name=None,
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

    def rename_machine(self, *, owner_id: int, device_id: str, machine_name: str) -> dict[str, Any]:
        """Set one durable display name across active credentials for a machine."""

        token_table = LiveDeviceToken.__table__
        now = datetime.now(UTC)
        with _write_transaction(self.engine) as connection:
            rows = connection.execute(
                select(token_table.c.id, token_table.c.machine_name).where(
                    token_table.c.owner_id == owner_id,
                    token_table.c.device_id == device_id,
                    token_table.c.revoked_at.is_(None),
                )
            ).all()
            if not rows:
                return {"found": False, "changed": False, "commit_seq": str(_current_commit_seq(connection))}
            changed = any(row.machine_name != machine_name for row in rows)
            if changed:
                connection.execute(
                    update(token_table)
                    .where(
                        token_table.c.owner_id == owner_id,
                        token_table.c.device_id == device_id,
                        token_table.c.revoked_at.is_(None),
                    )
                    .values(machine_name=machine_name)
                )
                commit_seq = _advance_commit_seq(connection, now)
            else:
                commit_seq = _current_commit_seq(connection)
            return {
                "found": True,
                "changed": changed,
                "device_id": device_id,
                "machine_name": machine_name,
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
