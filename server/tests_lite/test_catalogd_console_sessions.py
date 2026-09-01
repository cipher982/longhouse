import json
import os
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.catalogd.fact_reducer import read_session_fact_heads
from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.store import CatalogStore
from zerg.models.live_store import LiveArchiveOutbox
from zerg.models.live_store import LiveConsoleTurn
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionInputReceipt
from zerg.models.live_store import LiveSessionLaunchAttempt
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.models.live_store import LiveSessionThreadAlias
from zerg.models.live_store import LiveSession
from zerg.models.live_store import LiveUser
from zerg.services.agents.session_graph_writes import primary_thread_id_for_session
from zerg.services.console_turns import CatalogConsoleTurn
from zerg.services.live_catalog_timeline import project_catalog_session_facts
from zerg.services.session_runtime import RuntimeEventIngest


def test_catalog_console_session_is_idle_identity_not_launch(tmp_path):
    engine = create_catalog_engine(tmp_path / "catalog.db")
    initialize_catalog_schema(engine)
    session_id = uuid4()
    thread_id = uuid4()
    data = {
        "session_id": str(session_id),
        "thread_id": str(thread_id),
        "owner_id": 1,
        "provider": "codex",
        "device_id": "cinder",
        "cwd": "/tmp/longhouse",
        "project": "longhouse",
        "provider_config": {"permission_mode": "bypass"},
        "started_at": datetime.now(UTC),
    }

    result = CatalogStore(engine).create_console_session(data=data)

    assert result["created"] is True
    with Session(engine) as db:
        session = db.get(LiveSessionCatalog, str(session_id))
        thread = db.get(LiveSessionThread, str(thread_id))
        assert session.primary_thread_id == str(thread_id)
        # Empty/content admission is a timeline concern, not an origin bit.
        assert session.hidden_from_default_timeline == 0
        assert thread.device_id == "cinder"
        assert thread.cwd == "/tmp/longhouse"
        assert db.query(LiveSessionRun).count() == 0
        assert db.query(LiveSessionLaunchAttempt).count() == 0
        assert db.query(LiveArchiveOutbox).count() == 1

    replay = CatalogStore(engine).create_console_session(data=data)
    assert replay["created"] is False
    assert replay["exact_replay"] is True


def test_catalog_test_console_session_retains_automation_provenance(tmp_path):
    engine = create_catalog_engine(tmp_path / "catalog-automation.db")
    initialize_catalog_schema(engine)
    session_id = uuid4()
    thread_id = uuid4()

    CatalogStore(engine).create_console_session(
        data={
            "session_id": str(session_id),
            "thread_id": str(thread_id),
            "owner_id": 1,
            "provider": "codex",
            "device_id": "provider-factory-resume",
            "cwd": "/tmp/provider-factory",
            "project": "provider-console-codex",
            "launch_actor": "automation",
            "launch_surface": "test",
            "started_at": datetime.now(UTC),
        }
    )

    with Session(engine) as db:
        session = db.get(LiveSessionCatalog, str(session_id))
        assert session.environment == "test"
        assert session.origin_kind == "console"
        assert session.hidden_from_default_timeline == 1
        assert session.launch_actor == "automation"
        assert session.launch_surface == "test"


def test_console_create_outbox_is_exact_fail_closed_owner_evidence(tmp_path):
    engine = create_catalog_engine(tmp_path / "catalog-console-owner.db")
    initialize_catalog_schema(engine)
    now = datetime.now(UTC)
    matched_id = str(uuid4())
    wrong_key_id = str(uuid4())
    non_console_id = str(uuid4())
    with Session(engine) as db:
        db.add_all(
            [
                LiveUser(id=1, email="owner@example.com", is_active=True),
                LiveUser(id=42, email="other@example.com", is_active=True),
                LiveSessionCatalog(
                    session_id=matched_id,
                    provider="codex",
                    environment="production",
                    origin_kind="console",
                    started_at=now,
                ),
                LiveSessionCatalog(
                    session_id=wrong_key_id,
                    provider="codex",
                    environment="production",
                    origin_kind="console",
                    started_at=now,
                ),
                LiveSessionCatalog(
                    session_id=non_console_id,
                    provider="codex",
                    environment="production",
                    origin_kind="local",
                    started_at=now,
                ),
                LiveArchiveOutbox(
                    idempotency_key=f"console_session_create.v1:{matched_id}",
                    kind="console_session_create.v1",
                    payload_json=json.dumps({"session": {"owner_id": 1}}),
                ),
                LiveArchiveOutbox(
                    idempotency_key=f"console_session_create.v1:{wrong_key_id}:wrong",
                    kind="console_session_create.v1",
                    payload_json=json.dumps({"session": {"owner_id": 1}}),
                ),
                LiveArchiveOutbox(
                    idempotency_key=f"console_session_create.v1:{non_console_id}",
                    kind="console_session_create.v1",
                    payload_json=json.dumps({"session": {"owner_id": 1}}),
                ),
            ]
        )
        db.commit()

    store = CatalogStore(engine)
    with engine.connect() as connection:
        assert store._session_explicitly_belongs_to_owner(connection, session_id=matched_id, owner_id=1) is True
        assert store._session_explicitly_belongs_to_owner(connection, session_id=matched_id, owner_id=42) is False
        assert store._session_explicitly_belongs_to_owner(connection, session_id=wrong_key_id, owner_id=1) is False
        assert store._session_explicitly_belongs_to_owner(connection, session_id=non_console_id, owner_id=1) is False


def test_catalog_console_turns_claim_and_wake_fifo(tmp_path, monkeypatch):
    engine = create_catalog_engine(tmp_path / "catalog-turns.db")
    initialize_catalog_schema(engine)
    store = CatalogStore(engine)
    session_id = uuid4()
    thread_id = uuid4()
    with Session(engine) as db:
        db.add_all(
            [
                LiveUser(id=1, email="owner@example.com", is_active=True),
                LiveUser(id=42, email="other@example.com", is_active=True),
            ]
        )
        db.commit()
    store.create_console_session(
        data={
            "session_id": str(session_id),
            "thread_id": str(thread_id),
            "owner_id": 1,
            "provider": "codex",
            "device_id": "cinder",
            "cwd": "/tmp/longhouse",
            "project": "longhouse",
            "provider_config": {"permission_mode": "bypass"},
            "started_at": datetime.now(UTC),
        }
    )
    turn_identity = {
        "owner_id": 1,
        "session_id": str(session_id),
        "thread_id": str(thread_id),
        "provider": "codex",
        "device_id": "cinder",
    }

    class _ConsoleRegistry:
        @staticmethod
        def is_online(*, owner_id, device_id):
            return owner_id == 1 and device_id == "cinder"

        @staticmethod
        def supports(*, owner_id, device_id, capability):
            return owner_id == 1 and device_id == "cinder" and capability == "codex.turn_start"

    monkeypatch.setattr(
        "zerg.services.live_catalog_timeline.get_machine_control_channel_registry",
        lambda: _ConsoleRegistry(),
    )
    empty_read = store.read_session(session_id=str(session_id), owner_id=1)
    empty_state = project_catalog_session_facts(
        empty_read["facts"], observed_at=datetime.fromisoformat(empty_read["observed_at"])
    ).session_state
    assert empty_state.transcript.convergence == "current"
    assert empty_state.presentation.access.key == "live_control"
    assert empty_state.control is not None
    assert empty_state.control.actions.start_turn.state == "available"
    unauthorized = store.enqueue_console_turn(
        data={
            "session_id": str(session_id),
            "owner_id": 42,
            "message": "not yours",
            "client_request_id": "wrong-owner",
            "created_at": datetime.now(UTC),
        }
    )
    assert unauthorized == {"found": False}
    first = store.enqueue_console_turn(
        data={
            "session_id": str(session_id),
            "owner_id": 1,
            "message": "first",
            "client_request_id": "request-1",
            "created_at": datetime.now(UTC),
        }
    )
    second = store.enqueue_console_turn(
        data={
            "session_id": str(session_id),
            "owner_id": 1,
            "message": "second",
            "client_request_id": "request-2",
            "created_at": datetime.now(UTC),
        }
    )
    assert first["turn"]["state"] == "starting"
    assert first["turn"]["run_id"]
    assert first["turn"]["client_request_id"] == "request-1"
    assert second["turn"]["state"] == "queued"
    assert second["turn"]["run_id"] is None
    assert second["turn"]["client_request_id"] == "request-2"
    starting = store.list_starting_console_turns_for_device(owner_id=1, device_id="cinder")
    assert [turn["run_id"] for turn in starting["turns"]] == [first["turn"]["run_id"]]
    assert store.list_starting_console_turns_for_device(owner_id=42, device_id="cinder")["turns"] == []
    assert store.list_starting_console_turns_for_device(owner_id=1, device_id="cube")["turns"] == []
    current = store.read_current_console_turn(session_id=str(session_id), owner_id=1)
    assert current["found"] is True
    assert current["turn"]["turn_id"] == first["turn"]["turn_id"]
    assert store.read_current_console_turn(session_id=str(session_id), owner_id=42) == {"found": False}
    facts = store.read_session(session_id=str(session_id), owner_id=1)["facts"]
    assert facts["latest_console_turn"]["state"] == "starting"
    starting_read = store.read_shadow_session_state(session_id=str(session_id), owner_id=1)
    starting_state = project_catalog_session_facts(
        starting_read["legacy_facts"],
        observed_at=datetime.fromisoformat(starting_read["observed_at"]),
        canonical_heads=starting_read["heads"],
        commit_seq=int(starting_read["commit_seq"]),
    ).session_state
    assert starting_state.activity.state == "unknown"
    assert starting_state.run is not None and starting_state.run.lifecycle == "starting"
    assert starting_state.working_set == "open"
    assert starting_state.presentation.primary is not None
    assert starting_state.presentation.primary.label == "Starting"
    assert starting_state.presentation.access.key == "live_control"

    mismatches = {
        "owner_id": 42,
        "session_id": str(uuid4()),
        "thread_id": str(uuid4()),
        "provider": "claude",
        "device_id": "cube",
    }
    for field, value in mismatches.items():
        refused = store.update_console_turn(
            data={
                **turn_identity,
                field: value,
                "turn_id": first["turn"]["turn_id"],
                "run_id": first["turn"]["run_id"],
                "state": "starting",
                "expected_state": "starting",
                "updated_at": datetime.now(UTC),
            }
        )
        assert refused == {"found": False}

    uncertain = store.update_console_turn(
        data={
            **turn_identity,
            "turn_id": first["turn"]["turn_id"],
            "run_id": first["turn"]["run_id"],
            "state": "starting",
            "expected_state": "starting",
            "error_code": "turn_start_outcome_unknown",
            "error": "Machine control channel disconnected",
            "updated_at": datetime.now(UTC),
        }
    )
    assert uncertain["turn"]["state"] == "starting"
    with Session(engine) as db:
        uncertain_turn = db.get(LiveConsoleTurn, first["turn"]["turn_id"])
        uncertain_receipt = db.get(LiveSessionInputReceipt, uncertain_turn.receipt_id)
        assert uncertain_receipt.status == "delivering"
        assert json.loads(uncertain_receipt.error_json) == {
            "code": "turn_start_outcome_unknown",
            "message": "Machine control channel disconnected",
        }
    active = store.update_console_turn(
        data={
            **turn_identity,
            "turn_id": first["turn"]["turn_id"],
            "run_id": first["turn"]["run_id"],
            "state": "active",
            "expected_state": "starting",
            "updated_at": datetime.now(UTC),
        }
    )
    assert active["turn"]["state"] == "active"
    with Session(engine) as db:
        active_turn = db.get(LiveConsoleTurn, first["turn"]["turn_id"])
        active_receipt = db.get(LiveSessionInputReceipt, active_turn.receipt_id)
        assert active_receipt.error_json is None
    pre_phase_read = store.read_shadow_session_state(session_id=str(session_id), owner_id=1)
    pre_phase_state = project_catalog_session_facts(
        pre_phase_read["legacy_facts"],
        observed_at=datetime.fromisoformat(pre_phase_read["observed_at"]),
        canonical_heads=pre_phase_read["heads"],
        commit_seq=int(pre_phase_read["commit_seq"]),
    ).session_state
    assert pre_phase_state.activity.state == "unknown"
    assert pre_phase_state.run is not None and pre_phase_state.run.lifecycle == "running"
    assert pre_phase_state.working_set == "open"
    assert pre_phase_state.presentation.primary is not None
    assert pre_phase_state.presentation.primary.label == "Working"
    assert pre_phase_state.presentation.access.key == "live_control"
    phase_at = datetime.now(UTC)
    runtime_result = store.apply_session_runtime(
        events=[
            RuntimeEventIngest(
                runtime_key=f"codex:{session_id}",
                session_id=session_id,
                thread_id=thread_id,
                run_id=first["turn"]["run_id"],
                provider="codex",
                device_id="cinder",
                source="codex_exec",
                kind="phase_signal",
                phase="thinking",
                occurred_at=phase_at,
                dedupe_key=f"phase:{first['turn']['run_id']}:thinking",
            )
        ]
    )
    assert runtime_result["activity_facts"]["changed_heads"] == 1
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN")
        _commit_seq, heads = read_session_fact_heads(connection, session_id=str(session_id))
        connection.rollback()
    activity = next(head for head in heads if head["family"] == "activity")
    assert json.loads(activity["value_json"])["kind"] == "thinking"
    assert json.loads(activity["value_json"])["run_id"] == first["turn"]["run_id"]
    active_read = store.read_shadow_session_state(session_id=str(session_id), owner_id=1)
    active_state = project_catalog_session_facts(
        active_read["legacy_facts"],
        observed_at=datetime.fromisoformat(active_read["observed_at"]),
        canonical_heads=active_read["heads"],
        commit_seq=int(active_read["commit_seq"]),
    ).session_state
    assert active_state.activity.state in {"thinking", "executing"}
    assert active_state.presentation.access.key == "live_control"
    assert active_state.control is not None
    assert active_state.control.actions.start_turn.state == "available"
    assert active_state.control.actions.interrupt.state == "unavailable"
    assert active_state.control.actions.interrupt.reason == "unsupported"
    provider_thread_id = "019f6b93-edf6-7bd0-a757-b5195a61abdd"
    store.apply_session_runtime(
        events=[
            RuntimeEventIngest(
                runtime_key=f"codex:{session_id}",
                session_id=session_id,
                thread_id=thread_id,
                run_id=first["turn"]["run_id"],
                provider="codex",
                device_id="cinder",
                source="codex_exec",
                kind="binding_signal",
                occurred_at=datetime.now(UTC),
                dedupe_key=f"binding:{first['turn']['run_id']}",
                payload={"provider_session_id": provider_thread_id},
            )
        ]
    )
    settled = store.update_console_turn(
        data={
            **turn_identity,
            "run_id": first["turn"]["run_id"],
            "state": "completed",
            "updated_at": datetime.now(UTC),
        }
    )
    assert settled["turn"]["state"] == "completed"
    assert settled["next_turn"]["turn_id"] == second["turn"]["turn_id"]
    assert settled["next_turn"]["state"] == "starting"
    assert settled["next_turn"]["run_id"]
    assert settled["next_turn"]["resume_provider_thread_id"] == provider_thread_id
    exact_replay = store.update_console_turn(
        data={
            **turn_identity,
            "run_id": first["turn"]["run_id"],
            "state": "completed",
            "updated_at": datetime.now(UTC),
        }
    )
    assert exact_replay["applied"] is False
    assert exact_replay["exact_replay"] is True
    assert exact_replay["next_turn"]["turn_id"] == settled["next_turn"]["turn_id"]
    assert exact_replay["next_turn"]["run_id"] == settled["next_turn"]["run_id"]
    conflicting_replay = store.update_console_turn(
        data={
            **turn_identity,
            "run_id": first["turn"]["run_id"],
            "state": "failed",
            "updated_at": datetime.now(UTC),
        }
    )
    assert conflicting_replay["applied"] is False
    assert conflicting_replay["stale"] is True
    assert conflicting_replay["next_turn"] is None
    idle_at = datetime.now(UTC)
    store.apply_session_runtime(
        events=[
            RuntimeEventIngest(
                runtime_key=f"codex:{session_id}",
                session_id=session_id,
                thread_id=thread_id,
                run_id=settled["next_turn"]["run_id"],
                provider="codex",
                device_id="cinder",
                source="codex_exec",
                kind="phase_signal",
                phase="idle",
                occurred_at=idle_at,
                dedupe_key=f"phase:{settled['next_turn']['run_id']}:idle",
            )
        ]
    )
    queued_read = store.read_shadow_session_state(session_id=str(session_id), owner_id=1)
    queued_state = project_catalog_session_facts(
        queued_read["legacy_facts"],
        observed_at=datetime.fromisoformat(queued_read["observed_at"]),
        canonical_heads=queued_read["heads"],
        commit_seq=int(queued_read["commit_seq"]),
    ).session_state
    assert queued_state.activity.state == "quiescent"
    assert queued_state.presentation.access.key == "live_control"
    assert queued_state.control is not None
    assert queued_state.control.actions.start_turn.state == "available"
    stale_replay = store.update_console_turn(
        data={
            **turn_identity,
            "turn_id": first["turn"]["turn_id"],
            "run_id": first["turn"]["run_id"],
            "state": "active",
            "expected_state": "starting",
            "updated_at": datetime.now(UTC),
        }
    )
    assert stale_replay["applied"] is False
    assert stale_replay["stale"] is True
    assert stale_replay["turn"]["state"] == "completed"

    failed = store.update_console_turn(
        data={
            **turn_identity,
            "turn_id": settled["next_turn"]["turn_id"],
            "run_id": settled["next_turn"]["run_id"],
            "state": "failed",
            "error_code": "claude_lifecycle_hook_missing",
            "error": "run `longhouse claude configure`",
            "updated_at": datetime.now(UTC),
        }
    )
    assert failed["turn"]["state"] == "failed"
    with Session(engine) as db:
        failed_turn = db.get(LiveConsoleTurn, settled["next_turn"]["turn_id"])
        failed_run = db.get(LiveSessionRun, settled["next_turn"]["run_id"])
        receipt = db.get(LiveSessionInputReceipt, failed_turn.receipt_id)
        assert failed_run.exit_status == "claude_lifecycle_hook_missing"
        assert json.loads(receipt.error_json) == {
            "code": "claude_lifecycle_hook_missing",
            "message": "run `longhouse claude configure`",
        }


@pytest.mark.asyncio
async def test_agents_console_turn_uses_catalog_without_cold_session(monkeypatch):
    from zerg.routers import agents_sessions

    session_id = uuid4()
    turn_id = uuid4()
    run_id = uuid4()
    dispatched = {}

    async def enqueue(**kwargs):
        dispatched.update(kwargs)
        return CatalogConsoleTurn(
            turn_id=turn_id,
            run_id=run_id,
            state="active",
            created=True,
        )

    monkeypatch.setattr(agents_sessions, "enqueue_catalog_console_turn", enqueue)

    response = await agents_sessions.create_console_turn(
        session_id=session_id,
        body=agents_sessions.ConsoleTurnCreate(message="first message", client_request_id="request-1"),
        db=None,
        auth=SimpleNamespace(owner_id=1),
        _single=None,
    )

    assert response.turn_id == turn_id
    assert response.run_id == run_id
    assert response.state == "active"
    assert dispatched == {
        "owner_id": 1,
        "session_id": session_id,
        "message": "first message",
        "client_request_id": "request-1",
    }


def test_local_launch_shell_binds_thread_execution_target_for_console(tmp_path):
    """Console dispatch must not see `execution_target_missing` after a launch.

    Regression: `create_live_launch_catalog_shell` created LiveSessionThread
    without device_id/cwd, so `enqueue_console_turn` refused every managed-local
    Helm session (``execution_target_missing``) — the launch shell is the one
    binding the thread rows the console target check reads.
    """
    engine = create_catalog_engine(tmp_path / "catalog-launch-target.db")
    initialize_catalog_schema(engine)
    store = CatalogStore(engine)
    session_id = uuid4()
    with Session(engine) as db:
        db.add(LiveUser(id=1, email="owner@example.com", is_active=True))
        db.commit()
    now = datetime.now(UTC).replace(microsecond=0)
    created = store.create_local_launch(
        launch={
            "owner_id": 1,
            "git_repo": None,
            "git_branch": None,
            "started_at": now,
            "expires_at": (now + timedelta(minutes=5)),
            "plan": {
                "session_id": str(session_id),
                "provider": "pi",
                "provider_session_id": None,
                "source_name": "cinder",
                "source_runner_id": None,
                "cwd": "/tmp/pi-dogfood",
                "project": "pi-dogfood",
                "display_name": "ai",
                "managed_session_name": "pi-managed-1",
                "loop_mode": "assist",
                "permission_mode": "bypass",
                "launch_actor": "cli",
                "launch_surface": "terminal",
                "managed_transport": "pi_print",
                "attach_command": "",
                "provider_config": {
                    "pi_provider": "openrouter",
                    "model": "deepseek/deepseek-v4-flash-latest",
                },
            },
        }
    )
    assert created["created"] is True
    thread = None
    with Session(engine) as db:
        thread = db.get(LiveSessionThread, str(primary_thread_id_for_session(session_id)))
        assert thread is not None
        assert thread.device_id == "cinder"
        assert thread.cwd == "/tmp/pi-dogfood"
        assert json.loads(thread.provider_config_json or "{}").get("pi_provider") == "openrouter"
    turn = store.enqueue_console_turn(
        data={
            "session_id": str(session_id),
            "owner_id": 1,
            "message": "count to three",
            "client_request_id": "pi-turn-1",
            "created_at": now,
        }
    )
    assert "unavailable" not in turn
    assert turn["turn"]["provider"] == "pi"
    assert turn["turn"]["provider_config"]["pi_provider"] == "openrouter"


def _seed_branch_parent(engine, *, owner_id: int = 1):
    """A Helm parent that has a provider thread to fork from."""

    now = datetime.now(UTC)
    parent_id = str(uuid4())
    parent_thread_id = str(uuid4())
    with Session(engine) as db:
        db.add_all(
            [
                LiveUser(id=owner_id, email="owner@example.com", is_active=True),
                LiveSessionCatalog(
                    session_id=parent_id,
                    provider="codex",
                    environment="production",
                    origin_kind="managed_local",
                    project="longhouse",
                    cwd="/tmp/longhouse",
                    device_id="cinder",
                    started_at=now,
                    last_activity_at=now,
                    primary_thread_id=parent_thread_id,
                    created_at=now,
                    updated_at=now,
                ),
                LiveSessionThread(
                    id=parent_thread_id,
                    session_id=parent_id,
                    provider="codex",
                    device_id="cinder",
                    cwd="/tmp/longhouse",
                    branch_kind="root",
                    is_primary=1,
                    created_at=now,
                    updated_at=now,
                ),
                # Ownership is durable evidence, not a catalog column: the
                # store fails closed unless a row explicitly binds the owner.
                LiveSession(
                    session_id=parent_id,
                    owner_id=owner_id,
                    provider="codex",
                    device_id="cinder",
                    started_at=now,
                ),
                LiveSessionThreadAlias(
                    thread_id=parent_thread_id,
                    provider="codex",
                    alias_kind="provider_session_id",
                    alias_value="parent-provider-thread",
                    first_seen_at=now,
                    last_seen_at=now,
                ),
            ]
        )
        db.commit()
    return parent_id, parent_thread_id, now


def _branch_request(parent_id, *, now, owner_id=1, client_request_id="branch-1", message="keep going"):
    return {
        "parent_session_id": parent_id,
        "session_id": str(uuid4()),
        "thread_id": str(uuid4()),
        "owner_id": owner_id,
        "message": message,
        "client_request_id": client_request_id,
        "created_at": now,
    }


def test_branch_create_persists_child_lineage_and_first_turn_together(tmp_path):
    """Create and first turn are one transaction, and the turn forks.

    They were two catalogd RPCs in two transactions, which left a window where
    the parent could be resumed between them -- changing the thread the fork
    would be taken from -- and a client that died in the gap would leave an
    empty session nobody asked for.
    """

    engine = create_catalog_engine(tmp_path / "catalog.db")
    initialize_catalog_schema(engine)
    parent_id, parent_thread_id, now = _seed_branch_parent(engine)

    request = _branch_request(parent_id, now=now)
    result = CatalogStore(engine).create_branch_session(data=request)

    assert result["created"] is True
    turn = result["turn"]
    # A branch's first turn forks and never resumes: the child owns no thread
    # yet, so a resume identity here would be the parent's and would continue it.
    assert turn["fork_from_provider_thread_id"] == "parent-provider-thread"
    assert turn["resume_provider_thread_id"] is None
    assert turn["state"] == "starting"

    with Session(engine) as db:
        child_thread = db.get(LiveSessionThread, request["thread_id"])
        assert child_thread.parent_thread_id == parent_thread_id
        assert child_thread.parent_session_id == parent_id
        assert child_thread.branch_kind == "fork"
        # Provider-tier lineage rides the non-routing alias kind; the routing
        # alias belongs to whichever thread owns that provider session, and the
        # child claims its own once the fork returns a new id.
        alias = db.query(LiveSessionThreadAlias).filter(LiveSessionThreadAlias.thread_id == request["thread_id"]).one()
        assert alias.alias_kind == "forked_from_provider_session_id"
        assert alias.alias_value == "parent-provider-thread"
        assert db.query(LiveConsoleTurn).count() == 1
        assert db.query(LiveSessionRun).count() == 1


def test_branch_create_replay_is_keyed_on_the_parent(tmp_path):
    """A retry must not create a second branch.

    Replay cannot key on the child's own session id the way an ordinary console
    turn does: on a retry the child does not exist yet, so the parent plus the
    client request id is the only identity the request has.
    """

    engine = create_catalog_engine(tmp_path / "catalog.db")
    initialize_catalog_schema(engine)
    parent_id, _parent_thread_id, now = _seed_branch_parent(engine)
    store = CatalogStore(engine)

    first = store.create_branch_session(data=_branch_request(parent_id, now=now))
    replay = store.create_branch_session(data=_branch_request(parent_id, now=now))

    assert first["created"] is True
    assert replay["created"] is False
    assert replay["idempotency_conflict"] is False
    assert replay["session_id"] == first["session_id"]
    with Session(engine) as db:
        assert db.query(LiveSessionInputReceipt).count() == 1
        assert db.query(LiveConsoleTurn).count() == 1

    # Same key, different content is a conflict rather than a silent second
    # branch or a silently ignored edit.
    conflict = store.create_branch_session(data=_branch_request(parent_id, now=now, message="different"))
    assert conflict["created"] is False
    assert conflict["idempotency_conflict"] is True
    assert conflict["turn"] is None


def test_branch_create_refuses_a_parent_with_no_provider_thread(tmp_path):
    """Nothing to fork from is a typed refusal, not a half-made session."""

    engine = create_catalog_engine(tmp_path / "catalog.db")
    initialize_catalog_schema(engine)
    parent_id, parent_thread_id, now = _seed_branch_parent(engine)
    with Session(engine) as db:
        db.query(LiveSessionThreadAlias).filter(LiveSessionThreadAlias.thread_id == parent_thread_id).delete()
        db.commit()

    result = CatalogStore(engine).create_branch_session(data=_branch_request(parent_id, now=now))

    assert result == {"found": True, "unavailable": "provider_identity_missing"}
    with Session(engine) as db:
        assert db.query(LiveConsoleTurn).count() == 0
        assert db.query(LiveSessionCatalog).count() == 1


def test_branch_create_refuses_another_owners_session(tmp_path):
    engine = create_catalog_engine(tmp_path / "catalog.db")
    initialize_catalog_schema(engine)
    parent_id, _parent_thread_id, now = _seed_branch_parent(engine)

    result = CatalogStore(engine).create_branch_session(data=_branch_request(parent_id, now=now, owner_id=42))

    assert result == {"found": False}
