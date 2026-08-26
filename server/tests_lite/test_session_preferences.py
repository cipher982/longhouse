"""Session preferences, read the way a Runtime Host reads them.

``load_session_preferences`` has three branches, and until the ``TESTING``
early return came out of ``live_catalog_enabled()`` this file only reached the
one production never takes: the SQLAlchemy live-store fallback. Two tests here
monkeypatched ``live_store_configured`` and ``get_live_session_factory`` onto a
hand-built SQLite file and then proved the loader read it -- true, and true of
no Runtime Host.

They now run against ``live_catalog_harness``: a real ``CatalogDaemon`` over a
real Unix socket holding real rows, answering the same ``session.read.v2`` RPC
the API process issues in production. Preferences are seeded through
``session.preferences.update.v2`` against a Console session, because that is
the shape whose bounded preference row lives in the catalog; a shadow
session's storage row accepts only the hidden and read-through fields.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from uuid import UUID
from uuid import uuid4

import pytest

from tests_lite.live_catalog_harness import LiveCatalog
from tests_lite.live_catalog_harness import provision_live_catalog
from zerg.services.session_preferences import load_session_preferences


@pytest.fixture()
def live():
    """A Runtime Host shaped the way production shapes one."""

    with provision_live_catalog() as catalog:
        yield catalog


def _stale_archive(*, user_state: str, loop_mode: str, notification_muted: bool):
    """An archive mirror row still carrying what the live catalog has moved past."""

    return type(
        "ArchiveSession",
        (),
        {
            "user_state": user_state,
            "loop_mode": loop_mode,
            "notification_muted": notification_muted,
        },
    )()


def _console_session(
    live: LiveCatalog,
    *,
    owner_id: int,
    user_state: str,
    loop_mode: str,
    notification_muted: bool,
    user_hidden_from_timeline: bool | None = None,
) -> UUID:
    """Commit one Console session to the catalog and set its preferences there."""

    session_id = uuid4()
    now = datetime.now(UTC)
    created = live.rpc(
        "session.console.create.v2",
        {
            "session": {
                "session_id": str(session_id),
                "thread_id": str(uuid4()),
                "owner_id": owner_id,
                "provider": "codex",
                "device_id": "cinder",
                "cwd": "/workspace/longhouse",
                "started_at": now.isoformat(),
            }
        },
    )
    assert created["created"] is True, created
    updated = live.rpc(
        "session.preferences.update.v2",
        {
            "session_id": str(session_id),
            "user_state": user_state,
            "loop_mode": loop_mode,
            "notification_muted": notification_muted,
            "user_hidden_from_timeline": user_hidden_from_timeline,
            "last_read_at": None,
            "observed_at": now.isoformat(),
        },
    )
    assert updated["found"] is True, updated
    return session_id


def test_live_catalog_is_authoritative_for_session_preferences(live):
    """Guard: the catalog decides, and a stale archive mirror never overrides it.

    The archive row is a lagging copy. If the loader ever preferred it, a
    session the user archived, muted or put on autopilot would read back with
    whatever the mirror last happened to hold.
    """

    owner = live.create_user("owner@preferences.test")
    session_id = _console_session(
        live,
        owner_id=owner,
        user_state="archived",
        loop_mode="autopilot",
        notification_muted=True,
    )
    stale_archive = _stale_archive(user_state="active", loop_mode="assist", notification_muted=False)

    preferences = load_session_preferences(session_id, standalone_session=stale_archive)

    assert preferences.user_state == "archived"
    assert preferences.loop_mode == "autopilot"
    assert preferences.notification_muted is True


def test_missing_live_row_uses_canonical_defaults_not_archive(live):
    """Guard: a session the catalog does not know reads as canonical defaults.

    Falling back to the archive row here would resurrect preferences the
    catalog has no record of -- an unknown session would arrive archived, muted
    and on autopilot because a stale mirror said so.
    """

    unknown_session_id = uuid4()
    assert live.rpc("session.read.v2", {"session_id": str(unknown_session_id)})["found"] is False
    stale_archive = _stale_archive(user_state="archived", loop_mode="autopilot", notification_muted=True)

    preferences = load_session_preferences(unknown_session_id, standalone_session=stale_archive)

    assert preferences.user_state == "active"
    assert preferences.loop_mode == "assist"
    assert preferences.notification_muted is False


def test_catalog_mode_reads_preferences_without_opening_sqlite(live, monkeypatch):
    """Guard: the API process asks catalogd instead of opening the live database.

    catalogd owns the live SQLite file; a second writer in the API process is
    how the single-writer path gets lost. The factory is poisoned so any
    attempt to open it here fails loudly rather than quietly working on a
    developer laptop.
    """

    owner = live.create_user("owner@preferences.test")
    session_id = _console_session(
        live,
        owner_id=owner,
        user_state="snoozed",
        loop_mode="autopilot",
        notification_muted=True,
        user_hidden_from_timeline=True,
    )
    monkeypatch.setattr(
        "zerg.database.get_live_session_factory",
        lambda: (_ for _ in ()).throw(AssertionError("API process must not open live SQLite")),
    )

    preferences = load_session_preferences(session_id)

    assert preferences.user_state == "snoozed"
    assert preferences.loop_mode == "autopilot"
    assert preferences.notification_muted is True
    assert preferences.user_hidden_from_timeline is True
