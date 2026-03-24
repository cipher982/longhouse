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

import asyncio
import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
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
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.models import CommisJob
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentsBase
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionPresence
from zerg.models.agents import SessionTask
from zerg.models.agents import SessionTurnReview
from zerg.models.user import User
from zerg.models.work import OikosWakeup
from zerg.services.ingest_task_queue import _claim_pending
from zerg.services.ingest_task_queue import enqueue_ingest_tasks
from zerg.services.ingest_task_queue import reset_stale_running_tasks
from zerg.services.session_loop_controller import LoopControllerDecision
from zerg.session_loop_mode import SessionLoopMode

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


def _get_turn_reviews(factory):
    db = factory()
    reviews = db.query(SessionTurnReview).order_by(SessionTurnReview.id).all()
    db.close()
    return reviews


def _seed_completion_task(
    factory,
    *,
    ended_at: datetime,
    presence_state: str | None,
    presence_updated_at: datetime | None = None,
    user_context: dict | None = None,
    summary: str = "Code landed and a targeted next step may still exist.",
    loop_mode: SessionLoopMode = SessionLoopMode.MANUAL,
    assistant_text: str = "Only targeted verification remains. Run the pending targeted tests.",
    task_type: str = "turn_loop",
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
        summary=summary,
        user_state="active",
        loop_mode=loop_mode.value,
    )
    db.add(session)
    user_event = AgentEvent(
        session_id=session_id,
        role="user",
        content_text="Finish the remaining verification work.",
        timestamp=ended_at - timedelta(minutes=1),
    )
    assistant_event = AgentEvent(
        session_id=session_id,
        role="assistant",
        content_text=assistant_text,
        timestamp=ended_at,
    )
    db.add(user_event)
    db.add(assistant_event)
    db.flush()

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
    db.add(SessionTask(id=task_id, session_id=session_id, task_type=task_type, status="running"))
    db.commit()
    db.close()
    return session_id, task_id, user.id, assistant_event.id


def _continue_decision() -> LoopControllerDecision:
    return LoopControllerDecision(
        decision="continue",
        summary="The same session has one obvious bounded next step.",
        rationale="This is the routine continue case after a completed assistant turn.",
        recommended_action="continue_session",
        follow_up_prompt="Run the pending targeted tests.",
        blocked_reasons=(),
        model_id="glm-test",
        raw_response='{"decision":"continue"}',
        loop_thread_id=42,
    )


# ---------------------------------------------------------------------------
# enqueue_ingest_tasks
# ---------------------------------------------------------------------------


def test_enqueue_creates_summary_embedding_and_turn_loop_tasks(tmp_path):
    """enqueue_ingest_tasks inserts one summary + one embedding + one turn_loop task."""
    factory = _make_db(tmp_path, "enq_basic.db")
    db = factory()
    enqueue_ingest_tasks(db, "session-1")
    db.commit()
    db.close()

    tasks = _get_tasks(factory)
    types = {t.task_type for t in tasks}
    assert types == {"summary", "embedding", "turn_loop"}
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
    assert len(tasks) == 3  # still just 3, not 6


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
    # Only embedding + turn_loop should be pending; summary is running
    assert len(tasks) == 2
    assert {task.task_type for task in tasks} == {"embedding", "turn_loop"}


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

    assert len(claimed) == 3
    tasks = _get_tasks(factory, status="running")
    assert len(tasks) == 3
    assert all(t.attempts == 1 for t in tasks)


def test_claim_pending_prioritizes_turn_loop_before_summary_and_embedding(tmp_path):
    """Turn-loop work should run before slower post-ingest tasks."""
    factory = _make_db(tmp_path, "claim_priority.db")
    db = factory()
    enqueue_ingest_tasks(db, "session-1")
    db.commit()
    db.close()

    db = factory()
    claimed = _claim_pending(db, limit=10)
    db.close()

    assert [task_type for _, _, task_type in claimed] == ["turn_loop", "summary", "embedding"]


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
async def test_worker_requeues_timed_out_embedding_task(tmp_path, monkeypatch):
    """Timed-out embeddings should retry instead of blocking later turn-loop work forever."""
    from zerg.services.ingest_task_queue import TASK_TIMEOUT_SECONDS
    from zerg.services.ingest_task_queue import _execute_task

    factory = _make_db(tmp_path, "worker_embedding_timeout.db")
    db = factory()
    db.add(
        SessionTask(
            id="task-embedding-timeout",
            session_id="s1",
            task_type="embedding",
            status="running",
            attempts=1,
        )
    )
    db.commit()
    db.close()

    async def _hang(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setitem(TASK_TIMEOUT_SECONDS, "embedding", 0.01)

    with patch("zerg.services.ingest_task_queue.get_session_factory", return_value=factory):
        with patch("zerg.routers.agents._generate_embeddings_impl", new=AsyncMock(side_effect=_hang)):
            await _execute_task("task-embedding-timeout", "s1", "embedding")

    tasks = _get_tasks(factory, status="pending")
    assert len(tasks) == 1
    assert tasks[0].error == "embedding task timed out after 0.01s"


@pytest.mark.asyncio
async def test_process_batch_reprioritizes_new_turn_loop_work_between_tasks(tmp_path):
    """A newly queued turn_loop should preempt older embeddings on the next claim."""
    from zerg.services.ingest_task_queue import _process_batch

    factory = _make_db(tmp_path, "worker_reprioritize_between_tasks.db")
    db = factory()
    db.add(SessionTask(id="task-summary", session_id="session-summary", task_type="summary", status="pending"))
    db.add(SessionTask(id="task-embedding", session_id="session-embedding", task_type="embedding", status="pending"))
    db.commit()
    db.close()

    executed: list[str] = []

    async def _fake_execute(task_id: str, session_id: str, task_type: str) -> None:
        executed.append(task_type)
        db = factory()
        try:
            task = db.query(SessionTask).filter(SessionTask.id == task_id).one()
            task.status = "done"
            task.error = None
            if task_type == "summary":
                db.add(
                    SessionTask(
                        id="task-turn-loop",
                        session_id="session-turn-loop",
                        task_type="turn_loop",
                        status="pending",
                    )
                )
            db.commit()
        finally:
            db.close()

    with patch("zerg.services.ingest_task_queue.get_session_factory", return_value=factory):
        with patch("zerg.services.ingest_task_queue._execute_task", new=AsyncMock(side_effect=_fake_execute)):
            await _process_batch()

    assert executed == ["summary", "turn_loop", "embedding"]


@pytest.mark.asyncio
async def test_summary_task_does_not_run_turn_loop_anymore(tmp_path, monkeypatch):
    """Summary work should not be the trigger for turn-loop evaluation."""
    from zerg.services.ingest_task_queue import _execute_task

    factory = _make_db(tmp_path, "worker_summary_no_turn_loop.db")
    ended_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    session_id, task_id, _owner_id, _assistant_event_id = _seed_completion_task(
        factory,
        ended_at=ended_at,
        presence_state="idle",
        task_type="summary",
    )

    with patch("zerg.services.ingest_task_queue.get_session_factory", return_value=factory):
        with patch("zerg.routers.agents._generate_summary_impl", new_callable=AsyncMock):
            with patch(
                "zerg.services.session_turn_reviews.evaluate_session_turn_with_llm",
                new_callable=AsyncMock,
            ) as evaluate:
                await _execute_task(task_id, session_id, "summary")

    evaluate.assert_not_awaited()
    tasks = _get_tasks(factory, status="done")
    reviews = _get_turn_reviews(factory)
    assert len(tasks) == 1
    assert reviews == []


@pytest.mark.asyncio
async def test_turn_loop_task_wakes_operator_for_recent_completed_idle_session(tmp_path, monkeypatch):
    """Recent completed turns record an AI loop review when the turn_loop task runs."""
    from zerg.services.ingest_task_queue import _execute_task

    monkeypatch.setenv("OIKOS_OPERATOR_MODE_ENABLED", "1")
    factory = _make_db(tmp_path, "worker_operator_completion.db")
    ended_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    session_id, task_id, owner_id, assistant_event_id = _seed_completion_task(
        factory,
        ended_at=ended_at,
        presence_state="idle",
        presence_updated_at=ended_at,
        summary="Only targeted verification remains.",
        loop_mode=SessionLoopMode.ASSIST,
        user_context={
            "preferences": {
                "operator_mode": {
                    "enabled": True,
                    "shadow_mode": True,
                    "allow_continue": False,
                    "allow_notify": True,
                }
            }
        },
    )

    with patch("zerg.services.ingest_task_queue.get_session_factory", return_value=factory):
        with patch(
            "zerg.services.session_turn_reviews.evaluate_session_turn_with_llm",
            new=AsyncMock(return_value=_continue_decision()),
        ):
            with patch("zerg.services.oikos_service.invoke_oikos", new=AsyncMock(return_value=321)) as invoke_oikos:
                await _execute_task(task_id, session_id, "turn_loop")

    tasks = _get_tasks(factory, status="done")
    reviews = _get_turn_reviews(factory)
    db = factory()
    try:
        wakeups = db.query(OikosWakeup).order_by(OikosWakeup.id.asc()).all()
    finally:
        db.close()
    assert len(tasks) == 1
    assert len(reviews) == 1
    assert owner_id > 0
    assert assistant_event_id > 0
    assert reviews[0].status == "enqueued"
    assert reviews[0].reason == "notify_user"
    assert reviews[0].run_id == 321
    assert reviews[0].trigger_type == "turn.completed"
    assert reviews[0].decision == "continue"
    assert reviews[0].execution_state == "awaiting_user_approval"
    assert reviews[0].loop_mode == "assist"
    assert reviews[0].mode_capability == "notify_only"
    assert reviews[0].recommended_action == "continue_session"
    assert reviews[0].follow_up_prompt == "Run the pending targeted tests."
    assert len(wakeups) == 1
    assert wakeups[0].status == "enqueued"
    assert wakeups[0].source == "turn_loop"
    assert wakeups[0].payload["turn_review"]["decision"]["follow_up_prompt"] == "Run the pending targeted tests."
    invoke_oikos.assert_awaited_once()


@pytest.mark.asyncio
async def test_turn_loop_task_uses_latest_assistant_turn_timestamp_when_session_ended_at_is_stale(
    tmp_path,
    monkeypatch,
):
    """Fresh assistant turns should still produce reviews even when session metadata lags behind."""
    from zerg.services.ingest_task_queue import _execute_task

    monkeypatch.setenv("OIKOS_OPERATOR_MODE_ENABLED", "1")
    factory = _make_db(tmp_path, "worker_turn_loop_stale_ended_at.db")
    stale_ended_at = datetime.now(timezone.utc) - timedelta(minutes=20)
    fresh_turn_at = datetime.now(timezone.utc)
    session_id, task_id, _owner_id, _assistant_event_id = _seed_completion_task(
        factory,
        ended_at=stale_ended_at,
        presence_state="idle",
        presence_updated_at=fresh_turn_at,
        loop_mode=SessionLoopMode.ASSIST,
        user_context={
            "preferences": {
                "operator_mode": {
                    "enabled": True,
                    "shadow_mode": True,
                    "allow_continue": False,
                    "allow_notify": True,
                }
            }
        },
    )

    db = factory()
    try:
        session = db.query(AgentSession).filter(AgentSession.id == session_id).one()
        session.ended_at = stale_ended_at
        db.add(
            AgentEvent(
                session_id=session_id,
                role="user",
                content_text="continue from the current managed-local turn",
                timestamp=fresh_turn_at,
            )
        )
        db.add(
            AgentEvent(
                session_id=session_id,
                role="assistant",
                content_text="Only targeted verification remains. Run the pending targeted tests.",
                timestamp=fresh_turn_at,
            )
        )
        db.commit()
        latest_assistant_event = (
            db.query(AgentEvent)
            .filter(AgentEvent.session_id == session_id, AgentEvent.role == "assistant")
            .order_by(AgentEvent.id.desc())
            .first()
        )
    finally:
        db.close()

    with patch("zerg.services.ingest_task_queue.get_session_factory", return_value=factory):
        with patch(
            "zerg.services.session_turn_reviews.evaluate_session_turn_with_llm",
            new=AsyncMock(return_value=_continue_decision()),
        ):
            with patch("zerg.services.oikos_service.invoke_oikos", new=AsyncMock(return_value=777)) as invoke_oikos:
                await _execute_task(task_id, session_id, "turn_loop")

    reviews = _get_turn_reviews(factory)
    tasks = _get_tasks(factory, status="done")
    assert len(tasks) == 1
    assert len(reviews) == 1
    assert latest_assistant_event is not None
    assert reviews[0].assistant_event_id == latest_assistant_event.id
    assert reviews[0].status == "enqueued"
    assert reviews[0].run_id == 777
    invoke_oikos.assert_awaited_once()


@pytest.mark.asyncio
async def test_turn_loop_task_processes_stale_completed_turn_from_durable_queue(tmp_path, monkeypatch):
    """Durable turn_loop tasks should still review the turn even if the worker picks it up late."""
    from zerg.services.ingest_task_queue import _execute_task

    monkeypatch.setenv("OIKOS_OPERATOR_MODE_ENABLED", "1")
    factory = _make_db(tmp_path, "worker_turn_loop_stale_queue_delay.db")
    stale_ended_at = datetime.now(timezone.utc) - timedelta(minutes=20)
    session_id, task_id, _owner_id, _assistant_event_id = _seed_completion_task(
        factory,
        ended_at=stale_ended_at,
        presence_state="idle",
        presence_updated_at=stale_ended_at,
        loop_mode=SessionLoopMode.ASSIST,
        user_context={
            "preferences": {
                "operator_mode": {
                    "enabled": True,
                    "shadow_mode": True,
                    "allow_continue": False,
                    "allow_notify": True,
                }
            }
        },
    )
    db = factory()
    try:
        task = db.query(SessionTask).filter(SessionTask.id == task_id).one()
        task.created_at = stale_ended_at + timedelta(minutes=3)
        db.commit()
    finally:
        db.close()

    with patch("zerg.services.ingest_task_queue.get_session_factory", return_value=factory):
        with patch(
            "zerg.services.session_turn_reviews.evaluate_session_turn_with_llm",
            new=AsyncMock(return_value=_continue_decision()),
        ):
            with patch("zerg.services.oikos_service.invoke_oikos", new=AsyncMock(return_value=888)) as invoke_oikos:
                await _execute_task(task_id, session_id, "turn_loop")

    reviews = _get_turn_reviews(factory)
    tasks = _get_tasks(factory, status="done")
    assert len(tasks) == 1
    assert len(reviews) == 1
    assert reviews[0].status == "enqueued"
    assert reviews[0].run_id == 888
    assert reviews[0].assistant_turn_finished_at is not None
    assert reviews[0].turn_loop_enqueued_at is not None
    assert reviews[0].turn_loop_completed_at is not None
    assert reviews[0].turn_loop_enqueued_at == tasks[0].created_at
    assert reviews[0].turn_loop_completed_at >= reviews[0].turn_loop_enqueued_at
    invoke_oikos.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("presence_state", ["thinking", "running"])
async def test_turn_loop_task_skips_operator_when_session_is_still_active(tmp_path, monkeypatch, presence_state):
    """Fresh active presence should re-queue turn_loop instead of silently finishing."""
    from zerg.services.ingest_task_queue import _execute_task

    monkeypatch.setenv("OIKOS_OPERATOR_MODE_ENABLED", "1")
    factory = _make_db(tmp_path, f"worker_operator_skip_{presence_state}.db")
    ended_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    session_id, task_id, _owner_id, _assistant_event_id = _seed_completion_task(
        factory,
        ended_at=ended_at,
        presence_state=presence_state,
        presence_updated_at=datetime.now(timezone.utc),
    )

    with patch("zerg.services.ingest_task_queue.get_session_factory", return_value=factory):
        with patch("zerg.services.oikos_service.invoke_oikos", new_callable=AsyncMock) as invoke_oikos:
            await _execute_task(task_id, session_id, "turn_loop")

    invoke_oikos.assert_not_awaited()

    tasks = _get_tasks(factory, status="pending")
    reviews = _get_turn_reviews(factory)
    assert len(tasks) == 1
    assert len(reviews) == 0
    assert "waiting for active session presence to settle" in (tasks[0].error or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("presence_state", ["needs_user", "blocked"])
async def test_turn_loop_task_reviews_completed_turn_even_when_session_is_paused(tmp_path, monkeypatch, presence_state):
    """Pause states still represent a finished turn and should be reviewed."""
    from zerg.services.ingest_task_queue import _execute_task

    monkeypatch.setenv("OIKOS_OPERATOR_MODE_ENABLED", "1")
    factory = _make_db(tmp_path, f"worker_operator_pause_{presence_state}.db")
    ended_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    session_id, task_id, _owner_id, _assistant_event_id = _seed_completion_task(
        factory,
        ended_at=ended_at,
        presence_state=presence_state,
        presence_updated_at=datetime.now(timezone.utc),
        loop_mode=SessionLoopMode.ASSIST,
        user_context={
            "preferences": {
                "operator_mode": {
                    "enabled": True,
                    "shadow_mode": True,
                    "allow_continue": False,
                    "allow_notify": True,
                }
            }
        },
    )

    with patch("zerg.services.ingest_task_queue.get_session_factory", return_value=factory):
        with patch(
            "zerg.services.session_turn_reviews.evaluate_session_turn_with_llm",
            new=AsyncMock(return_value=_continue_decision()),
        ):
            with patch("zerg.services.oikos_service.invoke_oikos", new=AsyncMock(return_value=654)) as invoke_oikos:
                await _execute_task(task_id, session_id, "turn_loop")

    tasks = _get_tasks(factory, status="done")
    reviews = _get_turn_reviews(factory)
    assert len(tasks) == 1
    assert len(reviews) == 1
    assert reviews[0].status == "enqueued"
    assert reviews[0].run_id == 654
    assert reviews[0].execution_state == "awaiting_user_approval"
    assert reviews[0].decision == "continue"
    invoke_oikos.assert_awaited_once()


@pytest.mark.asyncio
async def test_turn_loop_task_autopilot_enqueues_same_session_resume_job(tmp_path, monkeypatch):
    """Autopilot sessions enqueue a bounded same-session continue job from turn_loop."""
    from zerg.services.ingest_task_queue import _execute_task

    monkeypatch.setenv("OIKOS_OPERATOR_MODE_ENABLED", "1")
    factory = _make_db(tmp_path, "worker_operator_autopilot.db")
    ended_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    session_id, task_id, owner_id, assistant_event_id = _seed_completion_task(
        factory,
        ended_at=ended_at,
        presence_state="idle",
        presence_updated_at=ended_at,
        loop_mode=SessionLoopMode.AUTOPILOT,
        user_context={
            "preferences": {
                "operator_mode": {
                    "enabled": True,
                    "shadow_mode": True,
                    "allow_continue": True,
                    "allow_notify": True,
                }
            }
        },
    )

    with patch("zerg.services.ingest_task_queue.get_session_factory", return_value=factory):
        with patch(
            "zerg.services.session_turn_reviews.evaluate_session_turn_with_llm",
            new=AsyncMock(return_value=_continue_decision()),
        ):
            await _execute_task(task_id, session_id, "turn_loop")

    tasks = _get_tasks(factory, status="done")
    reviews = _get_turn_reviews(factory)
    db = factory()
    try:
        jobs = db.query(CommisJob).order_by(CommisJob.id.asc()).all()
    finally:
        db.close()

    assert len(tasks) == 1
    assert len(reviews) == 1
    assert owner_id > 0
    assert assistant_event_id > 0
    assert reviews[0].status == "acted"
    assert reviews[0].reason == "continue_session"
    assert reviews[0].actual_outcome == "continue_session"
    assert reviews[0].shadow_alignment == "matched"
    assert reviews[0].follow_up_prompt == "Run the pending targeted tests."
    assert len(jobs) == 1
    assert jobs[0].owner_id == owner_id
    assert jobs[0].task == "Run the pending targeted tests."
    assert jobs[0].config["execution_mode"] == "workspace"
    assert jobs[0].config["resume_session_id"] == session_id
    assert jobs[0].config["backend"] == "zai"
    assert jobs[0].config["trigger"] == "turn_loop"
    assert jobs[0].config["assistant_event_id"] == assistant_event_id


@pytest.mark.asyncio
async def test_turn_loop_task_skips_operator_for_historical_completed_session(tmp_path, monkeypatch):
    """Historical backfill should not wake operator mode."""
    from zerg.services.ingest_task_queue import _execute_task

    monkeypatch.setenv("OIKOS_OPERATOR_MODE_ENABLED", "1")
    factory = _make_db(tmp_path, "worker_operator_skip_historical.db")
    ended_at = datetime.now(timezone.utc) - timedelta(minutes=45)
    session_id, task_id, _owner_id, _assistant_event_id = _seed_completion_task(
        factory,
        ended_at=ended_at,
        presence_state=None,
    )

    with patch("zerg.services.ingest_task_queue.get_session_factory", return_value=factory):
        with patch("zerg.services.oikos_service.invoke_oikos", new_callable=AsyncMock) as invoke_oikos:
            await _execute_task(task_id, session_id, "turn_loop")

    invoke_oikos.assert_not_awaited()

    tasks = _get_tasks(factory, status="done")
    reviews = _get_turn_reviews(factory)
    assert len(tasks) == 1
    assert len(reviews) == 0


@pytest.mark.asyncio
async def test_turn_loop_task_skips_operator_when_user_policy_disables_it(tmp_path, monkeypatch):
    """User-backed operator prefs still allow review recording, but keep execution observe-only."""
    from zerg.services.ingest_task_queue import _execute_task

    monkeypatch.setenv("OIKOS_OPERATOR_MODE_ENABLED", "1")
    factory = _make_db(tmp_path, "worker_operator_skip_policy.db")
    ended_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    session_id, task_id, _owner_id, _assistant_event_id = _seed_completion_task(
        factory,
        ended_at=ended_at,
        presence_state="idle",
        presence_updated_at=ended_at,
        user_context={"preferences": {"operator_mode": {"enabled": False}}},
    )

    with patch("zerg.services.ingest_task_queue.get_session_factory", return_value=factory):
        with patch(
            "zerg.services.session_turn_reviews.evaluate_session_turn_with_llm",
            new=AsyncMock(return_value=_continue_decision()),
        ):
            await _execute_task(task_id, session_id, "turn_loop")

    tasks = _get_tasks(factory, status="done")
    reviews = _get_turn_reviews(factory)
    assert len(tasks) == 1
    assert len(reviews) == 1
    assert reviews[0].status == "recorded"
    assert reviews[0].decision == "continue"
    assert reviews[0].execution_state == "observe_only"


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

    def override_verify_agents_token():
        return SimpleNamespace(device_id="ingest-task-queue", id="token-1", owner_id=1)

    api_app.dependency_overrides[get_db] = override
    api_app.dependency_overrides[verify_agents_token] = override_verify_agents_token
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
        assert len(tasks) == 3
        assert {t.task_type for t in tasks} == {"summary", "embedding", "turn_loop"}
    finally:
        api_app.dependency_overrides.clear()
