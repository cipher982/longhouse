"""Tests for the structured per-session loop mode endpoint.

Loop mode, timeline visibility and every other bounded session preference live
in the catalog now, so those run against a real one: catalogd owns the row and
the routes reach it over the same RPC a Runtime Host uses. ``/agents/sessions/
active`` is the exception -- it still hydrates its rows from the archive store
through ``get_db`` -- so its test keeps a real archive session of its own.
"""

from __future__ import annotations

import os
from datetime import UTC
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from uuid import UUID
from uuid import uuid4

from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")

os.environ.setdefault("TESTING", "1")

import pytest

from tests_lite.live_catalog_harness import LiveCatalog  # noqa: E402
from tests_lite.live_catalog_harness import live_catalog  # noqa: E402, F401
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: E402, F401
from zerg.database import Base  # noqa: E402
from zerg.database import get_db  # noqa: E402
from zerg.database import make_engine  # noqa: E402
from zerg.database import make_sessionmaker  # noqa: E402
from zerg.dependencies.agents_auth import verify_agents_token  # noqa: E402
from zerg.models.agents import AgentSession  # noqa: E402
from zerg.services.session_hot_cards import upsert_timeline_card_from_session  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_api_app_dependency_overrides():
    """An override installed here must not outlive this test.

    ``api_app`` is a process-global, so an override left behind keeps
    answering for every later test in the run. This file used to leave
    ``verify_agents_token`` returning device ``usage-stats``, and an unrelated
    storage-v2 test several hundred tests later failed with
    ``identity_mismatch``. Nothing catches that until an edit elsewhere
    reorders the suite, so each test puts back what it found.
    """

    from zerg.main import api_app

    saved = dict(api_app.dependency_overrides)
    try:
        yield
    finally:
        api_app.dependency_overrides.clear()
        api_app.dependency_overrides.update(saved)


def _console_session(live: LiveCatalog, *, owner_id: int, loop_mode: str | None = None) -> UUID:
    """Create one Console session and, optionally, set its loop mode."""

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
    if loop_mode is not None:
        updated = live.rpc(
            "session.preferences.update.v2",
            {
                "session_id": str(session_id),
                "owner_id": owner_id,
                "user_state": None,
                "loop_mode": loop_mode,
                "notification_muted": None,
                "user_hidden_from_timeline": None,
                "last_read_at": None,
                "observed_at": now.isoformat(),
            },
        )
        assert updated["found"] is True, updated
    return session_id


def _catalog_loop_mode(live: LiveCatalog, session_id: UUID) -> str:
    return str(live.rpc("session.read.v2", {"session_id": str(session_id)})["facts"]["catalog"]["loop_mode"])


def _make_db(tmp_path, name="loop_mode.db"):
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_session(factory, *, loop_mode="assist"):
    db = factory()
    session = AgentSession(
        provider="claude",
        environment="production",
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
        user_messages=2,
        assistant_messages=2,
        tool_calls=0,
        loop_mode=loop_mode,
    )
    db.add(session)
    db.flush()
    upsert_timeline_card_from_session(db, session)
    db.commit()
    db.refresh(session)
    session_id = str(session.id)
    db.close()
    return session_id


def _client(factory):
    from zerg.main import api_app


    def override():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    def override_verify_agents_token():
        return SimpleNamespace(device_id="session-loop-mode", id="token-1", owner_id=1)

    api_app.dependency_overrides[get_db] = override
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
    return TestClient(api_app)


def test_get_session_exposes_loop_mode(live_catalog, live_catalog_client):
    email = "loop-mode@test.local"
    owner_id = live_catalog.create_user(email)
    session_id = _console_session(live_catalog, owner_id=owner_id, loop_mode="autopilot")
    cookies = {"longhouse_session": live_catalog.browser_cookie(owner_id=owner_id, email=email)}

    # loop_mode is a product preference the browser renders, not part of
    # the archival projection the machine surface serves.
    response = live_catalog_client.get(f"/timeline/sessions/{session_id}", cookies=cookies)

    assert response.status_code == 200, response.text
    assert response.json()["loop_mode"] == "autopilot"


def test_patch_session_loop_mode_updates_value(live_catalog, live_catalog_client):
    owner_id = live_catalog.create_user("loop-mode-patch@test.local")
    token = live_catalog.create_device_token(owner_id=owner_id, device_id="cinder")
    session_id = _console_session(live_catalog, owner_id=owner_id)

    response = live_catalog_client.patch(
        f"/agents/sessions/{session_id}/loop-mode",
        json={"loop_mode": "autopilot"},
        headers={"X-Agents-Token": token},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"session_id": str(session_id), "loop_mode": "autopilot"}
    assert _catalog_loop_mode(live_catalog, session_id) == "autopilot"


def test_patch_session_timeline_visibility_hides_and_restores(live_catalog, live_catalog_client):
    owner_id = live_catalog.create_user("timeline-visibility@test.local")
    token = live_catalog.create_device_token(owner_id=owner_id, device_id="cinder")
    seeded = live_catalog.commit_session(owner_id=owner_id)
    session_id = str(seeded.session_id)
    headers = {"X-Agents-Token": token}

    hidden = live_catalog_client.patch(
        f"/agents/sessions/{session_id}/timeline-visibility",
        json={"hidden": True},
        headers=headers,
    )
    assert hidden.status_code == 200, hidden.text
    assert hidden.json() == {"session_id": session_id, "hidden": True}

    listing = live_catalog_client.get("/agents/sessions", headers=headers)
    assert listing.status_code == 200, listing.text
    assert session_id not in {item["id"] for item in listing.json()["sessions"]}

    restored = live_catalog_client.patch(
        f"/agents/sessions/{session_id}/timeline-visibility",
        json={"hidden": False},
        headers=headers,
    )
    assert restored.status_code == 200, restored.text
    assert restored.json() == {"session_id": session_id, "hidden": False}

    listing = live_catalog_client.get("/agents/sessions", headers=headers)
    assert session_id in {item["id"] for item in listing.json()["sessions"]}


def test_invalid_loop_mode_rejected(live_catalog, live_catalog_client):
    owner_id = live_catalog.create_user("loop-mode-invalid@test.local")
    token = live_catalog.create_device_token(owner_id=owner_id, device_id="cinder")
    session_id = _console_session(live_catalog, owner_id=owner_id)

    response = live_catalog_client.patch(
        f"/agents/sessions/{session_id}/loop-mode",
        json={"loop_mode": "wild-west"},
        headers={"X-Agents-Token": token},
    )

    assert response.status_code == 422, response.text
