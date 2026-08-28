"""Session-preference writes belong to one owner, on both surfaces.

Five preference routes exist twice: once on the machine surface
(``/agents/sessions/{id}/...``, authenticated by a device token) and once on the
browser surface (``/timeline/sessions/{id}/...``, authenticated by the
``longhouse_session`` cookie), the second delegating to the first. Both
authenticated their caller and then dropped the credential on the floor: the
service call carried no owner, ``session.preferences.update.v2`` refused to
accept one, and ``CatalogStore.update_session_preferences`` selected on
``session_id`` alone. An intruder's write landed on the owner's row and returned
the owner's own answer.

Each test below seeds owner A's Console session in a real catalog, drives all
five routes as owner B, and then checks the two things that matter: B is told
404 in the same words an unissued session id earns, and A's row still holds what
A left there. Owner A drives the same route afterwards so the 404 is proven to
be the ownership gate rather than a route that answers 404 for everyone.
"""

from __future__ import annotations

import os
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
from uuid import UUID
from uuid import uuid4

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from tests_lite.live_catalog_harness import LiveCatalog  # noqa: E402
from tests_lite.live_catalog_harness import live_catalog  # noqa: E402,F401
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: E402,F401

OWNER_A_EMAIL = "owner-a@preferences-scope.test"
OWNER_B_EMAIL = "owner-b@preferences-scope.test"

# read_through is bounded to what the client saw, so keep it in the past: this
# suite is asserting the owner predicate, never the max-write rule.
READ_THROUGH = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()

# (label, http method, path suffix, body). One entry per route under test; the
# browser twin shares the suffix and body and differs only in prefix and
# credential.
PREFERENCE_ROUTES: list[tuple[str, str, str, dict[str, Any]]] = [
    ("action", "post", "action", {"action": "park"}),
    ("read", "post", "read", {"read_through": READ_THROUGH}),
    ("loop-mode", "patch", "loop-mode", {"loop_mode": "autopilot"}),
    ("notification-watch", "patch", "notification-watch", {"notification_muted": True}),
    ("timeline-visibility", "patch", "timeline-visibility", {"hidden": True}),
]

# What owner A's freshly seeded session holds before anybody writes to it. A
# cross-owner write is only proven blocked if these are still true afterwards.
UNTOUCHED_CATALOG_STATE = {
    "user_state": "active",
    "loop_mode": "assist",
    "notification_muted": False,
    "user_hidden_from_timeline": False,
    "last_read_at": None,
}


@pytest.fixture(autouse=True)
def _restore_api_app_dependency_overrides():
    """``api_app`` is a process-global; an override here must not outlive the test."""

    from zerg.main import api_app

    saved = dict(api_app.dependency_overrides)
    try:
        yield
    finally:
        api_app.dependency_overrides.clear()
        api_app.dependency_overrides.update(saved)


def _console_session(live: LiveCatalog, *, owner_id: int) -> str:
    """One Console session bound to ``owner_id`` in the catalog that owns that fact."""

    session_id = uuid4()
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
                "project": "longhouse",
                "started_at": datetime.now(UTC).isoformat(),
            }
        },
    )
    assert created["created"] is True, created
    return str(session_id)


def _catalog_preferences(live: LiveCatalog, session_id: str) -> dict[str, Any]:
    facts = live.rpc("session.read.v2", {"session_id": session_id})
    assert facts["found"] is True, facts
    catalog = facts["facts"]["catalog"]
    return {key: catalog.get(key) for key in UNTOUCHED_CATALOG_STATE}


def _call(client, method: str, path: str, body: dict[str, Any], **kwargs):
    return getattr(client, method)(path, json=body, **kwargs)


@pytest.fixture()
def scoped(live_catalog: LiveCatalog):  # noqa: F811
    """Owner A with a session, owner B with credentials and no business here."""

    owner_a = live_catalog.create_user(OWNER_A_EMAIL)
    owner_b = live_catalog.create_user(OWNER_B_EMAIL)
    return {
        "session_id": _console_session(live_catalog, owner_id=owner_a),
        "token_a": live_catalog.create_device_token(owner_id=owner_a, device_id="cinder-a"),
        "token_b": live_catalog.create_device_token(owner_id=owner_b, device_id="cinder-b"),
        "cookies_a": {"longhouse_session": live_catalog.browser_cookie(owner_id=owner_a, email=OWNER_A_EMAIL)},
        "cookies_b": {"longhouse_session": live_catalog.browser_cookie(owner_id=owner_b, email=OWNER_B_EMAIL)},
    }


@pytest.mark.parametrize("label,method,suffix,body", PREFERENCE_ROUTES, ids=[route[0] for route in PREFERENCE_ROUTES])
def test_machine_surface_refuses_a_cross_owner_preference_write(
    live_catalog,  # noqa: F811
    live_catalog_client,  # noqa: F811
    scoped,
    label: str,
    method: str,
    suffix: str,
    body: dict[str, Any],
):
    """Guard: owner B's device token cannot write owner A's preferences.

    Before the owner predicate existed, B and A got byte-identical successful
    responses here and B's write landed on A's row.
    """

    session_id = scoped["session_id"]

    intruder = _call(
        live_catalog_client,
        method,
        f"/agents/sessions/{session_id}/{suffix}",
        body,
        headers={"X-Agents-Token": scoped["token_b"]},
    )
    unknown = _call(
        live_catalog_client,
        method,
        f"/agents/sessions/{uuid4()}/{suffix}",
        body,
        headers={"X-Agents-Token": scoped["token_b"]},
    )

    assert intruder.status_code == 404, intruder.text
    # Not merely "denied": denied in the same words a never-issued session id
    # earns, so the route is not an existence oracle for another owner's ids.
    assert (intruder.status_code, intruder.json()) == (unknown.status_code, unknown.json())
    assert _catalog_preferences(live_catalog, session_id) == UNTOUCHED_CATALOG_STATE

    owner = _call(
        live_catalog_client,
        method,
        f"/agents/sessions/{session_id}/{suffix}",
        body,
        headers={"X-Agents-Token": scoped["token_a"]},
    )
    # The 404 above has to be the ownership gate. A route that answers 404 for
    # its owner too would pass every assertion so far and prove nothing.
    assert owner.status_code == 200, owner.text
    assert _catalog_preferences(live_catalog, session_id) != UNTOUCHED_CATALOG_STATE


@pytest.mark.parametrize("label,method,suffix,body", PREFERENCE_ROUTES, ids=[route[0] for route in PREFERENCE_ROUTES])
def test_browser_surface_refuses_a_cross_owner_preference_write(
    live_catalog,  # noqa: F811
    live_catalog_client,  # noqa: F811
    scoped,
    label: str,
    method: str,
    suffix: str,
    body: dict[str, Any],
):
    """Guard: the same five routes on the browser surface, same boundary.

    The twins delegate to the machine handlers and used to pass ``_auth=None``,
    so the router-level cookie check authenticated the caller and nothing ever
    compared them to the session's owner.
    """

    session_id = scoped["session_id"]

    intruder = _call(
        live_catalog_client,
        method,
        f"/timeline/sessions/{session_id}/{suffix}",
        body,
        cookies=scoped["cookies_b"],
    )
    unknown = _call(
        live_catalog_client,
        method,
        f"/timeline/sessions/{uuid4()}/{suffix}",
        body,
        cookies=scoped["cookies_b"],
    )

    assert intruder.status_code == 404, intruder.text
    assert (intruder.status_code, intruder.json()) == (unknown.status_code, unknown.json())
    assert _catalog_preferences(live_catalog, session_id) == UNTOUCHED_CATALOG_STATE

    owner = _call(
        live_catalog_client,
        method,
        f"/timeline/sessions/{session_id}/{suffix}",
        body,
        cookies=scoped["cookies_a"],
    )
    assert owner.status_code == 200, owner.text
    assert _catalog_preferences(live_catalog, session_id) != UNTOUCHED_CATALOG_STATE


def test_catalog_rpc_refuses_a_preferences_write_with_no_owner(live_catalog):  # noqa: F811
    """Guard: the RPC contract itself requires the owner, so no caller can omit it.

    The router is not the only door onto ``session.preferences.update.v2``. If
    the daemon still accepted the old parameter set, any future caller would
    reintroduce the unscoped write without touching a route.
    """

    owner = live_catalog.create_user(OWNER_A_EMAIL)
    session_id = _console_session(live_catalog, owner_id=owner)
    params = {
        "session_id": session_id,
        "user_state": "parked",
        "loop_mode": None,
        "notification_muted": None,
        "user_hidden_from_timeline": None,
        "last_read_at": None,
        "observed_at": datetime.now(UTC).isoformat(),
    }

    with pytest.raises(Exception) as unowned:
        live_catalog.rpc("session.preferences.update.v2", params)
    assert "invalid parameters" in str(unowned.value)

    stranger = live_catalog.rpc("session.preferences.update.v2", {**params, "owner_id": owner + 1_000})
    assert stranger["found"] is False, stranger
    assert _catalog_preferences(live_catalog, session_id) == UNTOUCHED_CATALOG_STATE

    accepted = live_catalog.rpc("session.preferences.update.v2", {**params, "owner_id": owner})
    assert accepted["found"] is True, accepted
    assert accepted["preferences"]["user_state"] == "parked"


def test_preferences_load_never_reads_the_catalog_without_an_owner(live_catalog):  # noqa: F811
    """Guard: the read twin of the same write is owner-scoped or it is nothing.

    ``load_session_preferences`` used to reach ``session.read.v2`` with no owner,
    which is the one call shape ``read_session`` lets past its ownership check.
    Without an owner it must now answer canonical defaults, never the row.
    """

    from zerg.services.session_preferences import load_session_preferences

    owner = live_catalog.create_user(OWNER_A_EMAIL)
    stranger = live_catalog.create_user(OWNER_B_EMAIL)
    session_id = _console_session(live_catalog, owner_id=owner)
    live_catalog.rpc(
        "session.preferences.update.v2",
        {
            "session_id": session_id,
            "owner_id": owner,
            "user_state": "archived",
            "loop_mode": "autopilot",
            "notification_muted": True,
            "user_hidden_from_timeline": None,
            "last_read_at": None,
            "observed_at": datetime.now(UTC).isoformat(),
        },
    )

    assert load_session_preferences(UUID(session_id), owner_id=owner).user_state == "archived"
    assert load_session_preferences(UUID(session_id), owner_id=stranger).user_state == "active"
    assert load_session_preferences(UUID(session_id), owner_id=None).user_state == "active"
