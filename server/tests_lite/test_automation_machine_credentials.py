"""Automation machine credentials.

Spec: control-plane docs/specs/automation-machine-credentials.md. A credential
marked automation fills an absent ``launch_actor`` on what it ships, and marking
it backfills that machine's history; recorded provenance is never overwritten.
"""

from __future__ import annotations

import os
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from tests_lite.live_catalog_harness import LiveCatalog  # noqa: E402
from tests_lite.live_catalog_harness import live_catalog  # noqa: E402,F401
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: E402,F401
from zerg.routers.agents_storage_v2 import _credential_is_automation  # noqa: E402

SAURON = "clifford-sauron"
_GENERATIONS: dict[str, str] = {}
HUMAN = "cinder"


def _ship(live: LiveCatalog, client, *, token: str, device_id: str, launch_actor: str | None = None, session_id: str | None = None) -> str:
    session_id = session_id or uuid4()
    body = live.envelope_body(session_id=session_id, device_id=device_id, texts=("server selfcheck reports unhealthy",))
    # A later envelope for the same session continues its render generation.
    body["render"]["generation_id"] = _GENERATIONS.setdefault(str(session_id), body["render"]["generation_id"])
    body["session"]["launch_actor"] = launch_actor
    response = client.post(
        "/agents/storage/v2/envelopes",
        json=body,
        headers={"X-Agents-Token": token, "X-Longhouse-Storage-Lane": "live"},
    )
    assert response.status_code == 200, response.text
    return str(session_id)


def _catalog(live: LiveCatalog, session_id: str) -> dict:
    read = live.rpc("session.read.v2", {"session_id": session_id})
    assert read["found"] is True, read
    return read["facts"]["catalog"]


def _set(live: LiveCatalog, *, owner_id: int, device_id: str, automation: bool) -> dict:
    return live.rpc(
        "catalogd.device.automation.set.v2",
        {"owner_id": owner_id, "device_id": device_id, "automation": automation, "observed_at": datetime.now(UTC).isoformat()},
    )


def test_marking_a_machine_hides_its_history_and_what_it_ships_next(live_catalog, live_catalog_client):  # noqa: F811
    owner = live_catalog.create_user("owner@automation-creds.test")
    sauron_token = live_catalog.create_device_token(owner_id=owner, device_id=SAURON)
    human_token = live_catalog.create_device_token(owner_id=owner, device_id=HUMAN)

    before = _ship(live_catalog, live_catalog_client, token=sauron_token, device_id=SAURON)
    declared = _ship(live_catalog, live_catalog_client, token=sauron_token, device_id=SAURON, launch_actor="human_shell")
    human = _ship(live_catalog, live_catalog_client, token=human_token, device_id=HUMAN)
    assert _catalog(live_catalog, before)["hidden_from_default_timeline"] in (0, False)

    result = _set(live_catalog, owner_id=owner, device_id=SAURON, automation=True)
    assert result["found"] is True and result["tokens_updated"] == 1
    assert result["reclassified"] == [before]
    assert [row["session_id"] for row in result["sessions"]] == [before]

    backfilled = _catalog(live_catalog, before)
    assert backfilled["launch_actor"] == "automation"
    assert bool(backfilled["hidden_from_default_timeline"]) is True
    # Recorded provenance is never overwritten; the human machine is untouched.
    assert _catalog(live_catalog, declared)["launch_actor"] == "human_shell"
    assert bool(_catalog(live_catalog, human)["hidden_from_default_timeline"]) is False

    after = _ship(live_catalog, live_catalog_client, token=sauron_token, device_id=SAURON)
    shipped = _catalog(live_catalog, after)
    assert shipped["launch_actor"] == "automation"
    assert bool(shipped["hidden_from_default_timeline"]) is True

    # Idempotent, and a rerun re-mirrors every automation row to searchd.
    rerun = _set(live_catalog, owner_id=owner, device_id=SAURON, automation=True)
    assert rerun["reclassified"] == []
    assert sorted(row["session_id"] for row in rerun["sessions"]) == sorted([before, after])

    # An envelope with no actor never clears a recorded one, from any credential.
    _ship(live_catalog, live_catalog_client, token=sauron_token, device_id=SAURON, session_id=declared)
    assert _catalog(live_catalog, declared)["launch_actor"] == "human_shell"

    # Off clears the flag only; history stays hidden, new sessions ship visible.
    off = _set(live_catalog, owner_id=owner, device_id=SAURON, automation=False)
    assert off["found"] is True and off["sessions"] == []
    assert bool(_catalog(live_catalog, before)["hidden_from_default_timeline"]) is True
    later = _ship(live_catalog, live_catalog_client, token=sauron_token, device_id=SAURON)
    assert _catalog(live_catalog, later)["launch_actor"] is None
    # A backfilled session that keeps shipping after the flag is off stays hidden.
    _ship(live_catalog, live_catalog_client, token=sauron_token, device_id=SAURON, session_id=before)
    still = _catalog(live_catalog, before)
    assert still["launch_actor"] == "automation" and bool(still["hidden_from_default_timeline"]) is True


def test_marking_is_scoped_to_the_owner(live_catalog, live_catalog_client):  # noqa: F811
    owner = live_catalog.create_user("owner@automation-creds.test")
    stranger = live_catalog.create_user("stranger@automation-creds.test")
    token = live_catalog.create_device_token(owner_id=owner, device_id=SAURON)
    shipped = _ship(live_catalog, live_catalog_client, token=token, device_id=SAURON)

    assert _set(live_catalog, owner_id=stranger, device_id=SAURON, automation=True)["found"] is False
    assert _catalog(live_catalog, shipped)["launch_actor"] is None


def test_only_a_marked_credential_reports_automation():
    assert _credential_is_automation(SimpleNamespace(automation=True)) is True
    assert _credential_is_automation(SimpleNamespace(automation=False)) is False
    assert _credential_is_automation(None) is False


def test_set_automation_machine_mirrors_reclassified_rows_to_searchd(monkeypatch):
    from zerg.catalogd import client as catalog_client
    from zerg.services import catalogd_supervisor
    from zerg.services import searchd_supervisor
    from zerg.services.agents.automation_backfill import set_automation_machine

    calls: list[tuple[str, dict]] = []

    def fake_call(socket, method, *, params, timeout_seconds):
        calls.append((method, params))
        if method == "auth.owner.get.v2":
            return {"found": True, "owner_id": 4}
        if method == "catalogd.device.automation.set.v2":
            return {
                "found": True,
                "tokens_updated": 1,
                "commit_seq": "9",
                "reclassified": ["s1"],
                "sessions": [{"session_id": "s1", "user_hidden_from_timeline": False, "user_state": "active"}],
            }
        return {}

    monkeypatch.setattr(catalog_client, "call_catalogd_sync", fake_call)
    monkeypatch.setattr(catalogd_supervisor, "catalogd_paths", lambda: (None, "catalogd.sock"))
    monkeypatch.setattr(searchd_supervisor, "searchd_paths", lambda: (None, "searchd.sock"))

    result = set_automation_machine(SAURON, automation=True)

    assert result["owner_id"] == 4 and result["sessions_reclassified"] == 1 and result["sessions_mirrored"] == 1
    assert result["searchd_failures"] == []
    assert calls[1][1]["owner_id"] == 4 and calls[1][1]["device_id"] == SAURON
    assert calls[2] == (
        "search.session.reconcile_visibility.v2",
        {
            "session_id": "s1",
            "system_hidden": True,
            "test_scope_visible": False,
            "user_hidden_from_timeline": False,
            "user_state": "active",
            "source_commit_seq": 9,
        },
    )


def test_live_only_history_is_backfilled_and_live_rows_take_the_credential(live_catalog, live_catalog_client):  # noqa: F811
    owner = live_catalog.create_user("owner@automation-creds.test")
    token = live_catalog.create_device_token(owner_id=owner, device_id=SAURON)

    def seed_live_row(session_id: str) -> None:
        """A session the live catalog knows before any transcript is archived."""

        from zerg.catalogd.schema import create_catalog_engine
        from zerg.models.live_store import LiveSession
        from zerg.models.live_store import LiveSessionCatalog
        from zerg.services.catalogd_supervisor import catalogd_paths

        now = datetime.now(UTC).replace(microsecond=0)
        engine = create_catalog_engine(catalogd_paths()[0])
        try:
            with engine.begin() as connection:
                connection.execute(
                    LiveSession.__table__.insert().values(
                        session_id=session_id, owner_id=str(owner), provider="opencode", started_at=now, last_seen_at=now, updated_at=now
                    )
                )
                connection.execute(
                    LiveSessionCatalog.__table__.insert().values(
                        session_id=session_id,
                        provider="opencode",
                        environment="production",
                        device_id=SAURON,
                        started_at=now,
                        user_state="active",
                        notification_muted=0,
                    )
                )
        finally:
            engine.dispose()

    live_only = str(uuid4())
    seed_live_row(live_only)

    result = _set(live_catalog, owner_id=owner, device_id=SAURON, automation=True)
    assert live_only in result["reclassified"]
    backfilled = _catalog(live_catalog, live_only)
    assert backfilled["launch_actor"] == "automation"
    assert bool(backfilled["hidden_from_default_timeline"]) is True

    # A session whose live row exists before its first ship takes the
    # credential on both the archived and the live row.
    both = str(uuid4())
    seed_live_row(both)
    _ship(live_catalog, live_catalog_client, token=token, device_id=SAURON, session_id=both)
    shipped = _catalog(live_catalog, both)
    assert shipped["launch_actor"] == "automation"
    assert bool(shipped["hidden_from_default_timeline"]) is True
    from sqlalchemy import select

    from zerg.catalogd.schema import create_catalog_engine
    from zerg.models.live_store import LiveSessionCatalog
    from zerg.services.catalogd_supervisor import catalogd_paths

    engine = create_catalog_engine(catalogd_paths()[0])
    try:
        with engine.connect() as connection:
            live_actor = connection.execute(
                select(LiveSessionCatalog.__table__.c.launch_actor).where(LiveSessionCatalog.__table__.c.session_id == both)
            ).scalar_one()
    finally:
        engine.dispose()
    assert live_actor == "automation"


def test_a_live_only_recorded_actor_is_sticky(live_catalog, live_catalog_client):  # noqa: F811
    """A live row's actor (no launch surface, no archived row) is recorded provenance."""

    from zerg.catalogd.schema import create_catalog_engine
    from zerg.models.live_store import LiveSession
    from zerg.models.live_store import LiveSessionCatalog
    from zerg.services.catalogd_supervisor import catalogd_paths

    owner = live_catalog.create_user("owner@automation-creds.test")
    token = live_catalog.create_device_token(owner_id=owner, device_id=SAURON)
    now = datetime.now(UTC).replace(microsecond=0)
    human, backfilled = str(uuid4()), str(uuid4())
    engine = create_catalog_engine(catalogd_paths()[0])
    try:
        with engine.begin() as connection:
            for session_id, actor in ((human, "human_shell"), (backfilled, None)):
                connection.execute(
                    LiveSession.__table__.insert().values(
                        session_id=session_id, owner_id=str(owner), provider="opencode", started_at=now, last_seen_at=now, updated_at=now
                    )
                )
                connection.execute(
                    LiveSessionCatalog.__table__.insert().values(
                        session_id=session_id,
                        provider="opencode",
                        environment="production",
                        device_id=SAURON,
                        started_at=now,
                        user_state="active",
                        notification_muted=0,
                        launch_actor=actor,
                    )
                )
    finally:
        engine.dispose()

    assert _set(live_catalog, owner_id=owner, device_id=SAURON, automation=True)["reclassified"] == [backfilled]
    _ship(live_catalog, live_catalog_client, token=token, device_id=SAURON, session_id=human)
    assert _catalog(live_catalog, human)["launch_actor"] == "human_shell"

    _set(live_catalog, owner_id=owner, device_id=SAURON, automation=False)
    _ship(live_catalog, live_catalog_client, token=token, device_id=SAURON, session_id=backfilled)
    after_off = _catalog(live_catalog, backfilled)
    assert after_off["launch_actor"] == "automation"
    assert bool(after_off["hidden_from_default_timeline"]) is True


def test_a_restored_archived_actor_reaches_actorless_live_rows(live_catalog, live_catalog_client):  # noqa: F811
    from sqlalchemy import delete
    from sqlalchemy import select

    from zerg.catalogd.schema import create_catalog_engine
    from zerg.models.live_store import LiveSessionCatalog
    from zerg.services.catalogd_supervisor import catalogd_paths

    owner = live_catalog.create_user("owner@automation-creds.test")
    token = live_catalog.create_device_token(owner_id=owner, device_id=SAURON)
    session_id = _ship(live_catalog, live_catalog_client, token=token, device_id=SAURON)
    _set(live_catalog, owner_id=owner, device_id=SAURON, automation=True)
    _set(live_catalog, owner_id=owner, device_id=SAURON, automation=False)

    table = LiveSessionCatalog.__table__
    now = datetime.now(UTC).replace(microsecond=0)
    engine = create_catalog_engine(catalogd_paths()[0])
    try:
        with engine.begin() as connection:
            # The archived row says automation; the live row has no actor.
            connection.execute(delete(table).where(table.c.session_id == session_id))
            connection.execute(
                table.insert().values(
                    session_id=session_id,
                    provider="codex",
                    environment="production",
                    device_id=SAURON,
                    started_at=now,
                    user_state="active",
                    notification_muted=0,
                    launch_actor=None,
                )
            )
        _ship(live_catalog, live_catalog_client, token=token, device_id=SAURON, session_id=session_id)
        with engine.connect() as connection:
            live_actor = connection.execute(select(table.c.launch_actor).where(table.c.session_id == session_id)).scalar_one()
    finally:
        engine.dispose()
    assert live_actor == "automation"
    assert bool(_catalog(live_catalog, session_id)["hidden_from_default_timeline"]) is True
