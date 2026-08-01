"""Control teardown must not manufacture a new run.

A Helm session that exits cleanly ends its run. The next lease snapshot no
longer carries that session, so `mark_missing_live_control_leases` marks it
detached. That teardown path called `attach_live_catalog_control` without a
run_id, which minted a second run that never ends.

The consequence was not cosmetic: the projector binds activity and control
facts to the durable latest run, so a never-ending ghost run permanently
rejected every fact the session had. The session read `Activity unknown` and
`running` forever instead of `Idle` then `Ended`.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker

from zerg.database import initialize_live_database
from zerg.database import make_live_engine
from zerg.models.live_store import LiveSessionCatalog
from zerg.models.live_store import LiveSessionRun
from zerg.models.live_store import LiveSessionThread
from zerg.services.live_catalog_launch import attach_live_catalog_control
from zerg.services.managed_control_state import mark_missing_live_control_leases
from zerg.services.managed_control_state import upsert_live_control_leases


@pytest.fixture
def live_session_factory(tmp_path: Path):
    engine = make_live_engine(f"sqlite:///{tmp_path}/live.db")
    initialize_live_database(engine)
    try:
        yield sessionmaker(bind=engine)
    finally:
        engine.dispose()


def _seed_finished_helm_session(factory, *, provider: str = "cursor"):
    """A session whose run already ended cleanly, as after a normal exit."""

    started_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    ended_at = started_at + timedelta(minutes=1)
    session_id = uuid4()
    thread_id = uuid4()
    run_id = uuid4()
    with factory() as db:
        db.add(
            LiveSessionCatalog(
                session_id=str(session_id),
                provider=provider,
                environment="development",
                device_id="cinder",
                started_at=started_at,
                primary_thread_id=str(thread_id),
                created_at=started_at,
                updated_at=ended_at,
            )
        )
        db.add(
            LiveSessionThread(
                id=str(thread_id),
                session_id=str(session_id),
                provider=provider,
                branch_kind="root",
                is_primary=1,
                created_at=started_at,
                updated_at=ended_at,
            )
        )
        db.add(
            LiveSessionRun(
                id=str(run_id),
                thread_id=str(thread_id),
                provider=provider,
                host_id="cinder",
                launch_origin="longhouse_spawned",
                started_at=started_at,
                ended_at=ended_at,
                exit_status="exit_0",
            )
        )
        db.commit()
    return session_id, thread_id, run_id, ended_at


def _runs(factory, thread_id) -> list[LiveSessionRun]:
    with factory() as db:
        return db.query(LiveSessionRun).filter(LiveSessionRun.thread_id == str(thread_id)).all()


def test_missing_lease_does_not_mint_a_run(live_session_factory):
    session_id, thread_id, run_id, ended_at = _seed_finished_helm_session(live_session_factory)
    seen_at = ended_at + timedelta(seconds=2)

    lease = SimpleNamespace(
        session_id=session_id,
        provider="cursor",
        machine_id="cinder",
        state="attached",
        sequence=1,
        bridge_status="ready",
        thread_subscription_status=None,
        observed_at=ended_at - timedelta(seconds=30),
        lease_ttl_ms=900_000,
    )
    with live_session_factory() as db:
        upsert_live_control_leases(db, [lease], device_id="cinder", received_at=ended_at - timedelta(seconds=30))
        db.commit()
    with live_session_factory() as db:
        # The launcher is gone: the next snapshot omits the session entirely.
        mark_missing_live_control_leases(db, [], device_id="cinder", received_at=seen_at)
        db.commit()

    runs = _runs(live_session_factory, thread_id)
    assert [str(run.id) for run in runs] == [str(run_id)], "teardown must not create a second run"
    assert runs[0].ended_at is not None


def test_detach_without_a_run_is_a_no_op(live_session_factory):
    session_id, thread_id, run_id, ended_at = _seed_finished_helm_session(live_session_factory)

    with live_session_factory() as db:
        connection = attach_live_catalog_control(
            db,
            session_id=session_id,
            provider="cursor",
            device_id="cinder",
            state="detached",
            observed_at=ended_at + timedelta(seconds=2),
        )
        db.commit()

    assert connection is None
    assert [str(run.id) for run in _runs(live_session_factory, thread_id)] == [str(run_id)]


def test_detach_does_not_resurrect_an_ended_session(live_session_factory):
    session_id, thread_id, _run_id, ended_at = _seed_finished_helm_session(live_session_factory)
    with live_session_factory() as db:
        session = db.get(LiveSessionCatalog, str(session_id))
        session.ended_at = ended_at
        db.commit()

    with live_session_factory() as db:
        mark_missing_live_control_leases(db, [], device_id="cinder", received_at=ended_at + timedelta(seconds=2))
        db.commit()

    with live_session_factory() as db:
        session = db.get(LiveSessionCatalog, str(session_id))
        assert session.ended_at is not None, "a detach observation must not un-end a session"


def test_launch_still_creates_its_named_run(live_session_factory):
    """The guard must not break the path that legitimately opens a run."""

    session_id, thread_id, existing_run_id, ended_at = _seed_finished_helm_session(live_session_factory)
    new_run_id = uuid4()

    with live_session_factory() as db:
        connection = attach_live_catalog_control(
            db,
            session_id=session_id,
            provider="cursor",
            device_id="cinder",
            state="attached",
            run_id=new_run_id,
            observed_at=ended_at + timedelta(seconds=5),
        )
        db.commit()

    assert connection is not None
    assert {str(run.id) for run in _runs(live_session_factory, thread_id)} == {
        str(existing_run_id),
        str(new_run_id),
    }


def test_lease_attach_reuses_the_open_run(live_session_factory):
    """An observer with no run_id binds to the open run rather than making one."""

    started_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    session_id = uuid4()
    thread_id = uuid4()
    run_id = uuid4()
    with live_session_factory() as db:
        db.add(
            LiveSessionCatalog(
                session_id=str(session_id),
                provider="cursor",
                environment="development",
                device_id="cinder",
                started_at=started_at,
                primary_thread_id=str(thread_id),
                created_at=started_at,
                updated_at=started_at,
            )
        )
        db.add(
            LiveSessionThread(
                id=str(thread_id),
                session_id=str(session_id),
                provider="cursor",
                branch_kind="root",
                is_primary=1,
                created_at=started_at,
                updated_at=started_at,
            )
        )
        db.add(
            LiveSessionRun(
                id=str(run_id),
                thread_id=str(thread_id),
                provider="cursor",
                host_id="cinder",
                launch_origin="longhouse_spawned",
                started_at=started_at,
            )
        )
        db.commit()

    with live_session_factory() as db:
        connection = attach_live_catalog_control(
            db,
            session_id=session_id,
            provider="cursor",
            device_id="cinder",
            state="attached",
            observed_at=datetime.now(timezone.utc),
        )
        db.commit()

    assert connection is not None
    assert [str(run.id) for run in _runs(live_session_factory, thread_id)] == [str(run_id)]
