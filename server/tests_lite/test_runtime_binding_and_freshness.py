"""Regression tests for binding_signal, status_assertion, and turn freshness.

A Console adapter emits binding_signal carrying the new run_id before the new
run's first phase_signal. If binding_signal moves LiveRuntimeState.run_id onto
a row that still holds a prior run's terminal_state, every subsequent
phase_signal for the new run sees opens_new_run == False and gets dropped as
protected_terminal, leaving LiveRuntimeState frozen at the prior run's
terminal_state and causing _open_run_holds_live_ownership / turn.is_fresh to
return False once the active turn ages past 15 minutes.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import uuid4

from sqlalchemy.orm import Session

from zerg.catalogd.schema import create_catalog_engine
from zerg.catalogd.schema import initialize_catalog_schema
from zerg.catalogd.store import CatalogStore
from zerg.models.live_store import LiveConsoleTurn
from zerg.models.live_store import LiveRuntimeState
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionInputReceipt
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.services.session_runtime import RuntimeEventIngest
from zerg.services.session_runtime import ingest_live_runtime_events
from zerg.utils.time import normalize_utc


def _seed_console_session(tmp_path, name: str):
    engine = create_catalog_engine(tmp_path / name)
    initialize_catalog_schema(engine)
    store = CatalogStore(engine)
    session_id = uuid4()
    thread_id = uuid4()
    now = datetime.now(UTC)
    with Session(engine) as db:
        db.add(
            LiveSessionCatalog(
                session_id=str(session_id),
                provider="antigravity",
                environment="host-a",
                device_id="host-a",
                primary_thread_id=str(thread_id),
                started_at=now - timedelta(hours=2),
                updated_at=now - timedelta(hours=2),
            )
        )
        db.add(
            LiveSessionThread(
                id=str(thread_id),
                session_id=str(session_id),
                provider="antigravity",
                device_id="host-a",
                cwd="/repo",
                is_primary=1,
                created_at=now - timedelta(hours=2),
                updated_at=now - timedelta(hours=2),
            )
        )
        db.commit()
    return engine, store, session_id, thread_id, now


def test_binding_signal_for_new_run_does_not_poison_prior_terminal_state(tmp_path):
    engine, store, session_id, thread_id, now = _seed_console_session(tmp_path, "binding_new_run.db")
    runtime_key = f"antigravity:{session_id}"
    run1_id = uuid4()
    run2_id = uuid4()
    receipt_id = str(uuid4())
    turn_id = str(uuid4())
    t_run1_start = now - timedelta(hours=2)
    t_run1_end = now - timedelta(hours=1, minutes=50)
    t_run2_start = now - timedelta(minutes=30)

    with Session(engine) as db:
        db.add(
            LiveSessionRun(
                id=str(run1_id),
                thread_id=str(thread_id),
                provider="antigravity",
                host_id="host-a",
                cwd="/repo",
                launch_origin="longhouse_spawned",
                started_at=t_run1_start,
            )
        )
        db.commit()

    store.apply_session_runtime(
        events=[
            RuntimeEventIngest(
                runtime_key=runtime_key,
                session_id=session_id,
                thread_id=thread_id,
                run_id=run1_id,
                provider="antigravity",
                device_id="host-a",
                source="antigravity_acp",
                kind="phase_signal",
                phase="thinking",
                occurred_at=t_run1_start + timedelta(seconds=1),
                dedupe_key=f"r1-think:{run1_id}",
            ),
            RuntimeEventIngest(
                runtime_key=runtime_key,
                session_id=session_id,
                thread_id=thread_id,
                run_id=run1_id,
                provider="antigravity",
                device_id="host-a",
                source="antigravity_acp",
                kind="terminal_signal",
                occurred_at=t_run1_end,
                dedupe_key=f"r1-end:{run1_id}",
                payload={"terminal_state": "run_completed", "terminal_reason": "run_completed"},
            ),
        ]
    )

    with Session(engine) as db:
        db.add(
            LiveSessionRun(
                id=str(run2_id),
                thread_id=str(thread_id),
                provider="antigravity",
                host_id="host-a",
                cwd="/repo",
                launch_origin="longhouse_spawned",
                started_at=t_run2_start,
            )
        )
        db.add(
            LiveSessionInputReceipt(
                id=receipt_id,
                owner_id=1,
                session_id=str(session_id),
                thread_id=str(thread_id),
                provider="antigravity",
                device_id="host-a",
                client_request_id="ios-req-1",
                intent="auto",
                status="delivered",
                text="keep going",
                durable_event_id="antigravity-step-1505-user",
                created_at=t_run2_start,
                updated_at=t_run2_start + timedelta(seconds=1),
            )
        )
        db.add(
            LiveConsoleTurn(
                id=turn_id,
                session_id=str(session_id),
                thread_id=str(thread_id),
                receipt_id=receipt_id,
                run_id=str(run2_id),
                state="active",
                provider="antigravity",
                device_id="host-a",
                cwd="/repo",
                created_at=t_run2_start,
                updated_at=t_run2_start + timedelta(seconds=1),
            )
        )
        db.commit()

    # Second run emits binding_signal first, then phase_signal, then fresh
    # status_assertion 30 minutes later (past the 15m turn freshness horizon).
    store.apply_session_runtime(
        events=[
            RuntimeEventIngest(
                runtime_key=runtime_key,
                session_id=session_id,
                thread_id=thread_id,
                run_id=run2_id,
                provider="antigravity",
                device_id="host-a",
                source="antigravity_acp",
                kind="binding_signal",
                occurred_at=t_run2_start + timedelta(seconds=1),
                dedupe_key=f"r2-bind:{run2_id}",
                payload={"provider_session_id": "native-thread-1"},
            )
        ]
    )

    with Session(engine) as db:
        state = db.get(LiveRuntimeState, runtime_key)
        assert state is not None
        assert state.run_id == run1_id
        assert state.terminal_state == "run_completed"

    store.apply_session_runtime(
        events=[
            RuntimeEventIngest(
                runtime_key=runtime_key,
                session_id=session_id,
                thread_id=thread_id,
                run_id=run2_id,
                provider="antigravity",
                device_id="host-a",
                source="antigravity_acp",
                kind="phase_signal",
                phase="thinking",
                occurred_at=t_run2_start + timedelta(seconds=2),
                dedupe_key=f"r2-think:{run2_id}",
            ),
            RuntimeEventIngest(
                runtime_key=runtime_key,
                session_id=session_id,
                thread_id=thread_id,
                run_id=run2_id,
                provider="antigravity",
                device_id="host-a",
                source="antigravity_acp",
                kind="status_assertion",
                occurred_at=now - timedelta(seconds=5),
                dedupe_key=f"r2-assert:{run2_id}",
            ),
        ]
    )

    with Session(engine) as db:
        state = db.get(LiveRuntimeState, runtime_key)
        assert state is not None
        assert state.run_id == run2_id
        assert state.phase == "thinking"
        assert state.terminal_state is None
        assert normalize_utc(state.last_asserted_at) == now - timedelta(seconds=5)

    receipts = store.list_recent_input_receipts(session_id=str(session_id))["receipts"]
    assert len(receipts) == 1
    assert receipts[0]["turn"]["state"] == "active"
    assert receipts[0]["turn"]["is_fresh"] is True
    engine.dispose()


def test_phase_and_terminal_signals_self_heal_stale_prior_terminal(tmp_path):
    engine, _store, session_id, thread_id, now = _seed_console_session(tmp_path, "self_heal_terminal.db")
    runtime_key = f"antigravity:{session_id}"
    run_id = uuid4()
    old_terminal_at = now - timedelta(hours=10)
    run_started_at = now - timedelta(hours=1)

    # Seed the exact poisoned state left by the old binding_signal bug:
    # run_id points to the active run, while terminal_at predates run.started_at.
    with Session(engine) as db:
        db.add(
            LiveSessionRun(
                id=str(run_id),
                thread_id=str(thread_id),
                provider="antigravity",
                host_id="host-a",
                cwd="/repo",
                launch_origin="longhouse_spawned",
                started_at=run_started_at,
            )
        )
        db.add(
            LiveRuntimeState(
                runtime_key=runtime_key,
                session_id=session_id,
                thread_id=thread_id,
                run_id=run_id,
                provider="antigravity",
                device_id="host-a",
                phase="finished",
                phase_source="semantic",
                phase_started_at=old_terminal_at,
                last_runtime_signal_at=old_terminal_at,
                last_live_at=old_terminal_at,
                timeline_anchor_at=old_terminal_at,
                freshness_expires_at=old_terminal_at,
                terminal_state="run_completed",
                terminal_reason="run_completed",
                terminal_source="antigravity_acp",
                terminal_at=old_terminal_at,
                runtime_version=1,
            )
        )
        db.commit()

        result = ingest_live_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session_id,
                    thread_id=thread_id,
                    run_id=run_id,
                    provider="antigravity",
                    device_id="host-a",
                    source="antigravity_acp",
                    kind="phase_signal",
                    phase="thinking",
                    occurred_at=now - timedelta(seconds=10),
                    dedupe_key=f"heal-think:{run_id}",
                )
            ],
        )
        db.commit()
        assert runtime_key in result.updated_runtime_keys
        state = db.get(LiveRuntimeState, runtime_key)
        assert state is not None
        assert state.phase == "thinking"
        assert state.terminal_state is None
        assert state.terminal_at is None

    # Also verify a poisoned row receiving terminal_signal directly updates
    # terminal_at instead of treating it as same_run_terminal_replay.
    run2_id = uuid4()
    with Session(engine) as db:
        db.add(
            LiveSessionRun(
                id=str(run2_id),
                thread_id=str(thread_id),
                provider="antigravity",
                host_id="host-a",
                cwd="/repo",
                launch_origin="longhouse_spawned",
                started_at=run_started_at,
            )
        )
        state = db.get(LiveRuntimeState, runtime_key)
        assert state is not None
        state.run_id = run2_id
        state.phase = "finished"
        state.terminal_state = "run_completed"
        state.terminal_at = old_terminal_at
        db.commit()

        new_terminal_at = now - timedelta(seconds=2)
        ingest_live_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session_id,
                    thread_id=thread_id,
                    run_id=run2_id,
                    provider="antigravity",
                    device_id="host-a",
                    source="antigravity_acp",
                    kind="terminal_signal",
                    occurred_at=new_terminal_at,
                    dedupe_key=f"heal-end:{run2_id}",
                    payload={"terminal_state": "run_completed", "terminal_reason": "run_completed"},
                )
            ],
        )
        db.commit()
        state = db.get(LiveRuntimeState, runtime_key)
        assert state is not None
        assert normalize_utc(state.terminal_at) == new_terminal_at
    engine.dispose()


def test_status_assertion_ignores_terminated_row_and_mismatched_run(tmp_path):
    engine, _store, session_id, thread_id, now = _seed_console_session(tmp_path, "assertion_guards.db")
    runtime_key = f"antigravity:{session_id}"
    active_run_id = uuid4()
    other_run_id = uuid4()

    with Session(engine) as db:
        db.add(
            LiveRuntimeState(
                runtime_key=runtime_key,
                session_id=session_id,
                thread_id=thread_id,
                run_id=active_run_id,
                provider="antigravity",
                device_id="host-a",
                phase="thinking",
                phase_source="semantic",
                phase_started_at=now - timedelta(minutes=2),
                last_runtime_signal_at=now - timedelta(minutes=2),
                last_live_at=now - timedelta(minutes=2),
                timeline_anchor_at=now - timedelta(minutes=2),
                terminal_state=None,
                runtime_version=1,
            )
        )
        db.commit()

        # Mismatched run_id must not renew the active run's assertion timestamp.
        res_mismatch = ingest_live_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session_id,
                    run_id=other_run_id,
                    provider="antigravity",
                    source="antigravity_acp",
                    kind="status_assertion",
                    occurred_at=now - timedelta(seconds=10),
                    dedupe_key="assert-mismatch",
                )
            ],
        )
        assert res_mismatch.ignored == 1
        assert db.get(LiveRuntimeState, runtime_key).last_asserted_at is None

        # host_expired is unverified, not a confirmed run end; a returning host
        # may still renew last_asserted_at for the active run.
        state = db.get(LiveRuntimeState, runtime_key)
        state.terminal_state = "host_expired"
        state.terminal_at = now - timedelta(seconds=8)
        db.commit()

        res_host_expired = ingest_live_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session_id,
                    run_id=active_run_id,
                    provider="antigravity",
                    source="antigravity_acp",
                    kind="status_assertion",
                    occurred_at=now - timedelta(seconds=6),
                    dedupe_key="assert-host-expired",
                )
            ],
        )
        assert res_host_expired.ignored == 0
        assert normalize_utc(db.get(LiveRuntimeState, runtime_key).last_asserted_at) == now - timedelta(seconds=6)

        # Confirmed terminal runtime state must not accept status_assertion writes.
        state = db.get(LiveRuntimeState, runtime_key)
        state.terminal_state = "run_completed"
        state.terminal_at = now - timedelta(seconds=5)
        db.commit()

        res_terminal = ingest_live_runtime_events(
            db,
            [
                RuntimeEventIngest(
                    runtime_key=runtime_key,
                    session_id=session_id,
                    run_id=active_run_id,
                    provider="antigravity",
                    source="antigravity_acp",
                    kind="status_assertion",
                    occurred_at=now,
                    dedupe_key="assert-terminal",
                )
            ],
        )
        assert res_terminal.ignored == 1
        assert normalize_utc(db.get(LiveRuntimeState, runtime_key).last_asserted_at) == now - timedelta(seconds=6)
    engine.dispose()
