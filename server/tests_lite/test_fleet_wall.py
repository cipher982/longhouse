"""Fleet wall and session tail, asserted against a real live catalog.

Covers:
- GET /agents/sessions/wall — raw signal metadata with repo/project filters
- GET /agents/sessions/{id}/tail — tail-biased recent events

Both routes read the live catalog and nothing else: the wall projects a
catalogd timeline snapshot, and tail reads the storage-v2 render objects the
Machine Agent sealed. This file used to seed ``AgentSession``/``AgentEvent``
rows behind a ``get_db`` override, which is the branch a Runtime Host never
takes, so the sessions here are shipped through the real ingest route and the
control rows a bridge would leave behind are written into the catalog the
daemon owns.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import UUID
from uuid import uuid4

from sqlalchemy.orm import Session

from tests_lite.live_catalog_harness import LiveCatalog
from tests_lite.live_catalog_harness import live_catalog  # noqa: F401
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: F401
from tests_lite.test_delegation_lifecycle import DEVICE_ID as DELEGATION_DEVICE_ID
from tests_lite.test_delegation_lifecycle import PROVIDER as DELEGATION_PROVIDER
from tests_lite.test_delegation_lifecycle import _seed_running_session
from zerg.auth.hosted import session_cookie_name
from zerg.catalogd.schema import create_catalog_engine
from zerg.models.live_store import LiveSessionConnection
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.services.catalogd_supervisor import catalogd_paths

DEVICE_ID = "shipper-laptop"

# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


def _ship(
    live_catalog: LiveCatalog,
    client,
    *,
    owner_id: int,
    device_id: str = DEVICE_ID,
    texts: tuple[str, ...] = ("fix the bug",),
    project: str = "zerg",
    git_repo: str | None = None,
    cwd: str | None = None,
    roles: tuple[str, ...] | None = None,
    tool_name: str | None = None,
    now: datetime | None = None,
) -> UUID:
    """Ship one transcript the way a Machine Agent ships it.

    The wire envelope carries the session facts, so repo, cwd and activity
    window are set where a shipper sets them rather than written behind the
    route's back. ``roles`` shapes the render records the same way, which is the
    only place an event's role exists.
    """

    session_id = uuid4()
    token = live_catalog.create_device_token(owner_id=owner_id, device_id=device_id)
    body = live_catalog.envelope_body(
        session_id=session_id,
        device_id=device_id,
        texts=texts,
        project=project,
        now=now,
    )
    if git_repo is not None:
        body["session"]["git_repo"] = git_repo
    if cwd is not None:
        body["session"]["cwd"] = cwd
    for index, record in enumerate(body["render"]["records"]):
        if roles is not None:
            record["role"] = roles[index]
        if record["role"] == "tool":
            record["tool_name"] = tool_name
            record["tool_output_text"] = record["content_text"]
            record["content_text"] = None
    response = client.post(
        "/agents/storage/v2/envelopes",
        json=body,
        headers={"X-Agents-Token": token, "X-Longhouse-Storage-Lane": "live"},
    )
    assert response.status_code == 200, response.text
    return session_id


def _seed_control_connection(
    session_id: UUID,
    *,
    device_id: str = DEVICE_ID,
    provider: str = "codex",
    control_plane: str,
    state: str,
    acquisition_kind: str = "spawned_control",
    **capabilities: int,
) -> None:
    """Leave the thread, run and connection a control plane leaves behind.

    Written straight into the catalog daemon's database because no route in
    this test's surface acquires a control connection; the capability
    projection the wall reads is what is under test, not how the rows arrive.
    """

    database_path, _socket_path = catalogd_paths()
    engine = create_catalog_engine(database_path)
    now = datetime.now(UTC).replace(microsecond=0)
    thread_id, run_id = str(uuid4()), str(uuid4())
    try:
        with Session(engine) as db:
            db.add(
                LiveSessionThread(
                    id=thread_id,
                    session_id=str(session_id),
                    provider=provider,
                    branch_kind="root",
                    is_primary=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            db.add(
                LiveSessionRun(
                    id=run_id,
                    thread_id=thread_id,
                    provider=provider,
                    host_id=device_id,
                    launch_origin="longhouse_spawned",
                    started_at=now,
                )
            )
            db.add(
                LiveSessionConnection(
                    run_id=run_id,
                    adapter_connection_id=str(uuid4()),
                    lease_generation=str(uuid4()),
                    control_plane=control_plane,
                    acquisition_kind=acquisition_kind,
                    state=state,
                    device_id=device_id,
                    acquired_at=now,
                    last_health_at=now,
                    **capabilities,
                )
            )
            db.commit()
    finally:
        engine.dispose()


def _headers(live_catalog: LiveCatalog, owner_id: int, *, device_id: str = DEVICE_ID) -> dict[str, str]:
    return {"X-Agents-Token": live_catalog.create_device_token(owner_id=owner_id, device_id=device_id)}


# ---------------------------------------------------------------------------
# Wall endpoint tests
# ---------------------------------------------------------------------------


def test_wall_returns_sessions(live_catalog, live_catalog_client):
    """GET /agents/sessions/wall returns sessions with raw signal metadata."""
    owner_id = live_catalog.create_user("owner@wall.test")
    _ship(
        live_catalog,
        live_catalog_client,
        owner_id=owner_id,
        device_id="shipper-laptop",
        cwd="/Users/dev/git/zerg",
        git_repo="https://github.com/user/repo",
        project="zerg",
        texts=("fix the bug", "and the other one", "and this one"),
    )

    resp = live_catalog_client.get("/agents/sessions/wall", headers=_headers(live_catalog, owner_id))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] >= 1
    session = next(s for s in data["sessions"] if s["device_name"] == "shipper-laptop")
    assert session["cwd"] == "/Users/dev/git/zerg"
    assert session["git_repo"] == "https://github.com/user/repo"
    assert session["git_branch"] == "main"
    assert session["project"] == "zerg"
    assert session["provider"] == "codex"
    assert session["user_messages"] == 3


def test_wall_filters_by_repo(live_catalog, live_catalog_client):
    """Wall query repo filter does substring match."""
    owner_id = live_catalog.create_user("owner@wall.test")
    _ship(live_catalog, live_catalog_client, owner_id=owner_id, git_repo="https://github.com/user/zerg", project="zerg")
    _ship(live_catalog, live_catalog_client, owner_id=owner_id, git_repo="https://github.com/user/other", project="other")

    resp = live_catalog_client.get(
        "/agents/sessions/wall",
        params={"repo": "user/zerg"},
        headers=_headers(live_catalog, owner_id),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] == 1
    assert "zerg" in data["sessions"][0]["git_repo"]


def test_wall_repo_filter_matches_cwd(live_catalog, live_catalog_client):
    """Wall repo filter also matches against cwd for non-git workspaces."""
    owner_id = live_catalog.create_user("owner@wall.test")
    _ship(
        live_catalog,
        live_catalog_client,
        owner_id=owner_id,
        cwd="/Users/dev/git/acme/project",
        git_repo=None,
        project="project",
    )
    _ship(
        live_catalog,
        live_catalog_client,
        owner_id=owner_id,
        cwd="/Users/dev/git/zerg",
        git_repo="https://github.com/user/other",
        project="zerg",
    )

    resp = live_catalog_client.get(
        "/agents/sessions/wall",
        params={"repo": "acme"},
        headers=_headers(live_catalog, owner_id),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] == 1
    assert data["sessions"][0]["cwd"] == "/Users/dev/git/acme/project"


def test_wall_filters_by_project(live_catalog, live_catalog_client):
    """Wall query project filter returns only matching sessions."""
    owner_id = live_catalog.create_user("owner@wall.test")
    _ship(live_catalog, live_catalog_client, owner_id=owner_id, project="zerg", git_repo="a")
    _ship(live_catalog, live_catalog_client, owner_id=owner_id, project="hdr", git_repo="b")

    resp = live_catalog_client.get(
        "/agents/sessions/wall",
        params={"project": "zerg"},
        headers=_headers(live_catalog, owner_id),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] == 1
    assert data["sessions"][0]["project"] == "zerg"


def _timeline_heads(live_catalog: LiveCatalog, client, *, owner_id: int, email: str) -> dict[str, dict]:
    client.cookies.set(session_cookie_name(False), live_catalog.browser_cookie(owner_id=owner_id, email=email))
    try:
        response = client.get("/timeline/sessions?days_back=7")
    finally:
        client.cookies.clear()
    assert response.status_code == 200, response.text
    return {card["head"]["id"]: card["head"] for card in response.json()["sessions"]}


def test_wall_presence_agrees_with_the_served_timeline(live_catalog, live_catalog_client):
    """Wall/peers presence is the timeline's served state, never a second derivation.

    A hook presence post on a session with no owned run is not served activity:
    the timeline reads it as unknown, so the wall must too. The wall used to
    overlay the legacy runtime row and report ``needs_user`` here while the
    timeline said "Imported". A session with a durable run that reports a tool
    is served as running on both surfaces.
    """
    email = "owner@wall.test"
    owner_id = live_catalog.create_user(email)
    token = live_catalog.create_device_token(owner_id=owner_id, device_id=DELEGATION_DEVICE_ID)
    headers = {"X-Agents-Token": token}

    imported = _ship(live_catalog, live_catalog_client, owner_id=owner_id, device_id=DELEGATION_DEVICE_ID, git_repo="agree")
    response = live_catalog_client.post(
        "/agents/presence",
        json={"session_id": str(imported), "state": "needs_user", "cwd": "/tmp", "provider": "codex"},
        headers=headers,
    )
    assert response.status_code == 204, response.text

    running, thread_id, run_id = _seed_running_session(live_catalog, owner_id=owner_id)
    started = live_catalog.rpc(
        "session.console.turn.update.v2",
        {
            "turn": {
                "run_id": run_id,
                "owner_id": owner_id,
                "session_id": str(running),
                "thread_id": str(thread_id),
                "provider": DELEGATION_PROVIDER,
                "device_id": DELEGATION_DEVICE_ID,
                "state": "active",
                "expected_state": "starting",
                "error_code": None,
                "error": None,
                "updated_at": datetime.now(UTC).isoformat(),
            }
        },
    )
    assert started, started
    response = live_catalog_client.post(
        "/agents/presence",
        json={
            "session_id": str(running),
            "provider": DELEGATION_PROVIDER,
            "run_id": run_id,
            "state": "running",
            "tool_name": "Read",
            "occurred_at": datetime.now(UTC).isoformat(),
        },
        headers=headers,
    )
    assert response.status_code == 204, response.text

    heads = _timeline_heads(live_catalog, live_catalog_client, owner_id=owner_id, email=email)
    wall_response = live_catalog_client.get("/agents/sessions/wall", headers=headers)
    assert wall_response.status_code == 200, wall_response.text
    wall = {row["session_id"]: row for row in wall_response.json()["sessions"]}

    assert heads[str(imported)]["presence_state"] is None
    assert wall[str(imported)]["presence_state"] is None
    assert wall[str(imported)]["has_live_presence"] is False

    assert heads[str(running)]["presence_state"] == "running"
    assert wall[str(running)]["presence_state"] == "running"
    assert wall[str(running)]["has_live_presence"] is True


def test_wall_includes_kernel_control_buckets(live_catalog, live_catalog_client):
    """Wall exposes the same control bucket truth as timeline/detail."""
    owner_id = live_catalog.create_user("owner@wall.test")

    live_session = _ship(
        live_catalog,
        live_catalog_client,
        owner_id=owner_id,
        device_id="shipper-live-machine",
        git_repo="control-live",
        project="live",
    )
    _seed_control_connection(
        live_session,
        device_id="shipper-live-machine",
        control_plane="claude_channel_bridge",
        state="attached",
        can_send_input=1,
        can_interrupt=1,
        can_tail_output=1,
    )
    reattach_session = _ship(
        live_catalog,
        live_catalog_client,
        owner_id=owner_id,
        device_id="shipper-reattach-machine",
        git_repo="control-reattach",
        project="reattach",
    )
    _seed_control_connection(
        reattach_session,
        device_id="shipper-reattach-machine",
        control_plane="codex_bridge",
        state="detached",
        can_send_input=1,
        can_tail_output=1,
        can_resume=1,
    )
    observe_session = _ship(
        live_catalog,
        live_catalog_client,
        owner_id=owner_id,
        device_id="shipper-observe-machine",
        git_repo="control-observe",
        project="observe",
    )
    _seed_control_connection(
        observe_session,
        device_id="shipper-observe-machine",
        control_plane="log_tail",
        acquisition_kind="observe_only",
        state="attached",
        can_send_input=0,
        can_interrupt=0,
        can_tail_output=1,
    )
    imported_session = _ship(
        live_catalog,
        live_catalog_client,
        owner_id=owner_id,
        device_id="shipper-imported-machine",
        git_repo="control-imported",
        project="imported",
    )

    resp = live_catalog_client.get(
        "/agents/sessions/wall",
        params={"repo": "control-", "limit": 10},
        headers=_headers(live_catalog, owner_id),
    )
    assert resp.status_code == 200, resp.text
    rows = {row["session_id"]: row for row in resp.json()["sessions"]}

    assert rows[str(live_session)]["kernel_control_label"] == "live"
    assert rows[str(live_session)]["kernel_live_control_available"] is True
    assert rows[str(live_session)]["kernel_host_reattach_available"] is True
    assert rows[str(live_session)]["kernel_observe_only"] is False
    assert rows[str(live_session)]["kernel_search_only"] is False

    assert rows[str(reattach_session)]["kernel_control_label"] == "reattach"
    assert rows[str(reattach_session)]["kernel_live_control_available"] is False
    assert rows[str(reattach_session)]["kernel_host_reattach_available"] is True
    assert rows[str(reattach_session)]["kernel_staleness_reason"] == "connection_released"

    assert rows[str(observe_session)]["kernel_control_label"] == "search-only"
    assert rows[str(observe_session)]["kernel_observe_only"] is True
    assert rows[str(observe_session)]["kernel_search_only"] is False

    assert rows[str(imported_session)]["kernel_control_label"] == "imported"
    assert rows[str(imported_session)]["kernel_live_control_available"] is False
    assert rows[str(imported_session)]["kernel_host_reattach_available"] is False
    assert rows[str(imported_session)]["kernel_search_only"] is True


def test_wall_excludes_old_sessions(live_catalog, live_catalog_client):
    """Sessions older than the days param are excluded."""
    owner_id = live_catalog.create_user("owner@wall.test")
    _ship(
        live_catalog,
        live_catalog_client,
        owner_id=owner_id,
        git_repo="old",
        project="old",
        now=datetime.now(UTC).replace(microsecond=0) - timedelta(days=10),
    )
    _ship(live_catalog, live_catalog_client, owner_id=owner_id, git_repo="recent", project="recent")

    resp = live_catalog_client.get(
        "/agents/sessions/wall",
        params={"days": 3},
        headers=_headers(live_catalog, owner_id),
    )
    assert resp.status_code == 200, resp.text
    repos = [s["git_repo"] for s in resp.json()["sessions"]]
    assert "recent" in repos
    assert "old" not in repos


# ---------------------------------------------------------------------------
# Session tail tests
# ---------------------------------------------------------------------------


def test_tail_returns_404_for_missing_session(live_catalog, live_catalog_client):
    """GET /agents/sessions/{id}/tail returns 404 for nonexistent session."""
    owner_id = live_catalog.create_user("owner@tail.test")

    resp = live_catalog_client.get(
        f"/agents/sessions/{uuid4()}/tail",
        headers=_headers(live_catalog, owner_id),
    )

    assert resp.status_code == 404, resp.text


def test_tail_returns_recent_events(live_catalog, live_catalog_client):
    """GET /agents/sessions/{id}/tail returns last N events in chronological order."""
    owner_id = live_catalog.create_user("owner@tail.test")
    session_id = _ship(
        live_catalog,
        live_catalog_client,
        owner_id=owner_id,
        texts=("first message", "second response", "third message"),
        roles=("user", "assistant", "user"),
    )

    resp = live_catalog_client.get(
        f"/agents/sessions/{session_id}/tail",
        params={"limit": 2},
        headers=_headers(live_catalog, owner_id),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["session_id"] == str(session_id)
    assert len(data["events"]) == 2
    # Chronological order (oldest first)
    assert data["events"][0]["content"] == "second response"
    assert data["events"][1]["content"] == "third message"


def test_tail_filters_to_user_assistant_tool(live_catalog, live_catalog_client):
    """Tail only returns user, assistant, and tool role events."""
    owner_id = live_catalog.create_user("owner@tail.test")
    session_id = _ship(
        live_catalog,
        live_catalog_client,
        owner_id=owner_id,
        texts=("visible", "hidden", "also visible"),
        roles=("user", "system", "assistant"),
    )

    resp = live_catalog_client.get(
        f"/agents/sessions/{session_id}/tail",
        headers=_headers(live_catalog, owner_id),
    )
    assert resp.status_code == 200, resp.text
    roles = [event["role"] for event in resp.json()["events"]]
    assert "system" not in roles
    assert "user" in roles
    assert "assistant" in roles


def test_tail_truncates_long_content(live_catalog, live_catalog_client):
    """Content longer than 4000 chars is truncated."""
    owner_id = live_catalog.create_user("owner@tail.test")
    session_id = _ship(live_catalog, live_catalog_client, owner_id=owner_id, texts=("x" * 8000,))

    resp = live_catalog_client.get(
        f"/agents/sessions/{session_id}/tail",
        headers=_headers(live_catalog, owner_id),
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["events"][0]["content"]) == 4000


def test_tail_includes_tool_name(live_catalog, live_catalog_client):
    """Tool events include the tool_name field."""
    owner_id = live_catalog.create_user("owner@tail.test")
    session_id = _ship(
        live_catalog,
        live_catalog_client,
        owner_id=owner_id,
        texts=("output",),
        roles=("tool",),
        tool_name="Bash",
    )

    resp = live_catalog_client.get(
        f"/agents/sessions/{session_id}/tail",
        headers=_headers(live_catalog, owner_id),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["events"][0]["tool_name"] == "Bash"
