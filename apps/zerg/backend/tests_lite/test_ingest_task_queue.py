"""Tests for the ingest task queue (SQLite-backed, durable).

Covers:
- enqueue_ingest_tasks inserts pending tasks
- Dedup: skips duplicate pending/running tasks
- reset_stale_running_tasks recovers crashed tasks
- _claim_pending marks tasks as running and increments attempts
- Worker retries on failure up to max_attempts
- Worker marks done on success
- Ingest endpoint uses task queue (no BackgroundTasks)
"""

import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from unittest.mock import AsyncMock
from unittest.mock import patch
from uuid import uuid4

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.database import Base
from zerg.database import get_db
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentsBase
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionPresence
from zerg.models.agents import SessionTask
from zerg.models.user import User
from zerg.models.work import OikosWakeup
from zerg.services.ingest_task_queue import _claim_pending
from zerg.services.ingest_task_queue import enqueue_ingest_tasks
from zerg.services.ingest_task_queue import reset_stale_running_tasks

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path, name="itq.db"):
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    AgentsBase.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)


def _get_tasks(factory, *, status=None):
    db = factory()
    q = db.query(SessionTask)
    if status:
        q = q.filter(SessionTask.status == status)
    tasks = q.all()
    db.close()
    return tasks


def _get_wakeups(factory):
    db = factory()
    wakeups = db.query(OikosWakeup).order_by(OikosWakeup.id).all()
    db.close()
    return wakeups


def _seed_completion_summary_task(
    factory,
    *,
    ended_at: datetime,
    presence_state: str | None,
    presence_updated_at: datetime | None = None,
    user_context: dict | None = None,
):
    db = factory()
    user = User(email="owner@example.com", context=user_context or {})
    db.add(user)
    db.flush()

    session_id = str(uuid4())
    session = AgentSession(
        id=session_id,
        provider="claude",
        environment="development",
        project="zerg",
        cwd="/Users/davidrose/git/zerg",
        started_at=ended_at - timedelta(minutes=5),
        ended_at=ended_at,
        summary_title="Autonomy follow-up",
        summary="Code landed and a targeted next step may still exist.",
        user_state="active",
    )
    db.add(session)

    if presence_state is not None:
        db.add(
            SessionPresence(
                session_id=session_id,
                state=presence_state,
                project="zerg",
                provider="claude",
                updated_at=presence_updated_at or ended_at,
            )
        )

    task_id = f"task-{session_id}"
    db.add(SessionTask(id=task_id, session_id=session_id, task_type="summary", status="running"))
    db.commit()
    db.close()
    return session_id, task_id, user.id


# ---------------------------------------------------------------------------
# enqueue_ingest_tasks
# ---------------------------------------------------------------------------


def test_enqueue_creates_summary_and_embedding_tasks(tmp_path):
    """enqueue_ingest_tasks inserts one summary + one embedding task."""
    factory = _make_db(tmp_path, "enq_basic.db")
    db = factory()
    enqueue_ingest_tasks(db, "session-1")
    db.commit()
    db.close()

    tasks = _get_tasks(factory)
    types = {t.task_type for t in tasks}
    assert types == {"summary", "embedding"}
    assert all(t.status == "pending" for t in tasks)
    assert all(t.session_id == "session-1" for t in tasks)


def test_enqueue_deduplicates_pending_tasks(tmp_path):
    """enqueue_ingest_tasks skips insertion when a pending task already exists."""
    factory = _make_db(tmp_path, "enq_dedup.db")
    db = factory()
    enqueue_ingest_tasks(db, "session-1")
    db.commit()
    enqueue_ingest_tasks(db, "session-1")  # second call — should be no-op
    db.commit()
    db.close()

    tasks = _get_tasks(factory)
    assert len(tasks) == 2  # still just 2, not 4


def test_enqueue_deduplicates_running_tasks(tmp_path):
    """enqueue_ingest_tasks skips insertion when a running task already exists."""
    factory = _make_db(tmp_path, "enq_dedup_running.db")
    db = factory()
    # Manually insert a running task
    db.add(SessionTask(session_id="session-1", task_type="summary", status="running"))
    db.commit()

    enqueue_ingest_tasks(db, "session-1")
    db.commit()
    db.close()

    tasks = _get_tasks(factory, status="pending")
    # Only embedding should be pending; summary is running
    assert len(tasks) == 1
    assert tasks[0].task_type == "embedding"


def test_enqueue_allows_requeue_after_done(tmp_path):
    """enqueue_ingest_tasks allows re-queuing after done (new ingest events)."""
    factory = _make_db(tmp_path, "enq_requeue.db")
    db = factory()
    db.add(SessionTask(session_id="session-1", task_type="summary", status="done"))
    db.commit()

    enqueue_ingest_tasks(db, "session-1")
    db.commit()
    db.close()

    tasks = _get_tasks(factory, status="pending")
    types = {t.task_type for t in tasks}
    assert "summary" in types


# ---------------------------------------------------------------------------
# reset_stale_running_tasks
# ---------------------------------------------------------------------------


def test_reset_stale_running_resets_old_tasks(tmp_path):
    """Stale running tasks are reset to pending."""
    factory = _make_db(tmp_path, "stale.db")
    db = factory()
    stale = SessionTask(
        session_id="s1",
        task_type="summary",
        status="running",
        updated_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    db.add(stale)
    db.commit()
    db.close()

    db = factory()
    count = reset_stale_running_tasks(db)
    db.close()

    assert count == 1
    tasks = _get_tasks(factory, status="pending")
    assert len(tasks) == 1


def test_reset_stale_preserves_recent_running(tmp_path):
    """Recent running tasks are NOT reset."""
    factory = _make_db(tmp_path, "fresh_running.db")
    db = factory()
    db.add(SessionTask(session_id="s1", task_type="summary", status="running"))
    db.commit()
    db.close()

    db = factory()
    count = reset_stale_running_tasks(db)
    db.close()

    assert count == 0
    tasks = _get_tasks(factory, status="running")
    assert len(tasks) == 1


# ---------------------------------------------------------------------------
# _claim_pending
# ---------------------------------------------------------------------------


def test_claim_pending_marks_running(tmp_path):
    """_claim_pending moves pending tasks to running and increments attempts."""
    factory = _make_db(tmp_path, "claim.db")
    db = factory()
    enqueue_ingest_tasks(db, "session-1")
    db.commit()
    db.close()

    db = factory()
    claimed = _claim_pending(db, limit=10)
    db.close()

    assert len(claimed) == 2
    tasks = _get_tasks(factory, status="running")
    assert len(tasks) == 2
    assert all(t.attempts == 1 for t in tasks)


def test_claim_pending_empty_returns_empty(tmp_path):
    """_claim_pending returns empty list when no pending tasks."""
    factory = _make_db(tmp_path, "claim_empty.db")
    db = factory()
    claimed = _claim_pending(db, limit=10)
    db.close()
    assert claimed == []


# ---------------------------------------------------------------------------
# Worker execution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_marks_done_on_success(tmp_path):
    """Worker marks task done when execution succeeds."""
    from zerg.services.ingest_task_queue import _execute_task

    factory = _make_db(tmp_path, "worker_done.db")
    db = factory()
    db.add(SessionTask(id="task-1", session_id="s1", task_type="summary", status="running"))
    db.commit()
    db.close()

    with patch("zerg.services.ingest_task_queue.get_session_factory", return_value=factory):
        with patch("zerg.routers.agents._generate_summary_impl", new_callable=AsyncMock):
            await _execute_task("task-1", "s1", "summary")

    tasks = _get_tasks(factory, status="done")
    assert len(tasks) == 1


@pytest.mark.asyncio
async def test_summary_task_wakes_operator_for_recent_completed_idle_session(tmp_path, monkeypatch):
    """Recent completed turns wake operator mode after summary succeeds."""
    from zerg.services.ingest_task_queue import _execute_task

    monkeypatch.setenv("OIKOS_OPERATOR_MODE_ENABLED", "1")
    factory = _make_db(tmp_path, "worker_operator_completion.db")
    ended_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    session_id, task_id, owner_id = _seed_completion_summary_task(
        factory,
        ended_at=ended_at,
        presence_state="idle",
        presence_updated_at=ended_at,
    )

    with patch("zerg.services.ingest_task_queue.get_session_factory", return_value=factory):
        with patch("zerg.routers.agents._generate_summary_impl", new_callable=AsyncMock):
            with patch("zerg.services.oikos_service.invoke_oikos", new_callable=AsyncMock) as invoke_oikos:
                invoke_oikos.return_value = 123
                await _execute_task(task_id, session_id, "summary")

    invoke_oikos.assert_awaited_once()
    args = invoke_oikos.await_args.args
    kwargs = invoke_oikos.await_args.kwargs
    assert args[0] == owner_id
    assert "Trigger: session_completed" in args[1]
    assert args[2] == f"operator-session-completed-{session_id}-{task_id}"
    assert kwargs["source"] == "operator"
    assert kwargs["surface_payload"]["trigger_type"] == "session_completed"
    assert kwargs["surface_payload"]["session_id"] == session_id

    tasks = _get_tasks(factory, status="done")
    wakeups = _get_wakeups(factory)
    assert len(tasks) == 1
    assert len(wakeups) == 1
    assert wakeups[0].status == "enqueued"
    assert wakeups[0].run_id == 123
    assert wakeups[0].trigger_type == "session_completed"
    assert wakeups[0].payload["presence_state"] == "idle"


@pytest.mark.asyncio
@pytest.mark.parametrize("presence_state", ["thinking", "running", "needs_user", "blocked"])
async def test_summary_task_skips_operator_when_session_is_still_active(tmp_path, monkeypatch, presence_state):
    """Fresh active/pause presence suppresses completion wakeups."""
    from zerg.services.ingest_task_queue import _execute_task

    monkeypatch.setenv("OIKOS_OPERATOR_MODE_ENABLED", "1")
    factory = _make_db(tmp_path, f"worker_operator_skip_{presence_state}.db")
    ended_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    session_id, task_id, _owner_id = _seed_completion_summary_task(
        factory,
        ended_at=ended_at,
        presence_state=presence_state,
        presence_updated_at=datetime.now(timezone.utc),
    )

    with patch("zerg.services.ingest_task_queue.get_session_factory", return_value=factory):
        with patch("zerg.routers.agents._generate_summary_impl", new_callable=AsyncMock):
            with patch("zerg.services.oikos_service.invoke_oikos", new_callable=AsyncMock) as invoke_oikos:
                await _execute_task(task_id, session_id, "summary")

    invoke_oikos.assert_not_awaited()

    tasks = _get_tasks(factory, status="done")
    wakeups = _get_wakeups(factory)
    assert len(tasks) == 1
    assert len(wakeups) == 1
    assert wakeups[0].status == "suppressed"
    assert wakeups[0].reason == f"fresh_presence_{presence_state}"


@pytest.mark.asyncio
async def test_summary_task_skips_operator_for_historical_completed_session(tmp_path, monkeypatch):
    """Historical backfill should not wake operator mode."""
    from zerg.services.ingest_task_queue import _execute_task

    monkeypatch.setenv("OIKOS_OPERATOR_MODE_ENABLED", "1")
    factory = _make_db(tmp_path, "worker_operator_skip_historical.db")
    ended_at = datetime.now(timezone.utc) - timedelta(minutes=45)
    session_id, task_id, _owner_id = _seed_completion_summary_task(
        factory,
        ended_at=ended_at,
        presence_state=None,
    )

    with patch("zerg.services.ingest_task_queue.get_session_factory", return_value=factory):
        with patch("zerg.routers.agents._generate_summary_impl", new_callable=AsyncMock):
            with patch("zerg.services.oikos_service.invoke_oikos", new_callable=AsyncMock) as invoke_oikos:
                await _execute_task(task_id, session_id, "summary")

    invoke_oikos.assert_not_awaited()

    tasks = _get_tasks(factory, status="done")
    wakeups = _get_wakeups(factory)
    assert len(tasks) == 1
    assert len(wakeups) == 1
    assert wakeups[0].status == "suppressed"
    assert wakeups[0].reason == "stale_completion"


@pytest.mark.asyncio
async def test_summary_task_skips_operator_when_user_policy_disables_it(tmp_path, monkeypatch):
    """User-backed operator prefs can disable post-ingest completion wakeups."""
    from zerg.services.ingest_task_queue import _execute_task

    monkeypatch.setenv("OIKOS_OPERATOR_MODE_ENABLED", "1")
    factory = _make_db(tmp_path, "worker_operator_skip_policy.db")
    ended_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    session_id, task_id, _owner_id = _seed_completion_summary_task(
        factory,
        ended_at=ended_at,
        presence_state="idle",
        presence_updated_at=ended_at,
        user_context={"preferences": {"operator_mode": {"enabled": False}}},
    )

    with patch("zerg.services.ingest_task_queue.get_session_factory", return_value=factory):
        with patch("zerg.routers.agents._generate_summary_impl", new_callable=AsyncMock):
            with patch("zerg.services.oikos_service.invoke_oikos", new_callable=AsyncMock) as invoke_oikos:
                await _execute_task(task_id, session_id, "summary")

    invoke_oikos.assert_not_awaited()

    tasks = _get_tasks(factory, status="done")
    wakeups = _get_wakeups(factory)
    assert len(tasks) == 1
    assert len(wakeups) == 1
    assert wakeups[0].status == "suppressed"
    assert wakeups[0].reason == "user_policy_disabled"


@pytest.mark.asyncio
async def test_worker_requeues_on_failure_within_max_attempts(tmp_path):
    """Worker re-queues task as pending when failure < max_attempts."""
    from zerg.services.ingest_task_queue import _execute_task

    factory = _make_db(tmp_path, "worker_retry.db")
    db = factory()
    db.add(SessionTask(id="task-2", session_id="s1", task_type="summary", status="running", attempts=1, max_attempts=3))
    db.commit()
    db.close()

    with patch("zerg.services.ingest_task_queue.get_session_factory", return_value=factory):
        with patch(
            "zerg.routers.agents._generate_summary_impl",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            await _execute_task("task-2", "s1", "summary")

    tasks = _get_tasks(factory, status="pending")
    assert len(tasks) == 1
    assert tasks[0].error == "boom"


@pytest.mark.asyncio
async def test_worker_marks_failed_on_exhausted_attempts(tmp_path):
    """Worker marks task failed when attempts >= max_attempts."""
    from zerg.services.ingest_task_queue import _execute_task

    factory = _make_db(tmp_path, "worker_fail.db")
    db = factory()
    db.add(SessionTask(id="task-3", session_id="s1", task_type="summary", status="running", attempts=3, max_attempts=3))
    db.commit()
    db.close()

    with patch("zerg.services.ingest_task_queue.get_session_factory", return_value=factory):
        with patch(
            "zerg.routers.agents._generate_summary_impl",
            new_callable=AsyncMock,
            side_effect=RuntimeError("final failure"),
        ):
            await _execute_task("task-3", "s1", "summary")

    tasks = _get_tasks(factory, status="failed")
    assert len(tasks) == 1


# ---------------------------------------------------------------------------
# Ingest endpoint integration
# ---------------------------------------------------------------------------


def test_ingest_endpoint_enqueues_tasks(tmp_path):
    """POST /agents/ingest creates session_tasks rows (not BackgroundTasks)."""
    from fastapi.testclient import TestClient

    from zerg.main import api_app

    db_path = tmp_path / "ingest_e2e.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    AgentsBase.metadata.create_all(bind=engine)
    factory = make_sessionmaker(engine)

    def override():
        d = factory()
        try:
            yield d
        finally:
            d.close()

    api_app.dependency_overrides[get_db] = override
    try:
        client = TestClient(api_app)
        payload = {
            "provider": "claude",
            "environment": "production",
            "provider_session_id": "test-session-abc",
            "started_at": "2026-01-01T00:00:00Z",
            "events": [
                {
                    "role": "user",
                    "content_text": "hello",
                    "timestamp": "2026-01-01T00:00:01Z",
                }
            ],
        }
        resp = client.post("/agents/ingest", json=payload, headers={"X-Device-Token": "dev"})
        assert resp.status_code == 200
        assert resp.json()["events_inserted"] == 1

        # Verify tasks were enqueued
        tasks = _get_tasks(factory)
        assert len(tasks) == 2
        assert {t.task_type for t in tasks} == {"summary", "embedding"}
    finally:
        api_app.dependency_overrides.clear()
