from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
from uuid import UUID
from uuid import uuid4

from tests_lite.live_catalog_harness import LiveCatalog
from tests_lite.live_catalog_harness import live_catalog as live_catalog
from tests_lite.live_catalog_harness import live_catalog_client as live_catalog_client
from zerg.services.session_runtime import runtime_key_for_session


PROVIDER = "claude"
DEVICE_ID = "cinder"


def _task(
    task_id: str,
    kind: str,
    status: str,
    description: str,
    *,
    parent_tool_call_id: str | None = None,
) -> dict[str, str]:
    """The provider-authored task shape; server-owned clocks are omitted."""

    item = {"id": task_id, "kind": kind, "status": status, "description": description}
    if parent_tool_call_id is not None:
        item["parent_tool_call_id"] = parent_tool_call_id
    return item


def _event(
    *,
    session_id: UUID,
    thread_id: UUID,
    run_id: str,
    occurred_at: datetime,
    dedupe_key: str,
    items: list[dict[str, str]] | None = None,
    snapshot_observed_at: datetime | None = None,
    phase: str = "idle",
    freshness_ms: int = 120_000,
) -> dict[str, Any]:
    delegation: dict[str, Any] = {"items": items} if items is not None else {}
    if snapshot_observed_at is not None:
        delegation["observed_at"] = snapshot_observed_at.isoformat()
    delegation["freshness_ms"] = freshness_ms
    return {
        "runtime_key": runtime_key_for_session(PROVIDER, str(session_id)),
        "session_id": str(session_id),
        "thread_id": str(thread_id),
        "run_id": run_id,
        "provider": PROVIDER,
        "device_id": DEVICE_ID,
        "source": "claude_hook",
        "kind": "phase_signal",
        "phase": phase,
        "tool_name": None,
        "occurred_at": occurred_at.isoformat(),
        "freshness_ms": freshness_ms,
        "dedupe_key": dedupe_key,
        "payload": {"delegation": delegation} if items is not None or snapshot_observed_at is not None else {},
    }


def _seed_running_session(
    live: LiveCatalog,
    *,
    owner_id: int,
    provider_session_id: str | None = None,
) -> tuple[UUID, UUID, str]:
    """Create a real catalog session, transcript, and durable first run."""

    session_id = uuid4()
    thread_id = uuid4()
    now = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=10)
    created = live.rpc(
        "session.console.create.v2",
        {
            "session": {
                "session_id": str(session_id),
                "thread_id": str(thread_id),
                "owner_id": owner_id,
                "provider": PROVIDER,
                "device_id": DEVICE_ID,
                "cwd": "/workspace/delegation-lifecycle",
                "started_at": now.isoformat(),
            }
        },
    )
    assert created["created"] is True, created
    # The transcript makes the browser workspace route use the same served
    # catalog/projector surface as production rather than a control-only shell.
    live.commit_session(
        owner_id=owner_id,
        session_id=session_id,
        device_id=DEVICE_ID,
        provider=PROVIDER,
        project="delegation-lifecycle",
        texts=("Track the background work",),
        session_facts_overrides=({"provider_session_id": provider_session_id} if provider_session_id is not None else None),
        now=now,
    )
    first = live.rpc(
        "session.console.turn.enqueue.v2",
        {
            "turn": {
                "session_id": str(session_id),
                "owner_id": owner_id,
                "message": "start the delegated work",
                "client_request_id": f"delegation-run-{session_id}",
                "report_id": None,
                "attachments": [],
                "attachments_digest": None,
                "created_at": now.isoformat(),
            }
        },
    )
    assert first["found"] is True and first["created"] is True, first
    run_id = str(first["turn"]["run_id"])
    assert run_id and run_id != "None"
    return session_id, thread_id, run_id


def _post_event(client, *, token: str, event: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        "/agents/runtime/events/batch",
        json={"events": [event]},
        headers={"X-Agents-Token": token},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _workspace_state(
    client,
    *,
    live: LiveCatalog,
    owner_id: int,
    email: str,
    session_id: UUID,
) -> dict[str, Any]:
    """Read the normal browser session-detail projection over HTTP."""

    response = client.get(
        f"/timeline/sessions/{session_id}",
        cookies={"longhouse_session": live.browser_cookie(owner_id=owner_id, email=email)},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    state = body.get("session_state")
    assert isinstance(state, dict), body
    return state


def test_live_named_delegation_lifecycle_preserves_identity_and_observation_clock(live_catalog, live_catalog_client):
    owner_email = "delegation-owner@example.test"
    owner_id = live_catalog.create_user(owner_email)
    token = live_catalog.create_device_token(owner_id=owner_id, device_id=DEVICE_ID)
    session_id, thread_id, run_id = _seed_running_session(live_catalog, owner_id=owner_id)
    base = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=30)

    _post_event(
        live_catalog_client,
        token=token,
        event=_event(
            session_id=session_id,
            thread_id=thread_id,
            run_id=run_id,
            occurred_at=base - timedelta(seconds=5),
            snapshot_observed_at=base - timedelta(seconds=5),
            dedupe_key="empty-1",
            items=[],
        ),
    )
    zero = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner_id, email=owner_email, session_id=session_id)[
        "delegation"
    ]
    assert zero["state"] == "none"
    assert zero["count"] == 0
    assert zero["kinds"] == {}
    assert zero["items"] == []

    first_observed = base
    two = [
        _task("agent-1", "subagent", "running", "Implement the backend"),
        _task("shell-1", "shell", "pending", "Run the focused checks"),
    ]
    _post_event(
        live_catalog_client,
        token=token,
        event=_event(
            session_id=session_id,
            thread_id=thread_id,
            run_id=run_id,
            occurred_at=base + timedelta(seconds=1),
            snapshot_observed_at=first_observed,
            dedupe_key="delegation-two-1",
            items=two,
        ),
    )
    pending = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner_id, email=owner_email, session_id=session_id)[
        "delegation"
    ]
    assert pending["state"] == "pending"
    assert pending["count"] == 2
    assert pending["kinds"] == {"subagent": 1, "shell": 1}
    by_id = {item["id"]: item for item in pending["items"]}
    assert set(by_id) == {"agent-1", "shell-1"}
    assert by_id["agent-1"]["status"] == "running"
    assert by_id["shell-1"]["status"] == "pending"
    assert by_id["agent-1"]["description"] == "Implement the backend"
    assert by_id["agent-1"]["first_observed_at"] == pending["observed_at"]
    assert by_id["shell-1"]["first_observed_at"] == pending["observed_at"]
    pending_observed = pending["observed_at"]
    pending_valid_until = pending["valid_until"]

    # Re-delivery of the same provider observation must not renew its lease.
    _post_event(
        live_catalog_client,
        token=token,
        event=_event(
            session_id=session_id,
            thread_id=thread_id,
            run_id=run_id,
            occurred_at=base + timedelta(seconds=1),
            snapshot_observed_at=first_observed,
            dedupe_key="delegation-two-1",
            items=two,
        ),
    )
    retried = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner_id, email=owner_email, session_id=session_id)[
        "delegation"
    ]
    assert retried["observed_at"] == pending_observed
    assert retried["valid_until"] == pending_valid_until
    assert {item["id"]: item["first_observed_at"] for item in retried["items"]} == {
        "agent-1": pending_observed,
        "shell-1": pending_observed,
    }

    # Activity is a separate fact family. A later phase signal without a
    # registry must not erase the latest accepted named-work snapshot.
    _post_event(
        live_catalog_client,
        token=token,
        event=_event(
            session_id=session_id,
            thread_id=thread_id,
            run_id=run_id,
            occurred_at=base + timedelta(seconds=21),
            dedupe_key="activity-without-registry-1",
            phase="running",
            items=None,
        ),
    )
    after_activity = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner_id, email=owner_email, session_id=session_id)[
        "delegation"
    ]
    assert after_activity["state"] == "pending"
    assert after_activity["items"] == retried["items"]
    assert after_activity["observed_at"] == pending_observed

    # The provider reports the agent complete by removing it from the active
    # registry; the shell identity and its original observation remain.
    one_at = base + timedelta(seconds=22)
    _post_event(
        live_catalog_client,
        token=token,
        event=_event(
            session_id=session_id,
            thread_id=thread_id,
            run_id=run_id,
            occurred_at=one_at,
            snapshot_observed_at=one_at,
            dedupe_key="delegation-one-1",
            items=[_task("shell-1", "shell", "running", "Run the focused checks")],
        ),
    )
    one = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner_id, email=owner_email, session_id=session_id)[
        "delegation"
    ]
    assert one["state"] == "pending"
    assert one["count"] == 1
    assert one["kinds"] == {"shell": 1}
    assert [item["id"] for item in one["items"]] == ["shell-1"]
    assert one["items"][0]["status"] == "running"
    assert one["items"][0]["first_observed_at"] == pending_observed

    empty_at = base + timedelta(seconds=23)
    _post_event(
        live_catalog_client,
        token=token,
        event=_event(
            session_id=session_id,
            thread_id=thread_id,
            run_id=run_id,
            occurred_at=empty_at,
            snapshot_observed_at=empty_at,
            dedupe_key="empty-2",
            items=[],
        ),
    )
    empty = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner_id, email=owner_email, session_id=session_id)[
        "delegation"
    ]
    assert empty["state"] == "none"
    assert empty["count"] == 0
    assert empty["kinds"] == {}
    assert empty["items"] == []

    # A delayed snapshot from before the explicit empty observation cannot
    # resurrect work merely because it arrived after the clear.
    delayed = _post_event(
        live_catalog_client,
        token=token,
        event=_event(
            session_id=session_id,
            thread_id=thread_id,
            run_id=run_id,
            occurred_at=base + timedelta(seconds=25),
            snapshot_observed_at=one_at,
            dedupe_key="delegation-delayed-old-1",
            items=two,
        ),
    )
    assert delayed["accepted"] == 1
    still_empty = _workspace_state(
        live_catalog_client,
        live=live_catalog,
        owner_id=owner_id,
        email=owner_email,
        session_id=session_id,
    )["delegation"]
    assert still_empty["state"] == "none"
    assert still_empty["items"] == []


def test_live_delegation_expiry_is_unknown_and_old_run_cannot_pollute_new_run(live_catalog, live_catalog_client):
    owner_email = "delegation-fence@example.test"
    owner_id = live_catalog.create_user(owner_email)
    token = live_catalog.create_device_token(owner_id=owner_id, device_id=DEVICE_ID)
    session_id, thread_id, first_run_id = _seed_running_session(live_catalog, owner_id=owner_id)
    base = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=30)

    # The source clock is already outside its one-second lease. Expiry is not
    # evidence that the provider completed the tasks.
    _post_event(
        live_catalog_client,
        token=token,
        event=_event(
            session_id=session_id,
            thread_id=thread_id,
            run_id=first_run_id,
            occurred_at=base - timedelta(minutes=2),
            snapshot_observed_at=base - timedelta(minutes=2),
            dedupe_key="delegation-stale-1",
            items=[_task("agent-1", "subagent", "running", "Stale work")],
            freshness_ms=1_000,
        ),
    )
    stale = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner_id, email=owner_email, session_id=session_id)[
        "delegation"
    ]
    assert stale["state"] == "unknown"
    assert stale["items"] is None
    assert stale["state"] != "none"

    current_at = base + timedelta(seconds=1)
    _post_event(
        live_catalog_client,
        token=token,
        event=_event(
            session_id=session_id,
            thread_id=thread_id,
            run_id=first_run_id,
            occurred_at=current_at,
            snapshot_observed_at=current_at,
            dedupe_key="delegation-first-run-1",
            items=[_task("agent-1", "subagent", "running", "First run work")],
        ),
    )

    # Queue a second Console turn, then complete the first through catalogd so
    # the real store fences the first run and allocates the next run id.
    queued = live_catalog.rpc(
        "session.console.turn.enqueue.v2",
        {
            "turn": {
                "session_id": str(session_id),
                "owner_id": owner_id,
                "message": "start the next run",
                "client_request_id": f"delegation-next-{session_id}",
                "report_id": None,
                "attachments": [],
                "attachments_digest": None,
                "created_at": (base + timedelta(seconds=2)).isoformat(),
            }
        },
    )
    assert queued["found"] is True and queued["created"] is True, queued
    completed = live_catalog.rpc(
        "session.console.turn.update.v2",
        {
            "turn": {
                "run_id": first_run_id,
                "owner_id": owner_id,
                "session_id": str(session_id),
                "thread_id": str(thread_id),
                "provider": PROVIDER,
                "device_id": DEVICE_ID,
                "state": "completed",
                "expected_state": "starting",
                "error_code": None,
                "error": None,
                "updated_at": (base + timedelta(seconds=3)).isoformat(),
            }
        },
    )
    # The queued turn has no run id until the first turn's terminal transition.
    # Use the durable next-turn receipt returned by that transition.
    assert completed["applied"] is True, completed
    next_turn = completed["next_turn"]
    assert isinstance(next_turn, dict), completed
    second_run_id = str(next_turn["run_id"])
    assert second_run_id and second_run_id != first_run_id

    second_at = base + timedelta(seconds=4)
    _post_event(
        live_catalog_client,
        token=token,
        event=_event(
            session_id=session_id,
            thread_id=thread_id,
            run_id=second_run_id,
            occurred_at=second_at,
            snapshot_observed_at=second_at,
            dedupe_key="delegation-second-run-empty-1",
            items=[],
        ),
    )
    second_empty = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner_id, email=owner_email, session_id=session_id)[
        "delegation"
    ]
    assert second_empty["state"] == "none"
    assert second_empty["items"] == []

    # Even a newer-looking old-run snapshot is fenced by the durable latest
    # run; it cannot put first-run work back into the new run.
    fenced = _post_event(
        live_catalog_client,
        token=token,
        event=_event(
            session_id=session_id,
            thread_id=thread_id,
            run_id=first_run_id,
            occurred_at=base + timedelta(seconds=5),
            snapshot_observed_at=base + timedelta(seconds=5),
            dedupe_key="delegation-old-run-late-1",
            items=[_task("agent-1", "subagent", "running", "Old run work")],
        ),
    )
    assert fenced["accepted"] == 1
    after_old_run = _workspace_state(
        live_catalog_client,
        live=live_catalog,
        owner_id=owner_id,
        email=owner_email,
        session_id=session_id,
    )["delegation"]
    assert after_old_run["state"] == "none"
    assert after_old_run["items"] == []


def test_live_named_subagent_task_enriches_exact_child_timing_and_denies_ambiguity(live_catalog, live_catalog_client):
    owner_email = "delegation-child@example.test"
    owner_id = live_catalog.create_user(owner_email)
    token = live_catalog.create_device_token(owner_id=owner_id, device_id=DEVICE_ID)
    parent_id, thread_id, run_id = _seed_running_session(
        live_catalog,
        owner_id=owner_id,
        provider_session_id="parent-native-link",
    )
    child_started = datetime.now(UTC).replace(microsecond=0)
    child = live_catalog.commit_session(
        owner_id=owner_id,
        session_id=uuid4(),
        device_id=DEVICE_ID,
        provider=PROVIDER,
        project="delegation-child",
        texts=("Complete the named worker task",),
        session_facts_overrides={
            "provider_session_id": "child-native-link",
            "is_subagent": True,
            "parent_provider_session_id": "parent-native-link",
            "parent_tool_call_id": "call-agent-1",
            "workflow_run_id": run_id,
        },
        now=child_started,
    )
    observed_at = child_started + timedelta(seconds=1)
    _post_event(
        live_catalog_client,
        token=token,
        event=_event(
            session_id=parent_id,
            thread_id=thread_id,
            run_id=run_id,
            occurred_at=observed_at,
            snapshot_observed_at=observed_at,
            dedupe_key="delegation-child-link-1",
            items=[
                _task(
                    "agent-1",
                    "subagent",
                    "running",
                    "Complete the named worker task",
                    parent_tool_call_id="call-agent-1",
                )
            ],
        ),
    )
    state = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner_id, email=owner_email, session_id=parent_id)[
        "delegation"
    ]
    task = state["items"][0]
    assert task["id"] == "agent-1"
    assert task["session_id"] == str(child.session_id)
    assert datetime.fromisoformat(task["started_at"].replace("Z", "+00:00")) == child_started
    assert datetime.fromisoformat(task["last_activity_at"].replace("Z", "+00:00")) == child_started
    assert task["first_observed_at"] == state["observed_at"]

    # A second live child with the same source pointer is an ambiguous join.
    # The projector must not guess either identity or timing.
    duplicate_started = child_started + timedelta(seconds=2)
    live_catalog.commit_session(
        owner_id=owner_id,
        session_id=uuid4(),
        device_id=DEVICE_ID,
        provider=PROVIDER,
        project="delegation-child",
        texts=("Competing named worker task",),
        session_facts_overrides={
            "provider_session_id": "child-native-link-duplicate",
            "is_subagent": True,
            "parent_provider_session_id": "parent-native-link",
            "parent_tool_call_id": "call-agent-1",
            "workflow_run_id": run_id,
        },
        now=duplicate_started,
    )
    ambiguous_state = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner_id, email=owner_email, session_id=parent_id)[
        "delegation"
    ]
    ambiguous_task = ambiguous_state["items"][0]
    assert ambiguous_task["session_id"] is None
    assert ambiguous_task["started_at"] is None
    assert ambiguous_task["last_activity_at"] is None


def test_hook_presence_serves_named_registry_without_overwriting_parent_activity(live_catalog, live_catalog_client):
    email = "delegation-presence@example.test"
    owner = live_catalog.create_user(email)
    token = live_catalog.create_device_token(owner_id=owner, device_id=DEVICE_ID)
    session_id, _, run_id = _seed_running_session(live_catalog, owner_id=owner)
    observed = datetime.now(UTC) - timedelta(seconds=5)
    headers = {"X-Agents-Token": token}
    base = {"session_id": str(session_id), "provider": PROVIDER, "run_id": run_id}
    response = live_catalog_client.post(
        "/agents/presence",
        headers=headers,
        json={
            **base,
            "state": "idle",
            "occurred_at": observed.isoformat(),
            "delegation": {
                "observed_at": observed.isoformat(),
                "items": [_task("native-child", "subagent", "running", "Check the result")],
            },
        },
    )
    assert response.status_code == 204, response.text
    response = live_catalog_client.post(
        "/agents/presence",
        headers=headers,
        json={**base, "state": "running", "tool_name": "Read", "occurred_at": (observed + timedelta(seconds=1)).isoformat()},
    )
    assert response.status_code == 204, response.text
    state = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    assert state["activity"]["state"] == "executing"
    assert state["activity"]["tool"] == "Read"
    assert state["delegation"]["kinds"] == {"subagent": 1}
    assert state["delegation"]["items"][0]["description"] == "Check the result"
    assert datetime.fromisoformat(state["delegation"]["observed_at"].replace("Z", "+00:00")) == observed
    # A Stop retained beside newer activity arrives last. Its clearing
    # registry must apply without rolling the parent's Read back to idle.
    response = live_catalog_client.post(
        "/agents/presence",
        headers=headers,
        json={
            **base,
            "state": "idle",
            "occurred_at": (observed + timedelta(milliseconds=500)).isoformat(),
            "delegation": {"items": [], "observed_at": (observed + timedelta(milliseconds=500)).isoformat()},
        },
    )
    assert response.status_code == 204, response.text
    cleared = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    assert cleared["delegation"]["state"] == "none"
    assert cleared["delegation"]["items"] == []
    assert cleared["activity"]["state"] == "executing"
    assert cleared["activity"]["tool"] == "Read"


def test_expired_empty_registry_remains_unknown_without_background_attention(live_catalog, live_catalog_client):
    email = "delegation-expired-empty@example.test"
    owner = live_catalog.create_user(email)
    token = live_catalog.create_device_token(owner_id=owner, device_id=DEVICE_ID)
    session_id, thread_id, run_id = _seed_running_session(live_catalog, owner_id=owner)
    observed = datetime.now(UTC) - timedelta(minutes=2)
    _post_event(
        live_catalog_client,
        token=token,
        event=_event(
            session_id=session_id,
            thread_id=thread_id,
            run_id=run_id,
            occurred_at=observed,
            snapshot_observed_at=observed,
            dedupe_key="empty-old",
            items=[],
            freshness_ms=1_000,
        ),
    )
    state = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    assert state["delegation"]["state"] == "unknown"
    assert state["delegation"]["items"] == []
    assert datetime.fromisoformat(state["delegation"]["observed_at"].replace("Z", "+00:00")) == observed


def test_named_registry_larger_than_scalar_fact_budget_reaches_detail(live_catalog, live_catalog_client):
    email = "delegation-large@example.test"
    owner = live_catalog.create_user(email)
    token = live_catalog.create_device_token(owner_id=owner, device_id=DEVICE_ID)
    session_id, thread_id, run_id = _seed_running_session(live_catalog, owner_id=owner)
    observed = datetime.now(UTC) - timedelta(seconds=5)
    items = [_task(f"agent-{index}", "subagent", "running", str(index) + "x" * 255) for index in range(20)]
    _post_event(
        live_catalog_client,
        token=token,
        event=_event(
            session_id=session_id,
            thread_id=thread_id,
            run_id=run_id,
            occurred_at=observed,
            dedupe_key="large-named-registry",
            items=items,
        ),
    )
    state = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    assert state["delegation"]["state"] == "pending"
    assert state["delegation"]["count"] == 20
    assert state["delegation"]["kinds"] == {"subagent": 20}
    assert {item["id"]: item["description"] for item in state["delegation"]["items"]} == {item["id"]: item["description"] for item in items}


def test_over_budget_registry_preserves_prior_evidence_and_parent_activity(live_catalog, live_catalog_client):
    email = "delegation-byte-bound@example.test"
    owner = live_catalog.create_user(email)
    token = live_catalog.create_device_token(owner_id=owner, device_id=DEVICE_ID)
    session_id, thread_id, run_id = _seed_running_session(live_catalog, owner_id=owner)
    observed = datetime.now(UTC) - timedelta(seconds=5)
    _post_event(
        live_catalog_client,
        token=token,
        event=_event(
            session_id=session_id,
            thread_id=thread_id,
            run_id=run_id,
            occurred_at=observed,
            dedupe_key="within-budget",
            items=[_task("agent-kept", "subagent", "running", "Keep original observation")],
        ),
    )
    before = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    _post_event(
        live_catalog_client,
        token=token,
        event=_event(
            session_id=session_id,
            thread_id=thread_id,
            run_id=run_id,
            occurred_at=observed + timedelta(seconds=1),
            dedupe_key="over-byte-budget",
            phase="thinking",
            items=[_task(f"agent-{index}", "subagent", "running", "界" * 512) for index in range(256)],
        ),
    )
    after = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    assert after["activity"]["state"] == "thinking"
    assert after["delegation"] == before["delegation"]
