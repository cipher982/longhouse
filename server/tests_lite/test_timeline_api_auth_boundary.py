from __future__ import annotations

import os
import time
from datetime import datetime
from datetime import timezone
from unittest.mock import patch
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session as SASession

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-1234")
os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret-1234")
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-google-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-google-client-secret")

import zerg.dependencies.agents_auth as agents_auth_deps
import zerg.dependencies.auth as auth_deps
from tests_lite.live_catalog_harness import LiveCatalog
from tests_lite.live_catalog_harness import live_catalog  # noqa: F401
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: F401
from zerg.auth.session_tokens import SESSION_COOKIE_NAME
from zerg.auth.session_tokens import SESSION_TOKEN_KIND
from zerg.auth.session_tokens import _encode_jwt
from zerg.catalogd.schema import create_catalog_engine
from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.main import api_app
from zerg.models import User
from zerg.models.agents import AgentSession
from zerg.models.live_store import LiveSessionCatalog
from zerg.services.catalogd_supervisor import catalogd_paths
from zerg.services.session_hot_cards import upsert_timeline_card_from_session

OWNER_EMAIL = "owner@example.com"
# Enough sessions that a clamped page is provably shorter than the corpus.
_OVER_CAP_SESSIONS = 105


@pytest.fixture(autouse=True)
def _restore_api_app_dependency_overrides():
    """An override installed here must not outlive this test.

    ``api_app`` is a process-global, so an override left behind keeps answering
    for every later test in the run. ``_make_client`` below clears the whole map
    and installs its own, which is exactly the shape that silently re-points
    authentication for hundreds of unrelated tests, so each test puts back what
    it found.
    """

    saved = dict(api_app.dependency_overrides)
    try:
        yield
    finally:
        api_app.dependency_overrides.clear()
        api_app.dependency_overrides.update(saved)


def _set_browser_cookie(client: TestClient, catalog: LiveCatalog, *, owner_id: int) -> None:
    client.cookies.set(SESSION_COOKIE_NAME, catalog.browser_cookie(owner_id=owner_id, email=OWNER_EMAIL))


def _seed_catalog_session(*, device_id: str, cwd: str, git_repo: str | None) -> None:
    """Leave one session row in the live catalog, with facts of this test's choosing.

    ``LiveCatalog.commit_session`` derives cwd and git facts from the project;
    the workspace picker ranks exactly those, so this seeds the row directly and
    leaves the enrollment, scoping and ranking to catalogd.
    """

    database_path, _socket_path = catalogd_paths()
    engine = create_catalog_engine(database_path)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        with SASession(engine) as db:
            db.add(
                LiveSessionCatalog(
                    session_id=str(uuid4()),
                    provider="claude",
                    environment="development",
                    project="timeline-auth",
                    device_id=device_id,
                    cwd=cwd,
                    git_repo=git_repo,
                    git_branch="main",
                    launch_actor="human_shell",
                    launch_surface="terminal",
                    started_at=now,
                    last_activity_at=now,
                    user_messages=1,
                    assistant_messages=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            db.commit()
    finally:
        engine.dispose()


def _make_db(tmp_path):
    db_path = tmp_path / "test_timeline_api_auth_boundary.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _seed_user(db, *, user_id: int = 1) -> User:
    user = User(id=user_id, email="owner@example.com", role="ADMIN")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_session(db) -> str:
    session = AgentSession(
        id=uuid4(),
        provider="claude",
        environment="development",
        project="timeline-auth",
        device_id="dev-machine",
        cwd="/tmp/timeline-auth",
        git_repo=None,
        git_branch="main",
        launch_actor="human_shell",
        launch_surface="terminal",
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
        user_messages=1,
        assistant_messages=1,
        tool_calls=0,
    )
    db.add(session)
    db.flush()
    upsert_timeline_card_from_session(db, session)
    db.commit()
    return str(session.id)


def _issue_session_cookie(user_id: int = 1) -> str:
    return _encode_jwt(
        {
            "sub": str(user_id),
            "typ": SESSION_TOKEN_KIND,
            "exp": int(time.time()) + 300,
        },
        auth_deps.get_settings().jwt_secret,
    )


def _make_client(session_local) -> TestClient:
    api_app.dependency_overrides.clear()

    def override_db():
        with session_local() as db:
            yield db

    api_app.dependency_overrides[get_db] = override_db
    api_app.dependency_overrides[require_single_tenant] = lambda: None
    return TestClient(api_app)


def _force_browser_jwt_mode():
    auth_deps._strategy_cache.clear()
    return patch.object(auth_deps, "AUTH_DISABLED", False)


def _force_agents_token_mode():
    return patch.object(
        agents_auth_deps,
        "get_settings",
        return_value=type("S", (), {"auth_disabled": False})(),
    )


def test_timeline_sessions_accept_browser_session_cookie(live_catalog, live_catalog_client):  # noqa: F811
    owner = live_catalog.create_user(OWNER_EMAIL)
    seeded = live_catalog.commit_session(owner_id=owner, device_id="dev-machine", project="timeline-auth")
    _set_browser_cookie(live_catalog_client, live_catalog, owner_id=owner)

    response = live_catalog_client.get("/timeline/sessions")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total"] == 1
    assert payload["sessions"][0]["thread_id"] == str(seeded.session_id)
    assert payload["sessions"][0]["head"]["project"] == "timeline-auth"
    assert payload["sessions"][0]["detail"]["project"] == "timeline-auth"
    assert "catalog_list;dur=" in response.headers["server-timing"]


def test_timeline_session_events_anchor_tail_accepts_browser_session_cookie(live_catalog, live_catalog_client):  # noqa: F811
    owner = live_catalog.create_user(OWNER_EMAIL)
    seeded = live_catalog.commit_session(
        owner_id=owner,
        device_id="cinder",
        project="timeline-auth",
        texts=tuple(f"event {idx}" for idx in range(1, 6)),
    )
    _set_browser_cookie(live_catalog_client, live_catalog, owner_id=owner)

    response = live_catalog_client.get(
        f"/timeline/sessions/{seeded.session_id}/events",
        params={"limit": 2, "anchor": "tail", "branch_mode": "head"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total"] == 5
    assert [row["content_text"] for row in payload["events"]] == ["event 4", "event 5"]

    response = live_catalog_client.get(
        f"/timeline/sessions/{seeded.session_id}/events",
        params={"anchor": "middle"},
    )

    assert response.status_code == 400
    assert "anchor" in response.json()["detail"]


def test_timeline_session_workspace_bootstraps_session_thread_and_projection(live_catalog, live_catalog_client):  # noqa: F811
    """One round trip returns the focused session, its thread and its first page."""

    owner = live_catalog.create_user(OWNER_EMAIL)
    seeded = live_catalog.commit_session(
        owner_id=owner,
        device_id="cinder",
        project="timeline-auth",
        texts=("first turn", "second turn"),
    )
    _set_browser_cookie(live_catalog_client, live_catalog, owner_id=owner)

    response = live_catalog_client.get(f"/timeline/sessions/{seeded.session_id}/workspace", params={"limit": 50})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert response.headers["cache-control"] == "no-store"
    assert payload["session"]["id"] == str(seeded.session_id)
    assert payload["thread"]["head_session_id"] == str(seeded.session_id)
    assert [row["id"] for row in payload["thread"]["sessions"]] == [str(seeded.session_id)]
    assert payload["projection"]["focus_session_id"] == str(seeded.session_id)
    assert payload["projection"]["total"] == 2
    assert [item["event"]["content_text"] for item in payload["projection"]["items"]] == [
        "first turn",
        "second turn",
    ]
    assert "catalog_session;dur=" in response.headers["server-timing"]
    assert "storage_manifest;dur=" in response.headers["server-timing"]


def test_timeline_session_workspace_404s_for_a_session_the_catalog_does_not_hold(
    live_catalog,  # noqa: F811
    live_catalog_client,  # noqa: F811
):
    """No archive fallback remains: an unknown session is a 404, not a legacy read."""

    owner = live_catalog.create_user(OWNER_EMAIL)
    _set_browser_cookie(live_catalog_client, live_catalog, owner_id=owner)

    response = live_catalog_client.get(f"/timeline/sessions/{uuid4()}/workspace", params={"limit": 50})

    assert response.status_code == 404, response.text
    assert "not found" in response.json()["detail"]


def test_timeline_and_agents_session_workspace_return_same_body(live_catalog, live_catalog_client):  # noqa: F811
    """The browser veneer and the machine surface must not drift apart."""

    owner = live_catalog.create_user(OWNER_EMAIL)
    token = live_catalog.create_device_token(owner_id=owner, device_id="cinder")
    seeded = live_catalog.commit_session(owner_id=owner, device_id="cinder", project="timeline-auth")
    _set_browser_cookie(live_catalog_client, live_catalog, owner_id=owner)

    timeline_response = live_catalog_client.get(
        f"/timeline/sessions/{seeded.session_id}/workspace",
        params={"limit": 50},
    )
    agents_response = live_catalog_client.get(
        f"/agents/sessions/{seeded.session_id}/workspace",
        params={"limit": 50},
        headers={"X-Agents-Token": token},
    )

    assert timeline_response.status_code == 200, timeline_response.text
    assert agents_response.status_code == 200, agents_response.text
    assert timeline_response.json() == agents_response.json()
    assert timeline_response.headers["cache-control"] == "no-store"
    assert agents_response.headers["cache-control"] == "no-store"


def test_timeline_session_projection_pages_the_catalog_transcript(live_catalog, live_catalog_client):  # noqa: F811
    """The browser projection route pages storage-v2, with no archive lane behind it."""

    owner = live_catalog.create_user(OWNER_EMAIL)
    seeded = live_catalog.commit_session(
        owner_id=owner,
        device_id="cinder",
        project="timeline-auth",
        texts=("turn one", "turn two", "turn three"),
    )
    _set_browser_cookie(live_catalog_client, live_catalog, owner_id=owner)

    response = live_catalog_client.get(
        f"/timeline/sessions/{seeded.session_id}/projection",
        params={"limit": 2, "anchor": "tail"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["focus_session_id"] == str(seeded.session_id)
    assert payload["total"] == 3
    assert [item["event"]["content_text"] for item in payload["items"]] == ["turn two", "turn three"]

    missing = live_catalog_client.get(f"/timeline/sessions/{uuid4()}/projection")
    assert missing.status_code == 404, missing.text


def test_timeline_session_mobile_tail_returns_the_catalog_tail(live_catalog, live_catalog_client):  # noqa: F811
    """The iOS tail route reads storage-v2 and stamps the revision it read."""

    owner = live_catalog.create_user(OWNER_EMAIL)
    seeded = live_catalog.commit_session(
        owner_id=owner,
        device_id="cinder",
        project="timeline-auth",
        texts=("turn one", "turn two", "turn three"),
    )
    _set_browser_cookie(live_catalog_client, live_catalog, owner_id=owner)

    response = live_catalog_client.get(
        f"/timeline/sessions/{seeded.session_id}/mobile-tail",
        params={"limit": 2},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert response.headers["cache-control"] == "no-store"
    assert payload["session"]["id"] == str(seeded.session_id)
    assert [item["event"]["content_text"] for item in payload["projection"]["items"]] == ["turn two", "turn three"]
    assert payload["snapshot_event_id"] == payload["workspace_revision"]["latest_event_id"]

    missing = live_catalog_client.get(f"/timeline/sessions/{uuid4()}/mobile-tail")
    assert missing.status_code == 404, missing.text


def test_timeline_machine_workspaces_accept_browser_session_cookie(live_catalog, live_catalog_client):  # noqa: F811
    """The launch picker reads workspaces through the cookie surface.

    Regression guard for the iOS/web launch sheet: this endpoint MUST be
    reachable with only a browser session cookie. The /api/agents sibling is
    device-token-only; if the clients (or this route) drift onto that auth
    surface they 401 and the picker silently shows an empty list.
    """

    owner = live_catalog.create_user(OWNER_EMAIL)
    live_catalog.create_device_token(owner_id=owner, device_id="dev-machine")
    # Two sessions in the same cwd outrank a single-session cwd via frecency.
    for _ in range(2):
        _seed_catalog_session(device_id="dev-machine", cwd="/tmp/timeline-auth", git_repo=None)
    _seed_catalog_session(
        device_id="dev-machine",
        cwd="/tmp/solo-workspace",
        git_repo="git@github.com:example/solo.git",
    )
    _set_browser_cookie(live_catalog_client, live_catalog, owner_id=owner)

    response = live_catalog_client.get("/timeline/machines/dev-machine/workspaces?limit=12")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["device_id"] == "dev-machine"
    paths = [w["path"] for w in payload["workspaces"]]
    assert "/tmp/timeline-auth" in paths
    assert "/tmp/solo-workspace" in paths
    # Frecency: the 2-session cwd outranks the 1-session cwd.
    scores = [w["score"] for w in payload["workspaces"]]
    assert scores == sorted(scores, reverse=True)
    busy = next(w for w in payload["workspaces"] if w["path"] == "/tmp/timeline-auth")
    assert busy["session_count"] == 2
    # Git-aware label for the repo-backed cwd.
    solo_ws = next(w for w in payload["workspaces"] if w["path"] == "/tmp/solo-workspace")
    assert solo_ws["label"] == "solo (main)"


def test_timeline_machine_workspaces_reject_agents_header_without_browser_session(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        _seed_session(db)

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            response = client.get(
                "/timeline/machines/dev-machine/workspaces",
                headers={"X-Agents-Token": "dev"},
            )

        assert response.status_code == 401
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()


def test_timeline_sessions_reject_agents_header_without_browser_session(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        _seed_session(db)

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode():
            response = client.get("/timeline/sessions", headers={"X-Agents-Token": "dev"})

        assert response.status_code == 401
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()


def test_agents_sessions_reject_browser_cookie_without_agents_token(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        _seed_session(db)

    client = _make_client(session_local)

    try:
        with _force_browser_jwt_mode(), _force_agents_token_mode():
            client.cookies.set(SESSION_COOKIE_NAME, _issue_session_cookie())
            response = client.get("/agents/sessions")

        assert response.status_code == 401
        assert response.json()["detail"] == "Missing authentication - provide X-Agents-Token header"
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()


def test_agents_sessions_reject_bearer_device_token_without_agents_header(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        _seed_session(db)

    client = _make_client(session_local)

    try:
        with _force_agents_token_mode():
            response = client.get("/agents/sessions", headers={"Authorization": "Bearer zdt_fake"})

        assert response.status_code == 401
        assert response.json()["detail"] == "Missing authentication - provide X-Agents-Token header"
    finally:
        api_app.dependency_overrides.clear()


def test_agents_sessions_reject_legacy_non_device_token(tmp_path):
    session_local = _make_db(tmp_path)
    with session_local() as db:
        _seed_user(db)
        _seed_session(db)

    client = _make_client(session_local)

    try:
        with _force_agents_token_mode():
            response = client.get("/agents/sessions", headers={"X-Agents-Token": "legacy-token"})

        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid or revoked device token"
    finally:
        api_app.dependency_overrides.clear()


def test_timeline_sessions_clamps_oversized_limit(live_catalog, live_catalog_client):  # noqa: F811
    owner = live_catalog.create_user(OWNER_EMAIL)
    for _ in range(_OVER_CAP_SESSIONS):
        live_catalog.commit_session(owner_id=owner, device_id="dev-machine", project="timeline-auth")
    _set_browser_cookie(live_catalog_client, live_catalog, owner_id=owner)

    response = live_catalog_client.get("/timeline/sessions?limit=500")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total"] > 100
    assert len(payload["sessions"]) <= 100
    assert response.headers.get("X-Limit-Cap") == "100"


def test_timeline_sessions_summary_clamps_oversized_limit(live_catalog, live_catalog_client):  # noqa: F811
    owner = live_catalog.create_user(OWNER_EMAIL)
    for _ in range(_OVER_CAP_SESSIONS):
        live_catalog.commit_session(owner_id=owner, device_id="dev-machine", project="timeline-auth")
    _set_browser_cookie(live_catalog_client, live_catalog, owner_id=owner)

    response = live_catalog_client.get("/timeline/sessions/summary?limit=500")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total"] > 100
    assert len(payload["sessions"]) <= 100
    assert response.headers.get("X-Limit-Cap") == "100"


def test_timeline_sessions_stream_clamps_oversized_limit(tmp_path, monkeypatch):
    """The SSE stream endpoint must clamp limit and surface X-Limit-Cap.

    We don't actually consume the stream — we replace the generator with an
    immediately-completing async generator and verify the EventSourceResponse
    carries our header and that the underlying params were clamped.
    """
    from zerg.dependencies.browser_auth import get_current_browser_user_id_short_lived
    from zerg.dependencies.browser_auth import require_current_browser_user_short_lived

    session_local = _make_db(tmp_path)
    with session_local() as db:
        user = _seed_user(db)
        _seed_session(db)
        user_id = user.id

    client = _make_client(session_local)
    api_app.dependency_overrides[get_current_browser_user_id_short_lived] = lambda: user_id
    api_app.dependency_overrides[require_current_browser_user_short_lived] = lambda: None

    captured: dict = {}

    async def _fake_stream(request, *, params, skip_initial_replay, owner_id=None):
        captured["limit"] = params.limit
        # Immediately end the stream so TestClient can return headers + close.
        if False:
            yield {}
        return

    import zerg.routers.timeline as timeline_router

    monkeypatch.setattr(timeline_router, "stream_live_catalog_timeline", _fake_stream)

    try:
        with client.stream("GET", "/timeline/sessions/stream?limit=500") as response:
            assert response.status_code == 200
            assert response.headers.get("X-Limit-Cap") == "100"
            # Drain the (empty) stream so the context manager closes cleanly.
            for _ in response.iter_bytes():
                pass

        assert captured["limit"] == 100
    finally:
        auth_deps._strategy_cache.clear()
        api_app.dependency_overrides.clear()
