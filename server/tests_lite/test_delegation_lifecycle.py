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
    assert by_id["agent-1"]["user_messages"] is None
    assert by_id["agent-1"]["assistant_messages"] is None
    assert by_id["agent-1"]["tool_calls"] is None
    assert by_id["shell-1"]["user_messages"] is None
    assert by_id["shell-1"]["assistant_messages"] is None
    assert by_id["shell-1"]["tool_calls"] is None
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
    child_session_id = uuid4()
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
    from zerg.services.session_pubsub import get_pubsub
    from zerg.services.session_pubsub import topic_session

    child_body = live_catalog.envelope_body(
        session_id=child_session_id,
        device_id=DEVICE_ID,
        provider=PROVIDER,
        project="delegation-child",
        texts=("Complete the named worker task",),
        now=child_started,
    )
    child_body["session"].update(
        {
            "provider_session_id": "child-native-link",
            "is_subagent": True,
            "parent_provider_session_id": "parent-native-link",
            "parent_tool_call_id": "call-agent-1",
            "workflow_run_id": run_id,
        }
    )
    render_record = child_body["render"]["records"][0]
    child_body["render"]["records"] = [
        {**render_record, "event_subordinal": 0, "role": "user"},
        {
            **render_record,
            "event_id": f"{render_record['event_id']}-assistant",
            "event_subordinal": 1,
            "role": "assistant",
            "content_text": "The worker completed a step",
        },
        {
            **render_record,
            "event_id": f"{render_record['event_id']}-tool",
            "event_subordinal": 2,
            "role": "assistant",
            "content_text": None,
            "tool_name": "Bash",
            "tool_call_id": "child-tool-call",
        },
    ]
    parent_topic = topic_session(str(parent_id))
    bus = get_pubsub()
    with bus.subscribe(parent_topic, since_seq=bus.peek_latest_seq(parent_topic)) as subscription:
        response = live_catalog_client.post(
            "/agents/storage/v2/envelopes",
            json=child_body,
            headers={"X-Agents-Token": token, "X-Longhouse-Storage-Lane": "live"},
        )
        assert response.status_code == 200, response.text
        parent_message = live_catalog.loop.run(subscription.next_message(timeout=2.0))
    assert parent_message is not None
    assert parent_message.payload["session_id"] == str(parent_id)
    assert parent_message.payload["kind"] == "ingest_dependency"
    assert parent_message.payload["dependency_session_id"] == str(child_session_id)
    state = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner_id, email=owner_email, session_id=parent_id)[
        "delegation"
    ]
    task = state["items"][0]
    assert task["id"] == "agent-1"
    assert task["session_id"] == str(child_session_id)
    assert task["user_messages"] == 1
    assert task["assistant_messages"] == 1
    assert task["tool_calls"] == 1
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
    assert ambiguous_task["user_messages"] is None
    assert ambiguous_task["assistant_messages"] is None
    assert ambiguous_task["tool_calls"] is None


def test_raw_only_child_keeps_counters_unknown_until_archive_projection(live_catalog, live_catalog_client):
    email = "raw-child@example.test"
    owner = live_catalog.create_user(email)
    token = live_catalog.create_device_token(owner_id=owner, device_id=DEVICE_ID)
    parent, thread, run = _seed_running_session(live_catalog, owner_id=owner, provider_session_id="raw-parent-native")
    _post_event(
        live_catalog_client,
        token=token,
        event=_event(
            session_id=parent,
            thread_id=thread,
            run_id=run,
            occurred_at=datetime.now(UTC),
            dedupe_key="raw-child-registry",
            items=[_task("raw-agent", "subagent", "running", "Waiting for archive projection", parent_tool_call_id="raw-parent-tool")],
        ),
    )
    child = uuid4()
    body = live_catalog.envelope_body(
        session_id=child,
        device_id=DEVICE_ID,
        provider=PROVIDER,
        texts=("Native child history",),
        project="raw-child",
    )
    body["render"] = None
    body["session"].update(
        {
            "provider_session_id": "raw-child-native",
            "is_subagent": True,
            "parent_provider_session_id": "raw-parent-native",
            "parent_tool_call_id": "raw-parent-tool",
        }
    )
    response = live_catalog_client.post(
        "/agents/storage/v2/envelopes",
        json=body,
        headers={"X-Agents-Token": token, "X-Longhouse-Storage-Lane": "live"},
    )
    assert response.status_code == 200, response.text
    task = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=parent)["delegation"]["items"][
        0
    ]
    assert task["session_id"] == str(child)
    assert task["tool_calls"] is None
    assert task["assistant_messages"] is None
    assert task["user_messages"] is None


def test_ordinary_fork_archive_does_not_wake_background_parent(live_catalog, live_catalog_client):
    from zerg.services.session_pubsub import get_pubsub
    from zerg.services.session_pubsub import topic_session

    owner = live_catalog.create_user("fork-parent@example.test")
    token = live_catalog.create_device_token(owner_id=owner, device_id=DEVICE_ID)
    parent, _, _ = _seed_running_session(live_catalog, owner_id=owner, provider_session_id="fork-parent-native")
    child = uuid4()
    body = live_catalog.envelope_body(
        session_id=child,
        device_id=DEVICE_ID,
        provider=PROVIDER,
        texts=("Continue an ordinary fork",),
        project="ordinary-fork",
    )
    body["session"].update(
        {
            "provider_session_id": "ordinary-fork-native",
            "parent_provider_session_id": "fork-parent-native",
            "is_subagent": False,
        }
    )
    bus = get_pubsub()
    topic = topic_session(str(parent))
    before = bus.peek_latest_seq(topic)
    response = live_catalog_client.post(
        "/agents/storage/v2/envelopes",
        json=body,
        headers={"X-Agents-Token": token, "X-Longhouse-Storage-Lane": "live"},
    )
    assert response.status_code == 200, response.text
    child_row = live_catalog.rpc("storage.session.read.v2", {"session_id": str(child)})["session"]
    assert child_row["subagent_parent_session_id"] == str(parent)
    assert bus.peek_latest_seq(topic) == before


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


def test_child_lifecycle_is_source_only_and_survives_registry_late_arrival(live_catalog, live_catalog_client):
    email = "delegation-lifecycle-edge@example.test"
    owner = live_catalog.create_user(email)
    token = live_catalog.create_device_token(owner_id=owner, device_id=DEVICE_ID)
    session_id, thread_id, run_id = _seed_running_session(live_catalog, owner_id=owner)
    base = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=30)
    before = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    edge = _event(
        session_id=session_id,
        thread_id=thread_id,
        run_id=run_id,
        occurred_at=base + timedelta(seconds=10),
        dedupe_key="child-completed-before-registry-delivery",
    )
    edge.update(kind="delegation_signal", phase=None)
    edge["payload"] = {
        "delegation_update": {
            "operation": "remove",
            "membership": "existing_exact_link_only",
            "item": _task("agent-done", "subagent", "completed", "Finished child", parent_tool_call_id="toolu_done"),
            "source_event": "SubagentStop",
            "source_agent_id": "agent-done",
            "observed_at": edge["occurred_at"],
        }
    }
    _post_event(live_catalog_client, token=token, event=edge)
    unmatched = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    assert unmatched["activity"] == before["activity"]
    assert unmatched["delegation"]["state"] == "unknown"
    _post_event(
        live_catalog_client,
        token=token,
        event=_event(
            session_id=session_id,
            thread_id=thread_id,
            run_id=run_id,
            occurred_at=base,
            snapshot_observed_at=base,
            dedupe_key="late-original-registry",
            items=[
                _task("agent-done", "subagent", "running", "Original child", parent_tool_call_id="toolu_done"),
                _task("shell-kept", "shell", "running", "Other work remains"),
            ],
        ),
    )
    served = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    delegation = served["delegation"]
    assert delegation["state"] == "pending"
    assert delegation["count"] == 1
    assert delegation["kinds"] == {"shell": 1}
    assert [item["id"] for item in delegation["items"]] == ["shell-kept"]
    assert datetime.fromisoformat(delegation["observed_at"].replace("Z", "+00:00")) == base
    assert datetime.fromisoformat(delegation["valid_until"].replace("Z", "+00:00")) == base + timedelta(milliseconds=120_000)


def test_presence_child_registry_without_phase_does_not_refresh_parent_activity(live_catalog, live_catalog_client):
    email = "delegation-independent-presence@example.test"
    owner = live_catalog.create_user(email)
    token = live_catalog.create_device_token(owner_id=owner, device_id=DEVICE_ID)
    session_id, thread_id, run_id = _seed_running_session(live_catalog, owner_id=owner)
    base = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=20)
    _post_event(
        live_catalog_client,
        token=token,
        event=_event(
            session_id=session_id,
            thread_id=thread_id,
            run_id=run_id,
            occurred_at=base,
            dedupe_key="parent-own-work",
            phase="thinking",
        ),
    )
    before = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    observed = base + timedelta(seconds=5)
    response = live_catalog_client.post(
        "/agents/presence",
        headers={"X-Agents-Token": token},
        json={
            "session_id": str(session_id),
            "provider": PROVIDER,
            "run_id": run_id,
            "occurred_at": observed.isoformat(),
            "delegation": {"items": [_task("child-active", "subagent", "running", "Child continues")], "observed_at": observed.isoformat()},
        },
    )
    assert response.status_code == 204, response.text
    served = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    assert served["activity"] == before["activity"]
    assert served["delegation"]["state"] == "pending"
    assert [item["id"] for item in served["delegation"]["items"]] == ["child-active"]


def test_native_completion_edge_wins_over_same_callback_running_snapshot(live_catalog, live_catalog_client):
    email = "delegation-combined-callback@example.test"
    owner = live_catalog.create_user(email)
    token = live_catalog.create_device_token(owner_id=owner, device_id=DEVICE_ID)
    session_id, thread_id, run_id = _seed_running_session(live_catalog, owner_id=owner)
    observed = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=10)
    before = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    event = _event(
        session_id=session_id,
        thread_id=thread_id,
        run_id=run_id,
        occurred_at=observed,
        dedupe_key="native-stop-with-still-running-registry",
        items=[
            _task("finished-agent", "subagent", "running", "Finishing worker"),
            _task("independent-shell", "shell", "running", "Shell continues"),
        ],
    )
    event.update(kind="delegation_signal", phase=None)
    event["payload"]["delegation_update"] = {
        "operation": "remove",
        "membership": "existing_exact_link_only",
        "item": _task("finished-agent", "subagent", "completed", "Finishing worker"),
        "source_event": "SubagentStop",
        "source_agent_id": "finished-agent",
        "observed_at": observed.isoformat(),
    }
    _post_event(live_catalog_client, token=token, event=event)
    served = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    assert served["activity"] == before["activity"]
    delegation = served["delegation"]
    assert delegation["state"] == "pending"
    assert delegation["count"] == 1
    assert delegation["kinds"] == {"shell": 1}
    assert [item["id"] for item in delegation["items"]] == ["independent-shell"]
    assert datetime.fromisoformat(delegation["observed_at"].replace("Z", "+00:00")) == observed
    assert datetime.fromisoformat(delegation["valid_until"].replace("Z", "+00:00")) == observed + timedelta(milliseconds=120_000)
    # A newer complete native observation outranks a prior callback. Keeping
    # the old edge forever would invent an irreversible client-side lifecycle.
    fresh = _event(
        session_id=session_id,
        thread_id=thread_id,
        run_id=run_id,
        occurred_at=observed + timedelta(seconds=2),
        dedupe_key="fresh-complete-native-registry",
        items=[_task("finished-agent", "subagent", "running", "Fresh provider observation")],
    )
    fresh.update(kind="delegation_signal", phase=None)
    _post_event(live_catalog_client, token=token, event=fresh)
    newer = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    assert newer["delegation"]["count"] == 1
    assert [item["id"] for item in newer["delegation"]["items"]] == ["finished-agent"]


def test_invalid_native_progress_cannot_poison_or_renew_served_registry(live_catalog, live_catalog_client):
    email = "delegation-progress-validation@example.test"
    owner = live_catalog.create_user(email)
    token = live_catalog.create_device_token(owner_id=owner, device_id=DEVICE_ID)
    session_id, thread_id, run_id = _seed_running_session(live_catalog, owner_id=owner)
    observed = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=20)
    original = _event(
        session_id=session_id,
        thread_id=thread_id,
        run_id=run_id,
        occurred_at=observed,
        dedupe_key="valid-native-progress",
        items=[_task("job-1", "subagent", "running", "Original worker")],
    )
    original["payload"]["delegation"]["items"][0]["native_progress"] = {
        "observed_at": observed.isoformat(),
        "tool_count": 14,
        "current_tool": "Read",
    }
    original.update(kind="delegation_signal", phase=None)
    _post_event(live_catalog_client, token=token, event=original)
    before = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    malformed = _event(
        session_id=session_id,
        thread_id=thread_id,
        run_id=run_id,
        occurred_at=observed + timedelta(seconds=5),
        dedupe_key="invalid-progress-counter-type",
        items=[_task("job-1", "subagent", "running", "Unvalidated update")],
    )
    malformed["payload"]["delegation"]["items"][0]["native_progress"] = {"tool_count": {"not_numeric": True}}
    malformed.update(kind="delegation_signal", phase=None)
    _post_event(live_catalog_client, token=token, event=malformed)
    after = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    assert after["delegation"] == before["delegation"]
    assert after["activity"] == before["activity"]
    assert after["delegation"]["items"][0]["native_progress"]["tool_count"] == 14


def test_terminal_jobs_remain_visible_without_claiming_active_membership(live_catalog, live_catalog_client):
    email = "delegation-terminal-history@example.test"
    owner = live_catalog.create_user(email)
    token = live_catalog.create_device_token(owner_id=owner, device_id=DEVICE_ID)
    session_id, thread_id, run_id = _seed_running_session(live_catalog, owner_id=owner)
    observed = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=10)
    before = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    event = _event(
        session_id=session_id,
        thread_id=thread_id,
        run_id=run_id,
        occurred_at=observed,
        dedupe_key="terminal-job-history",
        items=[],
    )
    event.update(kind="delegation_signal", phase=None)
    event["payload"]["delegation"]["recent_items"] = [
        {
            "id": "bg_1",
            "kind": "shell",
            "status": "failed",
            "description": "Provider command",
            "registered_at": (observed - timedelta(seconds=8)).isoformat(),
            "ended_at": (observed - timedelta(seconds=1)).isoformat(),
        }
    ]
    _post_event(live_catalog_client, token=token, event=event)
    served = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    assert served["activity"] == before["activity"]
    assert served["delegation"]["state"] == "none"
    assert served["delegation"]["count"] == 0
    assert served["delegation"]["kinds"] == {}
    assert served["delegation"]["items"] == []
    terminal = served["delegation"]["recent_items"][0]
    assert terminal["id"] == "bg_1"
    assert terminal["status"] == "failed"
    assert terminal["session_id"] is None
    assert datetime.fromisoformat(terminal["ended_at"].replace("Z", "+00:00")) == observed - timedelta(seconds=1)
    expired = _event(
        session_id=session_id,
        thread_id=thread_id,
        run_id=run_id,
        occurred_at=observed + timedelta(seconds=1),
        dedupe_key="expired-terminal-observation",
        items=[],
        freshness_ms=1,
    )
    expired.update(kind="delegation_signal", phase=None)
    expired["payload"]["delegation"]["recent_items"] = event["payload"]["delegation"]["recent_items"]
    _post_event(live_catalog_client, token=token, event=expired)
    old = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    assert old["delegation"]["state"] == "unknown"
    assert old["delegation"]["recent_items"][0]["status"] == "failed"
    assert old["activity"] == before["activity"]


def test_bad_recent_history_does_not_discard_a_valid_active_observation(live_catalog, live_catalog_client):
    email = "delegation-recent-isolation@example.test"
    owner = live_catalog.create_user(email)
    token = live_catalog.create_device_token(owner_id=owner, device_id=DEVICE_ID)
    session_id, thread_id, run_id = _seed_running_session(live_catalog, owner_id=owner)
    observed = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=10)
    event = _event(
        session_id=session_id,
        thread_id=thread_id,
        run_id=run_id,
        occurred_at=observed,
        dedupe_key="valid-active-with-bad-history",
        items=[_task("active-worker", "subagent", "running", "Current provider work")],
    )
    event.update(kind="delegation_signal", phase=None)
    event["payload"]["delegation"]["recent_items"] = [
        {"id": "done-job", "kind": "shell", "status": "completed", "ended_at": observed.isoformat()},
        {"id": "bad-job", "kind": "shell", "status": "failed", "ended_at": "not-a-date"},
    ]
    _post_event(live_catalog_client, token=token, event=event)
    served = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    assert served["delegation"]["count"] == 1
    assert served["delegation"]["kinds"] == {"subagent": 1}
    assert [item["id"] for item in served["delegation"]["items"]] == ["active-worker"]
    assert [item["id"] for item in served["delegation"]["recent_items"]] == ["done-job"]


def test_child_tool_hooks_update_native_progress_and_stream_delta_without_resurrecting_stopped_subagent(
    live_catalog,
    live_catalog_client,
):
    from zerg.services.live_catalog_timeline import TimelineSessionListParams
    from zerg.services.live_catalog_timeline import list_live_catalog_timeline
    from zerg.services.live_catalog_timeline import project_machine_session_delta

    email = "delegation-child-tool-progress@example.test"
    owner = live_catalog.create_user(email)
    token = live_catalog.create_device_token(owner_id=owner, device_id=DEVICE_ID)
    session_id, _thread_id, run_id = _seed_running_session(live_catalog, owner_id=owner)
    base = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=30)

    # Parent posts an idle stop with one running background subagent.
    parent_stop = live_catalog_client.post(
        "/agents/presence",
        headers={"X-Agents-Token": token},
        json={
            "session_id": str(session_id),
            "provider": PROVIDER,
            "run_id": run_id,
            "state": "idle",
            "occurred_at": base.isoformat(),
            "dedupe_key": "parent-stop-with-bg-subagent",
            "delegation": {
                "items": [_task("agent-bg-1", "subagent", "running", "Wire terminal-truth into CI")],
                "observed_at": base.isoformat(),
            },
        },
    )
    assert parent_stop.status_code == 204, parent_stop.text
    initial = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    initial_activity = initial["activity"]
    initial_valid_until = initial["delegation"]["valid_until"]

    # Child PreToolUse reports live tool execution without touching parent activity or renewing delegation TTL.
    t_pre = base + timedelta(seconds=4)
    child_pre = live_catalog_client.post(
        "/agents/presence",
        headers={"X-Agents-Token": token},
        json={
            "session_id": str(session_id),
            "provider": PROVIDER,
            "run_id": run_id,
            "occurred_at": t_pre.isoformat(),
            "dedupe_key": "child-pre-bash-1",
            "delegation_update": {
                "operation": "observe",
                "membership": "existing_exact_link_only",
                "source_event": "PreToolUse",
                "source_agent_id": "agent-bg-1",
                "observed_at": t_pre.isoformat(),
                "item": {
                    "id": "agent-bg-1",
                    "kind": "subagent",
                    "status": "running",
                    "parent_tool_call_id": "toolu_bg_1",
                    "native_progress": {
                        "observed_at": t_pre.isoformat(),
                        "status": "running",
                        "current_tool": "Bash",
                    },
                },
            },
        },
    )
    assert child_pre.status_code == 204, child_pre.text
    after_pre = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    assert after_pre["activity"] == initial_activity
    assert after_pre["delegation"]["valid_until"] == initial_valid_until
    progress_pre = after_pre["delegation"]["items"][0]["native_progress"]
    assert progress_pre["observed_at"] == t_pre.isoformat().replace("+00:00", "Z")
    assert progress_pre["status"] == "running"
    assert progress_pre["current_tool"] == "Bash"

    # Canonical machine session_delta includes compact delegation + native_progress for menu bar rendering.
    timeline_page = list_live_catalog_timeline(
        params=TimelineSessionListParams(
            project=None,
            provider=None,
            environment=None,
            include_test=True,
            hide_autonomous=False,
            include_automation=True,
            include_hidden=True,
            device_id=None,
            days_back=7,
            query=None,
            limit=20,
            offset=0,
            sort="recent",
            mode="all",
            context_mode="all",
        ),
        owner_id=owner,
    )
    timeline_row = next(card.head for card in timeline_page.sessions if str(card.head.id) == str(session_id))
    delta = project_machine_session_delta(timeline_row, commit_seq=1, canonical=True)
    assert delta["delegation"]["state"] == "pending"
    assert delta["delegation"]["count"] == 1
    assert delta["delegation"]["kinds"] == {"subagent": 1}
    assert delta["delegation"]["items"][0]["description"] == "Wire terminal-truth into CI"
    assert delta["delegation"]["items"][0]["native_progress"]["current_tool"] == "Bash"

    # A subsequent parent registry snapshot (which lacks native_progress) preserves the running child's progress.
    t_parent = base + timedelta(seconds=8)
    parent_refresh = live_catalog_client.post(
        "/agents/presence",
        headers={"X-Agents-Token": token},
        json={
            "session_id": str(session_id),
            "provider": PROVIDER,
            "run_id": run_id,
            "occurred_at": t_parent.isoformat(),
            "dedupe_key": "parent-registry-refresh",
            "delegation": {
                "items": [_task("agent-bg-1", "subagent", "running", "Wire terminal-truth into CI")],
                "observed_at": t_parent.isoformat(),
            },
        },
    )
    assert parent_refresh.status_code == 204, parent_refresh.text
    after_parent_refresh = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    assert after_parent_refresh["delegation"]["items"][0]["native_progress"]["current_tool"] == "Bash"

    # Child PostToolUse transitions native_progress back to thinking (clearing current_tool).
    t_post = base + timedelta(seconds=10)
    child_post = live_catalog_client.post(
        "/agents/presence",
        headers={"X-Agents-Token": token},
        json={
            "session_id": str(session_id),
            "provider": PROVIDER,
            "run_id": run_id,
            "occurred_at": t_post.isoformat(),
            "dedupe_key": "child-post-bash-1",
            "delegation_update": {
                "operation": "observe",
                "membership": "existing_exact_link_only",
                "source_event": "PostToolUse",
                "source_agent_id": "agent-bg-1",
                "observed_at": t_post.isoformat(),
                "item": {
                    "id": "agent-bg-1",
                    "kind": "subagent",
                    "status": "running",
                    "native_progress": {
                        "observed_at": t_post.isoformat(),
                        "status": "thinking",
                    },
                },
            },
        },
    )
    assert child_post.status_code == 204, child_post.text
    after_post = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    progress_post = after_post["delegation"]["items"][0]["native_progress"]
    assert progress_post["observed_at"] == t_post.isoformat().replace("+00:00", "Z")
    assert progress_post["status"] == "thinking"
    assert progress_post["current_tool"] is None

    # SubagentStop removes the subagent cleanly, and a late straggler PostToolUse cannot resurrect it.
    t_stop = base + timedelta(seconds=12)
    child_stop = live_catalog_client.post(
        "/agents/presence",
        headers={"X-Agents-Token": token},
        json={
            "session_id": str(session_id),
            "provider": PROVIDER,
            "run_id": run_id,
            "occurred_at": t_stop.isoformat(),
            "dedupe_key": "child-stop-1",
            "delegation_update": {
                "operation": "remove",
                "membership": "existing_exact_link_only",
                "source_event": "SubagentStop",
                "source_agent_id": "agent-bg-1",
                "observed_at": t_stop.isoformat(),
                "item": {
                    "id": "agent-bg-1",
                    "kind": "subagent",
                    "status": "completed",
                },
            },
        },
    )
    assert child_stop.status_code == 204, child_stop.text
    after_stop = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    assert after_stop["delegation"]["state"] == "none"
    assert after_stop["delegation"]["count"] == 0

    t_straggler = base + timedelta(seconds=13)
    straggler = live_catalog_client.post(
        "/agents/presence",
        headers={"X-Agents-Token": token},
        json={
            "session_id": str(session_id),
            "provider": PROVIDER,
            "run_id": run_id,
            "occurred_at": t_straggler.isoformat(),
            "dedupe_key": "child-straggler-post-1",
            "delegation_update": {
                "operation": "observe",
                "membership": "existing_exact_link_only",
                "source_event": "PostToolUse",
                "source_agent_id": "agent-bg-1",
                "observed_at": t_straggler.isoformat(),
                "item": {
                    "id": "agent-bg-1",
                    "kind": "subagent",
                    "status": "running",
                    "native_progress": {
                        "observed_at": t_straggler.isoformat(),
                        "status": "thinking",
                    },
                },
            },
        },
    )
    assert straggler.status_code == 204, straggler.text
    after_straggler = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    assert after_straggler["delegation"]["state"] == "none"
    assert after_straggler["delegation"]["count"] == 0


def test_claude_mod_state_monitors_waiting_subagents_and_hud_projection(live_catalog, live_catalog_client):
    email = "claude-hud-owner@example.test"
    owner = live_catalog.create_user(email)
    token = live_catalog.create_device_token(owner_id=owner, device_id=DEVICE_ID)
    session_id, _thread_id, run_id = _seed_running_session(live_catalog, owner_id=owner)
    base = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=20)
    started_ms = int((base - timedelta(seconds=65)).timestamp() * 1000)

    mod_resp = live_catalog_client.post(
        "/agents/presence",
        headers={"X-Agents-Token": token},
        json={
            "session_id": str(session_id),
            "provider": PROVIDER,
            "run_id": run_id,
            "occurred_at": base.isoformat(),
            "hook_event_name": "ClaudeModState",
            "dedupe_key": "mod-state-hud-1",
            "delegation": {
                "observed_at": base.isoformat(),
                "freshness_ms": 120_000,
                "count": 2,
                "kinds": {"monitor": 1, "subagent": 1},
                "items": [
                    {
                        "id": "sh-mon",
                        "kind": "monitor",
                        "status": "running",
                        "description": "Watch canary rollout",
                    },
                    {
                        "id": "agent-w1",
                        "kind": "subagent",
                        "status": "waiting",
                        "description": "Inspect hooks",
                        "agent_type": "Explore",
                        "model": "claude-opus-5-5",
                        "is_fork": True,
                        "parent_agent_id": "root-agent",
                        "spawn_depth": 2,
                        "native_progress": {
                            "observed_at": base.isoformat(),
                            "status": "waiting",
                            "tool_count": 14,
                            "tokens": 4200,
                            "context_tokens": 1800,
                            "duration_ms": 12500,
                        },
                    },
                ],
            },
            "session_usage": {
                "started_at": started_ms,
                "context": {"tokens": 120_000, "window": 1_000_000, "percent": 12.0},
                "cost_usd": 14.5,
                "rate_limits": [{"kind": "five_hour", "percent_used": 22.0, "resets_at": "2026-11-15T08:00:00Z"}],
            },
            "session_crons": [{"id": "cron-1", "schedule": "*/5 * * * *", "recurring": True, "prompt": "check"}],
            "permission_mode": "bypassPermissions",
        },
    )
    assert mod_resp.status_code == 204, mod_resp.text

    cwd_ts = base + timedelta(seconds=2)
    cwd_resp = live_catalog_client.post(
        "/agents/presence",
        headers={"X-Agents-Token": token},
        json={
            "session_id": str(session_id),
            "provider": PROVIDER,
            "occurred_at": cwd_ts.isoformat(),
            "hook_event_name": "CwdChanged",
            "cwd": "/workspace/delegation-lifecycle/subdir",
            "git_branch": "feat/claude-fidelity",
            "dedupe_key": "cwd-changed-1",
        },
    )
    assert cwd_resp.status_code == 204, cwd_resp.text

    cookies = {"longhouse_session": live_catalog.browser_cookie(owner_id=owner, email=email)}
    detail = live_catalog_client.get(f"/timeline/sessions/{session_id}", cookies=cookies)
    assert detail.status_code == 200, detail.text
    workspace = live_catalog_client.get(f"/timeline/sessions/{session_id}/workspace", cookies=cookies)
    assert workspace.status_code == 200, workspace.text
    mobile_tail = live_catalog_client.get(f"/timeline/sessions/{session_id}/mobile-tail", cookies=cookies)
    assert mobile_tail.status_code == 200, mobile_tail.text

    for payload_session in (detail.json(), workspace.json()["session"], mobile_tail.json()["session"]):
        assert payload_session["cwd"] == "/workspace/delegation-lifecycle/subdir"
        assert payload_session["git_branch"] == "feat/claude-fidelity"
        assert payload_session["permission_mode"] == "bypassPermissions"
        assert payload_session["session_crons"] == [{"id": "cron-1", "schedule": "*/5 * * * *", "recurring": True, "prompt": "check"}]
        assert payload_session["pending_background_agent_count"] == 1
        usage = payload_session["usage_latest"]
        assert usage["context_tokens"] == 120_000
        assert usage["context_window"] == 1_000_000
        assert usage["context_window_source"] == "native"
        assert usage["used_percent"] == 12.0
        assert usage["cost_usd"] == 14.5
        assert usage["cumulative_duration_ms"] >= 65_000
        assert usage["rate_limits"] == [{"kind": "five_hour", "percent_used": 22.0, "resets_at": "2026-11-15T08:00:00Z"}]
        delegation = payload_session["session_state"]["delegation"]
        assert delegation["state"] == "pending"
        assert delegation["count"] == 2
        assert delegation["kinds"] == {"monitor": 1, "subagent": 1}
        subagent = next(item for item in delegation["items"] if item["id"] == "agent-w1")
        assert subagent["status"] == "waiting"
        assert subagent["agent_type"] == "Explore"
        assert subagent["model"] == "claude-opus-5-5"
        assert subagent["is_fork"] is True
        assert subagent["parent_agent_id"] == "root-agent"
        assert subagent["spawn_depth"] == 2
        assert subagent["native_progress"]["tool_count"] == 14
        assert subagent["native_progress"]["tokens"] == 4200


def test_child_post_tool_clears_tool_preserves_progress_and_waiting_stop_keeps_subagent(live_catalog, live_catalog_client):
    email = "claude-waiting-stop@example.test"
    owner = live_catalog.create_user(email)
    token = live_catalog.create_device_token(owner_id=owner, device_id=DEVICE_ID)
    session_id, _thread_id, run_id = _seed_running_session(live_catalog, owner_id=owner)
    base = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=20)

    snap_resp = live_catalog_client.post(
        "/agents/presence",
        headers={"X-Agents-Token": token},
        json={
            "session_id": str(session_id),
            "provider": PROVIDER,
            "run_id": run_id,
            "occurred_at": base.isoformat(),
            "hook_event_name": "ClaudeModState",
            "dedupe_key": "snap-progress-1",
            "delegation": {
                "observed_at": base.isoformat(),
                "freshness_ms": 120_000,
                "count": 1,
                "kinds": {"subagent": 1},
                "items": [
                    {
                        "id": "a-child-1",
                        "kind": "subagent",
                        "status": "running",
                        "description": "Audit hooks",
                        "native_progress": {
                            "observed_at": base.isoformat(),
                            "status": "running",
                            "tokens": 3100,
                            "duration_ms": 4000,
                        },
                    }
                ],
            },
        },
    )
    assert snap_resp.status_code == 204, snap_resp.text

    t_pre = base + timedelta(seconds=2)
    pre_resp = live_catalog_client.post(
        "/agents/presence",
        headers={"X-Agents-Token": token},
        json={
            "session_id": str(session_id),
            "provider": PROVIDER,
            "run_id": run_id,
            "occurred_at": t_pre.isoformat(),
            "dedupe_key": "child-pre-1",
            "delegation_update": {
                "operation": "observe",
                "membership": "existing_exact_link_only",
                "source_event": "PreToolUse",
                "source_agent_id": "a-child-1",
                "observed_at": t_pre.isoformat(),
                "item": {
                    "id": "a-child-1",
                    "kind": "subagent",
                    "status": "running",
                    "native_progress": {
                        "observed_at": t_pre.isoformat(),
                        "status": "running",
                        "current_tool": "Bash",
                        "last_intent": "Run cargo test",
                    },
                },
            },
        },
    )
    assert pre_resp.status_code == 204, pre_resp.text

    t_post = base + timedelta(seconds=4)
    post_resp = live_catalog_client.post(
        "/agents/presence",
        headers={"X-Agents-Token": token},
        json={
            "session_id": str(session_id),
            "provider": PROVIDER,
            "run_id": run_id,
            "occurred_at": t_post.isoformat(),
            "dedupe_key": "child-post-1",
            "delegation_update": {
                "operation": "observe",
                "membership": "existing_exact_link_only",
                "source_event": "PostToolUse",
                "source_agent_id": "a-child-1",
                "observed_at": t_post.isoformat(),
                "item": {
                    "id": "a-child-1",
                    "kind": "subagent",
                    "status": "running",
                    "native_progress": {
                        "observed_at": t_post.isoformat(),
                        "status": "thinking",
                    },
                },
            },
        },
    )
    assert post_resp.status_code == 204, post_resp.text
    after_post = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    prog = after_post["delegation"]["items"][0]["native_progress"]
    assert prog["current_tool"] is None
    assert prog["last_intent"] == "Run cargo test"
    assert prog["tool_count"] == 1
    assert prog["tokens"] == 3100
    assert prog["duration_ms"] == 4000

    t_wait = base + timedelta(seconds=6)
    wait_stop = live_catalog_client.post(
        "/agents/presence",
        headers={"X-Agents-Token": token},
        json={
            "session_id": str(session_id),
            "provider": PROVIDER,
            "run_id": run_id,
            "occurred_at": t_wait.isoformat(),
            "dedupe_key": "child-wait-stop-1",
            "delegation_update": {
                "operation": "observe",
                "membership": "existing_exact_link_only",
                "source_event": "SubagentStop",
                "source_agent_id": "a-child-1",
                "observed_at": t_wait.isoformat(),
                "item": {
                    "id": "a-child-1",
                    "kind": "subagent",
                    "status": "waiting",
                    "agent_type": "Explore",
                    "spawn_depth": 2,
                    "native_progress": {
                        "observed_at": t_wait.isoformat(),
                        "status": "waiting",
                    },
                },
            },
        },
    )
    assert wait_stop.status_code == 204, wait_stop.text
    after_wait = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    assert after_wait["delegation"]["state"] == "pending"
    assert after_wait["delegation"]["count"] == 1
    item = after_wait["delegation"]["items"][0]
    assert item["status"] == "waiting"
    assert item["agent_type"] == "Explore"
    assert item["spawn_depth"] == 2
    assert item["native_progress"]["tool_count"] == 1
    assert item["native_progress"]["last_intent"] == "Run cargo test"


def test_task_created_upserts_and_task_completed_moves_to_recent_items(live_catalog, live_catalog_client):
    email = "claude-tasks-owner@example.test"
    owner = live_catalog.create_user(email)
    token = live_catalog.create_device_token(owner_id=owner, device_id=DEVICE_ID)
    session_id, _thread_id, run_id = _seed_running_session(live_catalog, owner_id=owner)
    base = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=15)

    created = live_catalog_client.post(
        "/agents/presence",
        headers={"X-Agents-Token": token},
        json={
            "session_id": str(session_id),
            "provider": PROVIDER,
            "run_id": run_id,
            "occurred_at": base.isoformat(),
            "dedupe_key": "task-created-1",
            "delegation_update": {
                "operation": "observe",
                "membership": "upsert",
                "source_event": "TaskCreated",
                "observed_at": base.isoformat(),
                "item": {
                    "id": "task-1",
                    "kind": "other",
                    "status": "running",
                    "description": "Run verification",
                },
            },
        },
    )
    assert created.status_code == 204, created.text
    after_create = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    assert after_create["delegation"]["state"] == "pending"
    assert after_create["delegation"]["count"] == 1
    assert after_create["delegation"]["items"][0]["id"] == "task-1"
    assert after_create["delegation"]["items"][0]["description"] == "Run verification"

    t_done = base + timedelta(seconds=5)
    completed = live_catalog_client.post(
        "/agents/presence",
        headers={"X-Agents-Token": token},
        json={
            "session_id": str(session_id),
            "provider": PROVIDER,
            "run_id": run_id,
            "occurred_at": t_done.isoformat(),
            "dedupe_key": "task-completed-1",
            "delegation_update": {
                "operation": "remove",
                "membership": "existing_exact_link_only",
                "source_event": "TaskCompleted",
                "observed_at": t_done.isoformat(),
                "item": {
                    "id": "task-1",
                    "kind": "other",
                    "status": "completed",
                    "description": None,
                },
            },
        },
    )
    assert completed.status_code == 204, completed.text
    after_done = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    assert after_done["delegation"]["state"] == "none"
    assert after_done["delegation"]["count"] == 0
    assert after_done["delegation"]["items"] == []
    assert len(after_done["delegation"]["recent_items"]) == 1
    assert after_done["delegation"]["recent_items"][0]["id"] == "task-1"
    assert after_done["delegation"]["recent_items"][0]["status"] == "completed"
    assert after_done["delegation"]["recent_items"][0]["description"] == "Run verification"


def test_task_notification_provider_fact_retires_open_monitor_and_populates_recent_items(live_catalog, live_catalog_client):
    email = "task-notif-retire@example.test"
    owner = live_catalog.create_user(email)
    token = live_catalog.create_device_token(owner_id=owner, device_id=DEVICE_ID)
    session_id, thread_id, run_id = _seed_running_session(live_catalog, owner_id=owner)
    base = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=20)

    _post_event(
        live_catalog_client,
        token=token,
        event=_event(
            session_id=session_id,
            thread_id=thread_id,
            run_id=run_id,
            occurred_at=base,
            dedupe_key="stop-monitor-1",
            items=[_task("mon-1", "monitor", "running", "Wait for QA deploy")],
            snapshot_observed_at=base,
        ),
    )
    opened = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    assert opened["delegation"]["state"] == "pending"
    assert opened["delegation"]["count"] == 1

    t_done = base + timedelta(seconds=8)
    result = live_catalog.rpc(
        "session.provider_facts.insert.v2",
        {
            "session_id": str(session_id),
            "source_epoch": str(uuid4()),
            "provider_facts": [
                {
                    "kind": "delegation.task_notification",
                    "at": t_done.isoformat(),
                    "source_position": 1,
                    "payload": {
                        "task_id": "mon-1",
                        "status": "completed",
                        "summary": "QA deploy green",
                    },
                }
            ],
        },
    )
    assert result["delegation_reconciled"] == 1

    retired = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    assert retired["delegation"]["state"] == "none"
    assert retired["delegation"]["count"] == 0
    assert retired["delegation"]["items"] == []
    assert retired["delegation"]["recent_items"][0]["id"] == "mon-1"
    assert retired["delegation"]["recent_items"][0]["status"] == "completed"


def test_intermediate_monitor_tick_without_terminal_status_keeps_monitor_open(live_catalog, live_catalog_client):
    email = "monitor-tick-open@example.test"
    owner = live_catalog.create_user(email)
    token = live_catalog.create_device_token(owner_id=owner, device_id=DEVICE_ID)
    session_id, thread_id, run_id = _seed_running_session(live_catalog, owner_id=owner)
    base = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=20)

    _post_event(
        live_catalog_client,
        token=token,
        event=_event(
            session_id=session_id,
            thread_id=thread_id,
            run_id=run_id,
            occurred_at=base,
            dedupe_key="stop-monitor-tick-1",
            items=[_task("mon-1", "monitor", "running", "Watch CI build")],
            snapshot_observed_at=base,
        ),
    )

    result = live_catalog.rpc(
        "session.provider_facts.insert.v2",
        {
            "session_id": str(session_id),
            "source_epoch": str(uuid4()),
            "provider_facts": [
                {
                    "kind": "delegation.task_notification",
                    "at": (base + timedelta(seconds=4)).isoformat(),
                    "source_position": 1,
                    "payload": {
                        "task_id": "mon-1",
                        "summary": "still polling",
                    },
                }
            ],
        },
    )
    assert result["delegation_reconciled"] == 0

    state = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    assert state["delegation"]["state"] == "pending"
    assert state["delegation"]["count"] == 1
    assert state["delegation"]["items"][0]["id"] == "mon-1"


def test_terminal_task_notification_arriving_before_delayed_stop_retires_item_immediately(live_catalog, live_catalog_client):
    email = "notif-before-stop@example.test"
    owner = live_catalog.create_user(email)
    token = live_catalog.create_device_token(owner_id=owner, device_id=DEVICE_ID)
    session_id, thread_id, run_id = _seed_running_session(live_catalog, owner_id=owner)
    base = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=20)

    live_catalog.rpc(
        "session.provider_facts.insert.v2",
        {
            "session_id": str(session_id),
            "source_epoch": str(uuid4()),
            "provider_facts": [
                {
                    "kind": "delegation.task_notification",
                    "at": (base + timedelta(seconds=5)).isoformat(),
                    "source_position": 1,
                    "payload": {
                        "task_id": "sh-1",
                        "status": "completed",
                        "summary": "make test-frontend finished",
                    },
                }
            ],
        },
    )

    _post_event(
        live_catalog_client,
        token=token,
        event=_event(
            session_id=session_id,
            thread_id=thread_id,
            run_id=run_id,
            occurred_at=base,
            dedupe_key="delayed-stop-sh-1",
            items=[_task("sh-1", "shell", "running", "make test-frontend")],
            snapshot_observed_at=base,
        ),
    )

    state = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    assert state["delegation"]["state"] == "none"
    assert state["delegation"]["count"] == 0
    assert state["delegation"]["recent_items"][0]["id"] == "sh-1"
    assert state["delegation"]["recent_items"][0]["status"] == "completed"


def test_claude_mod_state_sync_agents_empty_preserves_running_shell(live_catalog, live_catalog_client):
    email = "mod-sync-preserves-shell@example.test"
    owner = live_catalog.create_user(email)
    token = live_catalog.create_device_token(owner_id=owner, device_id=DEVICE_ID)
    session_id, thread_id, run_id = _seed_running_session(live_catalog, owner_id=owner)
    base = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=20)

    _post_event(
        live_catalog_client,
        token=token,
        event=_event(
            session_id=session_id,
            thread_id=thread_id,
            run_id=run_id,
            occurred_at=base,
            dedupe_key="stop-shell-keep",
            items=[_task("sh-1", "shell", "running", "lockf build")],
            snapshot_observed_at=base,
        ),
    )

    t_sync = base + timedelta(seconds=3)
    sync_resp = live_catalog_client.post(
        "/agents/presence",
        headers={"X-Agents-Token": token},
        json={
            "session_id": str(session_id),
            "provider": PROVIDER,
            "run_id": run_id,
            "hook_event_name": "ClaudeModState",
            "occurred_at": t_sync.isoformat(),
            "dedupe_key": "mod-sync-empty-1",
            "delegation_update": {
                "operation": "sync_agents",
                "source_event": "ClaudeModState",
                "observed_at": t_sync.isoformat(),
                "items": [],
            },
        },
    )
    assert sync_resp.status_code == 204, sync_resp.text

    state = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    assert state["delegation"]["state"] == "pending"
    assert state["delegation"]["count"] == 1
    assert state["delegation"]["items"][0]["id"] == "sh-1"


def test_claude_mod_state_sync_agents_retires_subagent_while_preserving_shell(live_catalog, live_catalog_client):
    email = "mod-sync-mixed@example.test"
    owner = live_catalog.create_user(email)
    token = live_catalog.create_device_token(owner_id=owner, device_id=DEVICE_ID)
    session_id, thread_id, run_id = _seed_running_session(live_catalog, owner_id=owner)
    base = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=20)

    _post_event(
        live_catalog_client,
        token=token,
        event=_event(
            session_id=session_id,
            thread_id=thread_id,
            run_id=run_id,
            occurred_at=base,
            dedupe_key="stop-shell-mixed",
            items=[_task("sh-1", "shell", "running", "lockf build")],
            snapshot_observed_at=base,
        ),
    )

    t_add = base + timedelta(seconds=2)
    add_resp = live_catalog_client.post(
        "/agents/presence",
        headers={"X-Agents-Token": token},
        json={
            "session_id": str(session_id),
            "provider": PROVIDER,
            "run_id": run_id,
            "hook_event_name": "ClaudeModState",
            "occurred_at": t_add.isoformat(),
            "dedupe_key": "mod-sync-add-1",
            "delegation_update": {
                "operation": "sync_agents",
                "source_event": "ClaudeModState",
                "observed_at": t_add.isoformat(),
                "items": [
                    {
                        "id": "ag-1",
                        "kind": "subagent",
                        "status": "running",
                        "description": "Audit spacing",
                    }
                ],
            },
        },
    )
    assert add_resp.status_code == 204, add_resp.text
    after_add = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    assert after_add["delegation"]["count"] == 2

    t_clear = base + timedelta(seconds=6)
    clear_resp = live_catalog_client.post(
        "/agents/presence",
        headers={"X-Agents-Token": token},
        json={
            "session_id": str(session_id),
            "provider": PROVIDER,
            "run_id": run_id,
            "hook_event_name": "ClaudeModState",
            "occurred_at": t_clear.isoformat(),
            "dedupe_key": "mod-sync-clear-1",
            "delegation_update": {
                "operation": "sync_agents",
                "source_event": "ClaudeModState",
                "observed_at": t_clear.isoformat(),
                "items": [],
            },
        },
    )
    assert clear_resp.status_code == 204, clear_resp.text
    after_clear = _workspace_state(live_catalog_client, live=live_catalog, owner_id=owner, email=email, session_id=session_id)
    assert after_clear["delegation"]["count"] == 1
    assert after_clear["delegation"]["items"][0]["id"] == "sh-1"
    assert after_clear["delegation"]["recent_items"][0]["id"] == "ag-1"
    assert after_clear["delegation"]["recent_items"][0]["status"] == "completed"
