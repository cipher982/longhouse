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
from zerg.routers.agents_storage_v2 import _apply_credential_provenance  # noqa: E402

SAURON = "clifford-sauron"
HUMAN = "cinder"


def _ship(live: LiveCatalog, client, *, token: str, device_id: str, launch_actor: str | None = None) -> str:
    session_id = uuid4()
    body = live.envelope_body(session_id=session_id, device_id=device_id, texts=("server selfcheck reports unhealthy",))
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

    # Idempotent: nothing left to reclassify.
    assert _set(live_catalog, owner_id=owner, device_id=SAURON, automation=True)["sessions"] == []

    # Off clears the flag only; history stays hidden, new sessions ship visible.
    off = _set(live_catalog, owner_id=owner, device_id=SAURON, automation=False)
    assert off["found"] is True and off["sessions"] == []
    assert bool(_catalog(live_catalog, before)["hidden_from_default_timeline"]) is True
    later = _ship(live_catalog, live_catalog_client, token=sauron_token, device_id=SAURON)
    assert _catalog(live_catalog, later)["launch_actor"] is None


def test_marking_is_scoped_to_the_owner(live_catalog, live_catalog_client):  # noqa: F811
    owner = live_catalog.create_user("owner@automation-creds.test")
    stranger = live_catalog.create_user("stranger@automation-creds.test")
    token = live_catalog.create_device_token(owner_id=owner, device_id=SAURON)
    shipped = _ship(live_catalog, live_catalog_client, token=token, device_id=SAURON)

    assert _set(live_catalog, owner_id=stranger, device_id=SAURON, automation=True)["found"] is False
    assert _catalog(live_catalog, shipped)["launch_actor"] is None


def test_credential_fills_only_absent_provenance():
    automation = SimpleNamespace(automation=True)
    facts = {"launch_actor": None, "hidden_from_default_timeline": False}
    _apply_credential_provenance(facts, automation)
    assert facts == {"launch_actor": "automation", "hidden_from_default_timeline": True}

    declared = {"launch_actor": "human_shell", "hidden_from_default_timeline": False}
    _apply_credential_provenance(declared, automation)
    assert declared == {"launch_actor": "human_shell", "hidden_from_default_timeline": False}

    human = {"launch_actor": None, "hidden_from_default_timeline": False}
    _apply_credential_provenance(human, SimpleNamespace(automation=False))
    assert human == {"launch_actor": None, "hidden_from_default_timeline": False}


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
                "sessions": [{"session_id": "s1", "user_hidden_from_timeline": False, "user_state": "active"}],
            }
        return {}

    monkeypatch.setattr(catalog_client, "call_catalogd_sync", fake_call)
    monkeypatch.setattr(catalogd_supervisor, "catalogd_paths", lambda: (None, "catalogd.sock"))
    monkeypatch.setattr(searchd_supervisor, "searchd_paths", lambda: (None, "searchd.sock"))

    result = set_automation_machine(SAURON, automation=True)

    assert result["owner_id"] == 4 and result["sessions_reclassified"] == 1 and result["searchd_failures"] == []
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
