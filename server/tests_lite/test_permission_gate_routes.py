"""Permission-gate routes against a real live catalog.

Held permission prompts are catalog interactions now: the PreToolUse hook
registers one through ``interaction.register.v2``, the browser answers it
through ``interaction.resolve.v2``, and the hook long-polls
``interaction.decision.read.v2``. Nothing here writes a ``SessionPauseRequest``
row, because no Runtime Host does either -- these tests drive the routes over
HTTP against the daemons ``live_catalog_harness`` provisions.
"""

from __future__ import annotations

import os
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import UUID
from uuid import uuid4

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("AUTH_DISABLED", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-1234")
os.environ.setdefault("INTERNAL_API_SECRET", Fernet.generate_key().decode())

from tests_lite.live_catalog_harness import LiveCatalog  # noqa: E402
from tests_lite.live_catalog_harness import live_catalog  # noqa: E402, F401
from tests_lite.live_catalog_harness import live_catalog_client  # noqa: E402, F401
from zerg.auth import managed_session_tokens as managed_tokens  # noqa: E402
from zerg.routers import session_chat  # noqa: E402


def _console_session(live: LiveCatalog, *, owner_id: int, provider: str = "claude") -> UUID:
    """Create the managed session a permission prompt can be held against."""

    session_id = uuid4()
    created = live.rpc(
        "session.console.create.v2",
        {
            "session": {
                "session_id": str(session_id),
                "thread_id": str(uuid4()),
                "owner_id": owner_id,
                "provider": provider,
                "device_id": "cinder",
                "cwd": "/workspace/perm-gate",
                "started_at": datetime.now(UTC).isoformat(),
            }
        },
    )
    assert created["created"] is True, created
    return session_id


def _hook_headers(*, owner_id: int, session_id: UUID) -> dict[str, str]:
    """The hook-scoped session token a managed provider hook actually carries."""

    token = managed_tokens.issue_managed_session_token(
        owner_id=owner_id,
        session_id=str(session_id),
        project="perm-gate",
        device_id="cinder",
        scope=managed_tokens.MANAGED_SESSION_SCOPE_HOOK,
    )
    return {"X-Agents-Token": token}


def _seed(live: LiveCatalog, *, email: str, provider: str = "claude") -> tuple[int, UUID, dict[str, str], dict[str, str]]:
    owner_id = live.create_user(email)
    session_id = _console_session(live, owner_id=owner_id, provider=provider)
    return (
        owner_id,
        session_id,
        _hook_headers(owner_id=owner_id, session_id=session_id),
        {"longhouse_session": live.browser_cookie(owner_id=owner_id, email=email)},
    )


def _interactions(live: LiveCatalog, session_id: UUID, *, status: str | None = None) -> list[dict]:
    return list(
        live.rpc(
            "interaction.list.v2",
            {"session_id": str(session_id), "status": status, "limit": 20},
        )["interactions"]
    )


def _resolve(live: LiveCatalog, session_id: UUID, interaction_id: str, *, status: str, payload: dict) -> dict:
    return live.rpc(
        "interaction.resolve.v2",
        {
            "session_id": str(session_id),
            "interaction_id": interaction_id,
            "status": status,
            "response_payload": payload,
            "response_text": payload.get("permissionDecisionReason"),
            "resolved_at": datetime.now(UTC).isoformat(),
        },
    )


def test_register_then_poll_returns_decision_after_resolve(live_catalog, live_catalog_client):
    _owner_id, session_id, headers, _cookies = _seed(live_catalog, email="perm-poll@test.local")
    tool_use_id = "toolu_abc123"

    resp = live_catalog_client.post(
        "/agents/permission-requests",
        json={
            "session_id": str(session_id),
            "tool_use_id": tool_use_id,
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    ack = resp.json()
    assert ack["status"] == "pending"

    # Before an answer, the hook poll sees pending (no decision yet).
    poll = live_catalog_client.get(
        "/agents/permission-decision",
        params={"session_id": str(session_id), "tool_use_id": tool_use_id},
        headers=headers,
    )
    assert poll.status_code == 200, poll.text
    assert poll.json() == {"decision": None, "reason": None, "resolved": False}

    # The held request is an answerable permission_prompt interaction.
    held = _interactions(live_catalog, session_id)
    assert len(held) == 1
    assert held[0]["id"] == ack["pause_request_id"]
    assert held[0]["request_key"] == ack["request_key"]
    assert held[0]["kind"] == "permission_prompt"
    assert held[0]["can_respond"] is True
    assert held[0]["provider_request_id"] == tool_use_id

    _resolve(
        live_catalog,
        session_id,
        held[0]["id"],
        status="resolved",
        payload={"permissionDecision": "allow", "permissionDecisionReason": "approved in test"},
    )

    # Now the hook poll returns the decision.
    poll2 = live_catalog_client.get(
        "/agents/permission-decision",
        params={"session_id": str(session_id), "tool_use_id": tool_use_id},
        headers=headers,
    )
    assert poll2.status_code == 200, poll2.text
    assert poll2.json() == {"decision": "allow", "reason": "approved in test", "resolved": True}


def test_cursor_permission_request_has_provider_copy_and_deadline(live_catalog, live_catalog_client):
    _owner_id, session_id, headers, _cookies = _seed(live_catalog, email="perm-cursor@test.local", provider="cursor")

    before = datetime.now(UTC)
    ack = live_catalog_client.post(
        "/agents/permission-requests",
        json={
            "session_id": str(session_id),
            "tool_use_id": "cursor-shell-1",
            "tool_name": "Shell",
            "provider": "cursor",
            "wait_timeout_seconds": 7,
        },
        headers=headers,
    )
    assert ack.status_code == 200, ack.text
    held = _interactions(live_catalog, session_id)[0]
    assert held["projection"]["summary"] == "Cursor wants to use Shell."
    # The catalog enforces the provider's closed contract, so Cursor's prompt
    # can only be held under Cursor's own source and poll transport.
    assert held["source"] == "cursor_permission_gate"
    assert held["reply_transport"] == "cursor_permission_poll"
    expires_at = datetime.fromisoformat(held["expires_at"])
    assert before + timedelta(seconds=6) <= expires_at <= before + timedelta(seconds=9)

    poll = live_catalog_client.get(
        "/agents/permission-decision",
        params={"session_id": str(session_id), "tool_use_id": "cursor-shell-1", "provider": "cursor"},
        headers=headers,
    )
    assert poll.json() == {"decision": None, "reason": None, "resolved": False}


def test_permission_deadline_cannot_be_late_approved(live_catalog, live_catalog_client):
    _owner_id, session_id, headers, cookies = _seed(live_catalog, email="perm-deadline@test.local")

    ack = live_catalog_client.post(
        "/agents/permission-requests",
        json={
            "session_id": str(session_id),
            "tool_use_id": "expired",
            # Registered with a deadline that has already passed, which is what
            # a hook retry after a long stall looks like.
            "occurred_at": (datetime.now(UTC) - timedelta(seconds=30)).isoformat(),
            "wait_timeout_seconds": 1,
        },
        headers=headers,
    ).json()

    # The deadline decides before anyone answers, and no late answer can move it.
    poll = live_catalog_client.get(
        "/agents/permission-decision",
        params={"session_id": str(session_id), "tool_use_id": "expired"},
        headers=headers,
    )
    assert poll.json() == {"decision": "deny", "reason": "Approval deadline expired", "resolved": True}

    late = _resolve(
        live_catalog,
        session_id,
        ack["pause_request_id"],
        status="resolved",
        payload={"permissionDecision": "allow", "permissionDecisionReason": "too late"},
    )
    assert late["resolved"] is False
    assert late["interaction"]["status"] == "expired"

    refused = live_catalog_client.post(
        f"/sessions/{session_id}/pause-requests/{ack['pause_request_id']}/response",
        json={"decision": "answer"},
        cookies=cookies,
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "pause_request_not_pending"


def test_poll_unknown_tool_use_id_is_pending(live_catalog, live_catalog_client):
    _owner_id, session_id, headers, _cookies = _seed(live_catalog, email="perm-unknown-tool@test.local")

    poll = live_catalog_client.get(
        "/agents/permission-decision",
        params={"session_id": str(session_id), "tool_use_id": "toolu_never_registered"},
        headers=headers,
    )

    assert poll.status_code == 200, poll.text
    assert poll.json() == {"decision": None, "reason": None, "resolved": False}


def test_register_unknown_session_is_404(live_catalog, live_catalog_client):
    owner_id = live_catalog.create_user("perm-unknown-session@test.local")
    unknown_session_id = uuid4()

    resp = live_catalog_client.post(
        "/agents/permission-requests",
        json={"session_id": str(unknown_session_id), "tool_use_id": "toolu_x", "tool_name": "Bash"},
        headers=_hook_headers(owner_id=owner_id, session_id=unknown_session_id),
    )

    assert resp.status_code == 404, resp.text


def test_deny_resolution_maps_to_deny_decision(live_catalog, live_catalog_client):
    _owner_id, session_id, headers, _cookies = _seed(live_catalog, email="perm-deny@test.local")
    tool_use_id = "toolu_deny"

    ack = live_catalog_client.post(
        "/agents/permission-requests",
        json={"session_id": str(session_id), "tool_use_id": tool_use_id, "tool_name": "Bash"},
        headers=headers,
    ).json()
    _resolve(live_catalog, session_id, ack["pause_request_id"], status="rejected", payload={})

    poll = live_catalog_client.get(
        "/agents/permission-decision",
        params={"session_id": str(session_id), "tool_use_id": tool_use_id},
        headers=headers,
    )

    assert poll.json()["decision"] == "deny"
    assert poll.json()["resolved"] is True


def test_answer_via_pause_route_resolves_in_place_without_push(live_catalog, live_catalog_client, monkeypatch):
    """The full loop: register -> answer via the pause-response route (pull-mode,
    no managed-control websocket push) -> hook poll returns allow."""
    _owner_id, session_id, headers, cookies = _seed(live_catalog, email="perm-answer@test.local")

    pushed: list[dict] = []

    async def _fail_if_pushed(**kwargs):
        pushed.append(kwargs)
        raise AssertionError("permission prompts must not push over managed-control")

    monkeypatch.setattr(session_chat, "answer_pause_request_on_managed_local_session", _fail_if_pushed)

    tool_use_id = "toolu_loop"
    ack = live_catalog_client.post(
        "/agents/permission-requests",
        json={"session_id": str(session_id), "tool_use_id": tool_use_id, "tool_name": "Bash"},
        headers=headers,
    ).json()

    # Answer through the real browser pause-response route.
    resp = live_catalog_client.post(
        f"/sessions/{session_id}/pause-requests/{ack['pause_request_id']}/response",
        json={"decision": "answer"},
        cookies=cookies,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "resolved"
    assert not pushed  # never dispatched a websocket command

    # The hook poll now reads allow.
    poll = live_catalog_client.get(
        "/agents/permission-decision",
        params={"session_id": str(session_id), "tool_use_id": tool_use_id},
        headers=headers,
    )
    assert poll.json() == {"decision": "allow", "reason": "Longhouse allow", "resolved": True}


def test_reject_via_pause_route_maps_to_deny(live_catalog, live_catalog_client, monkeypatch):
    _owner_id, session_id, headers, cookies = _seed(live_catalog, email="perm-reject@test.local")

    async def _fail_if_pushed(**kwargs):
        raise AssertionError("permission prompts must not push over managed-control")

    monkeypatch.setattr(session_chat, "answer_pause_request_on_managed_local_session", _fail_if_pushed)

    tool_use_id = "toolu_loop_deny"
    ack = live_catalog_client.post(
        "/agents/permission-requests",
        json={"session_id": str(session_id), "tool_use_id": tool_use_id, "tool_name": "Bash"},
        headers=headers,
    ).json()

    resp = live_catalog_client.post(
        f"/sessions/{session_id}/pause-requests/{ack['pause_request_id']}/response",
        json={"decision": "reject"},
        cookies=cookies,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "rejected"

    poll = live_catalog_client.get(
        "/agents/permission-decision",
        params={"session_id": str(session_id), "tool_use_id": tool_use_id},
        headers=headers,
    )
    assert poll.json()["decision"] == "deny"


def test_dispatch_keys_on_transport_not_kind(live_catalog, live_catalog_client, monkeypatch):
    """A held request whose reply_transport is not a pull transport must route to
    the managed-control PUSH path, proving dispatch keys on transport, not kind."""
    owner_id, session_id, _headers, cookies = _seed(live_catalog, email="perm-push@test.local", provider="codex")

    pushed: list[dict] = []

    async def _fake_push(**kwargs):
        pushed.append(kwargs)
        from zerg.services.managed_local_control import ManagedLocalSendResult

        return ManagedLocalSendResult(ok=True, exit_code=0, response_data={"status": "resolved"})

    monkeypatch.setattr(session_chat, "answer_pause_request_on_managed_local_session", _fake_push)

    now = datetime.now(UTC)
    runtime_key = f"codex:{session_id}"
    registered = live_catalog.rpc(
        "interaction.register.v2",
        {
            "interaction": {
                "session_id": str(session_id),
                "runtime_key": runtime_key,
                "provider": "codex",
                "device_id": "cinder",
                "source": "codex_app_server",
                "reply_transport": "managed_push",
                "provider_request_id": "perm-push",
                "request_key": f"codex:{runtime_key}:perm-push",
                "kind": "structured_question",
                "tool_name": None,
                "title": "Approve edit",
                "summary": "Codex is asking to apply a patch.",
                "request_payload": {},
                "can_respond": True,
                "occurred_at": now.isoformat(),
                "expires_at": None,
                "single_active": True,
            }
        },
    )
    assert registered["found_session"] is True, registered

    resp = live_catalog_client.post(
        f"/sessions/{session_id}/pause-requests/{registered['interaction']['id']}/response",
        json={"decision": "answer"},
        cookies=cookies,
    )

    assert resp.status_code == 200, resp.text
    assert pushed, "a non-pull transport must dispatch over managed control"
    assert pushed[0]["owner_id"] == owner_id


def test_permission_prompt_request_is_user_facing(live_catalog, live_catalog_client):
    """Answerable permission-gate requests must reach the browser's pending list
    instead of being filtered out as provider bookkeeping."""
    _owner_id, session_id, headers, cookies = _seed(live_catalog, email="perm-visible@test.local")

    ack = live_catalog_client.post(
        "/agents/permission-requests",
        json={"session_id": str(session_id), "tool_use_id": "toolu_vis", "tool_name": "Bash"},
        headers=headers,
    ).json()

    listed = live_catalog_client.get(f"/sessions/{session_id}/pause-requests", cookies=cookies)

    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["total"] == 1
    request = body["requests"][0]
    assert request["id"] == ack["pause_request_id"]
    assert request["kind"] == "permission_prompt"
    assert request["can_respond"] is True
    assert request["summary"] == "Claude wants to use Bash."
    # The catalog carries the transport the answer path dispatches on.
    assert _interactions(live_catalog, session_id)[0]["reply_transport"] == "claude_pretooluse_pull"


def test_same_tool_use_id_register_is_idempotent(live_catalog, live_catalog_client):
    """A tool_use_id is unique per Claude tool invocation, so re-registering it
    (a hook network retry) must return the SAME row, not orphan the first poll.
    The poll-by-pause_request_id handle is what the hook uses to read its row."""
    _owner_id, session_id, headers, _cookies = _seed(live_catalog, email="perm-idempotent@test.local")
    tool_use_id = "toolu_dup"
    body = {"session_id": str(session_id), "tool_use_id": tool_use_id, "tool_name": "Bash"}

    ack1 = live_catalog_client.post("/agents/permission-requests", json=body, headers=headers).json()
    ack2 = live_catalog_client.post("/agents/permission-requests", json=body, headers=headers).json()

    # Idempotent: same invocation -> same held interaction.
    assert ack1["pause_request_id"] == ack2["pause_request_id"]
    assert len(_interactions(live_catalog, session_id)) == 1

    _resolve(
        live_catalog,
        session_id,
        ack1["pause_request_id"],
        status="rejected",
        payload={"permissionDecision": "deny", "permissionDecisionReason": "no"},
    )
    poll = live_catalog_client.get(
        "/agents/permission-decision",
        params={
            "session_id": str(session_id),
            "tool_use_id": tool_use_id,
            "pause_request_id": ack1["pause_request_id"],
        },
        headers=headers,
    )

    assert poll.json() == {"decision": "deny", "reason": "no", "resolved": True}


def test_register_rejects_empty_tool_use_id(live_catalog, live_catalog_client):
    _owner_id, session_id, headers, _cookies = _seed(live_catalog, email="perm-empty-tool@test.local")

    resp = live_catalog_client.post(
        "/agents/permission-requests",
        json={"session_id": str(session_id), "tool_use_id": "", "tool_name": "Bash"},
        headers=headers,
    )

    assert resp.status_code == 400, resp.text


def test_resolved_without_decision_payload_maps_to_deny(live_catalog, live_catalog_client):
    """A row resolved WITHOUT an explicit permissionDecision (e.g. superseded)
    must read as deny, never a silent allow."""
    _owner_id, session_id, headers, _cookies = _seed(live_catalog, email="perm-no-payload@test.local")

    ack = live_catalog_client.post(
        "/agents/permission-requests",
        json={"session_id": str(session_id), "tool_use_id": "toolu_nopayload", "tool_name": "Bash"},
        headers=headers,
    ).json()
    _resolve(live_catalog, session_id, ack["pause_request_id"], status="resolved", payload={})

    poll = live_catalog_client.get(
        "/agents/permission-decision",
        params={
            "session_id": str(session_id),
            "tool_use_id": "toolu_nopayload",
            "pause_request_id": ack["pause_request_id"],
        },
        headers=headers,
    )

    assert poll.json()["decision"] == "deny"
    assert poll.json()["resolved"] is True
