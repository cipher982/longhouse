from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from uuid import uuid4

import pytest

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionTask
from zerg.services.session_enrichment_reconciler import active_summary_session_ids
from zerg.services.session_enrichment_reconciler import reconcile_summaries_once
from zerg.services.session_enrichment_reconciler import select_initial_title_session_ids
from zerg.services.session_enrichment_reconciler import select_stale_summary_session_ids
from zerg.services.write_serializer import get_write_serializer


def _make_db(tmp_path, name: str = "session_enrichment_reconciler.db", **engine_kwargs):
    db_path = tmp_path / name
    engine = make_engine(f"sqlite:///{db_path}", **engine_kwargs)
    Base.metadata.create_all(bind=engine)
    factory = make_sessionmaker(engine)
    get_write_serializer().configure(factory)
    return engine, factory


def _seed_session(
    db,
    *,
    project: str = "zerg",
    environment: str = "cinder",
    cwd: str | None = None,
    summary: str | None = None,
    summary_title: str | None = None,
    first_user_message_preview: str | None = None,
    user_messages: int = 2,
    assistant_messages: int = 2,
    transcript_revision: int = 3,
    summary_revision: int = 0,
    last_activity_at: datetime | None = None,
) -> AgentSession:
    now = datetime.now(timezone.utc)
    session = AgentSession(
        provider="codex",
        environment=environment,
        project=project,
        cwd=cwd,
        started_at=last_activity_at or now,
        last_activity_at=last_activity_at or now,
        user_messages=user_messages,
        assistant_messages=assistant_messages,
        tool_calls=0,
        summary=summary,
        summary_title=summary_title,
        first_user_message_preview=first_user_message_preview,
        transcript_revision=transcript_revision,
        summary_revision=summary_revision,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _seed_summary_task(db, session: AgentSession, *, status: str = "pending") -> None:
    db.add(
        SessionTask(
            id=str(uuid4()),
            session_id=str(session.id),
            task_type="summary",
            status=status,
        )
    )
    db.commit()


def test_select_stale_summary_sessions_orders_missing_title_then_recency(tmp_path):
    _, factory = _make_db(tmp_path)
    db = factory()
    try:
        old = datetime.now(timezone.utc) - timedelta(days=3)
        recent = datetime.now(timezone.utc) - timedelta(minutes=1)
        missing_old = _seed_session(db, project="missing-old", summary_title=None, last_activity_at=old)
        titled_recent = _seed_session(db, project="titled-recent", summary_title="Has title", last_activity_at=recent)
        missing_recent = _seed_session(db, project="missing-recent", summary_title=None, last_activity_at=recent)

        assert select_stale_summary_session_ids(db, limit=10) == [
            str(missing_recent.id),
            str(missing_old.id),
            str(titled_recent.id),
        ]
    finally:
        db.close()


def test_select_stale_summary_sessions_skips_test_and_provider_proof_rows(tmp_path):
    _, factory = _make_db(tmp_path)
    db = factory()
    try:
        normal = _seed_session(db, project="normal")
        _seed_session(db, project="test-row", environment="test")
        _seed_session(
            db,
            project="proof-row",
            cwd="/Users/david/.longhouse/canaries/provider-live/opencode/proof/workspace",
        )

        assert select_stale_summary_session_ids(db, limit=10) == [str(normal.id)]
    finally:
        db.close()


def test_select_initial_title_debt_ignores_summary_completion_and_honors_retry(tmp_path):
    _, factory = _make_db(tmp_path)
    db = factory()
    try:
        session = _seed_session(
            db,
            summary_title="zerg",
            user_messages=1,
            assistant_messages=1,
            transcript_revision=3,
            summary_revision=3,
        )

        assert select_initial_title_session_ids(db, limit=10) == [str(session.id)]

        session.title_retry_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        db.commit()
        assert select_initial_title_session_ids(db, limit=10) == []

        session.title_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        assert select_initial_title_session_ids(db, limit=10) == [str(session.id)]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_low_content_title_debt_is_not_hidden_by_summary_fast_forward(tmp_path):
    _, factory = _make_db(tmp_path)
    db = factory()
    try:
        session = _seed_session(
            db,
            user_messages=1,
            assistant_messages=0,
            transcript_revision=4,
            summary_revision=0,
        )
        session_id = str(session.id)
    finally:
        db.close()

    async def _generate(_session_id: str) -> None:
        raise AssertionError("low-content session should not call summary provider")

    async def _generate_initial_title(selected_session_id: str) -> bool:
        assert selected_session_id == session_id
        return False

    result = await reconcile_summaries_once(
        session_factory=factory,
        limit=10,
        concurrency=2,
        generate_summary=_generate,
        generate_initial_title=_generate_initial_title,
    )
    assert result.initial_selected == 1
    assert result.initial_started == 1
    assert result.initial_titled == 0
    assert result.fast_forwarded == 0
    assert result.started == 0

    verify = factory()
    try:
        refreshed = verify.get(AgentSession, session_id)
        assert refreshed is not None
        assert refreshed.summary_revision == 0
    finally:
        verify.close()


@pytest.mark.asyncio
async def test_low_content_sessions_try_initial_title_before_fast_forward(tmp_path):
    _, factory = _make_db(tmp_path)
    db = factory()
    try:
        session = _seed_session(
            db,
            first_user_message_preview="Make the menu bar row affordance obvious.",
            user_messages=1,
            assistant_messages=0,
            transcript_revision=4,
            summary_revision=0,
        )
        session_id = str(session.id)
    finally:
        db.close()

    titled: list[str] = []

    async def _generate_initial_title(selected_session_id: str) -> bool:
        titled.append(selected_session_id)
        write_db = factory()
        try:
            target = write_db.get(AgentSession, selected_session_id)
            target.summary_title = "Menu Bar Row Affordance"
            target.summary_revision = target.transcript_revision
            write_db.commit()
        finally:
            write_db.close()
        return True

    async def _generate_summary(_session_id: str) -> None:
        raise AssertionError("low-content session should not call full summary provider")

    result = await reconcile_summaries_once(
        session_factory=factory,
        limit=10,
        concurrency=2,
        generate_summary=_generate_summary,
        generate_initial_title=_generate_initial_title,
    )

    assert titled == [session_id]
    assert result.initial_selected == 1
    assert result.initial_started == 1
    assert result.initial_titled == 1
    assert result.fast_forwarded == 0
    assert result.started == 0

    verify = factory()
    try:
        refreshed = verify.get(AgentSession, session_id)
        assert refreshed is not None
        assert refreshed.summary_title == "Menu Bar Row Affordance"
        assert refreshed.summary_revision == 4
    finally:
        verify.close()


@pytest.mark.asyncio
async def test_low_content_test_sessions_skip_initial_title(tmp_path):
    _, factory = _make_db(tmp_path)
    db = factory()
    try:
        session = _seed_session(
            db,
            first_user_message_preview="LONGHOUSE_OPENCODE_NOREPLY_skip_title",
            user_messages=1,
            assistant_messages=0,
            transcript_revision=4,
            summary_revision=0,
        )
        session.environment = "test"
        db.commit()
        session_id = str(session.id)
    finally:
        db.close()

    async def _generate_initial_title(_session_id: str) -> bool:
        raise AssertionError("test/provider-proof session should not call title provider")

    async def _generate_summary(_session_id: str) -> None:
        raise AssertionError("low-content session should not call full summary provider")

    result = await reconcile_summaries_once(
        session_factory=factory,
        limit=10,
        concurrency=2,
        generate_summary=_generate_summary,
        generate_initial_title=_generate_initial_title,
    )

    assert result.initial_selected == 0
    assert result.initial_started == 0
    assert result.initial_titled == 0
    # Fast-forwarding is a local DB revision catch-up, not provider work; test
    # sessions still use it so they do not remain permanently stale.
    assert result.fast_forwarded == 1
    assert result.started == 0

    verify = factory()
    try:
        refreshed = verify.get(AgentSession, session_id)
        assert refreshed is not None
        assert refreshed.summary_title is None
        assert refreshed.summary_revision == 4
    finally:
        verify.close()


@pytest.mark.asyncio
async def test_failed_initial_title_attempt_is_not_fast_forwarded(tmp_path):
    _, factory = _make_db(tmp_path)
    db = factory()
    try:
        session = _seed_session(
            db,
            first_user_message_preview="Make the menu bar row affordance obvious.",
            user_messages=1,
            assistant_messages=0,
            transcript_revision=4,
            summary_revision=0,
        )
        session_id = str(session.id)
    finally:
        db.close()

    async def _generate_initial_title(selected_session_id: str) -> bool:
        assert selected_session_id == session_id
        return False

    async def _generate_summary(_session_id: str) -> None:
        raise AssertionError("low-content session should not call full summary provider")

    result = await reconcile_summaries_once(
        session_factory=factory,
        limit=10,
        concurrency=2,
        generate_summary=_generate_summary,
        generate_initial_title=_generate_initial_title,
    )

    assert result.initial_selected == 1
    assert result.initial_started == 1
    assert result.initial_titled == 0
    assert result.fast_forwarded == 0
    assert result.started == 0

    verify = factory()
    try:
        refreshed = verify.get(AgentSession, session_id)
        assert refreshed is not None
        assert refreshed.summary_title is None
        assert refreshed.summary_revision == 0
    finally:
        verify.close()


@pytest.mark.asyncio
async def test_concurrent_scans_do_not_duplicate_provider_calls(tmp_path):
    _, factory = _make_db(tmp_path)
    db = factory()
    try:
        session = _seed_session(db)
        session_id = str(session.id)
    finally:
        db.close()

    entered = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def _generate(generated_session_id: str) -> None:
        calls.append(generated_session_id)
        entered.set()
        await release.wait()

    first = asyncio.create_task(
        reconcile_summaries_once(session_factory=factory, limit=10, concurrency=1, generate_summary=_generate)
    )
    await entered.wait()

    second = await reconcile_summaries_once(
        session_factory=factory,
        limit=10,
        concurrency=1,
        generate_summary=_generate,
    )
    assert second.started == 0
    assert calls == [session_id]

    release.set()
    first_result = await first
    assert first_result.started == 1
    assert await active_summary_session_ids() == set()


@pytest.mark.asyncio
async def test_summary_reconciler_respects_concurrency_cap(tmp_path):
    _, factory = _make_db(tmp_path)
    db = factory()
    try:
        for idx in range(5):
            _seed_session(db, project=f"session-{idx}")
    finally:
        db.close()

    current = 0
    max_seen = 0
    lock = asyncio.Lock()

    async def _generate(_session_id: str) -> None:
        nonlocal current, max_seen
        async with lock:
            current += 1
            max_seen = max(max_seen, current)
        await asyncio.sleep(0.02)
        async with lock:
            current -= 1

    result = await reconcile_summaries_once(
        session_factory=factory,
        limit=10,
        concurrency=2,
        generate_summary=_generate,
    )
    assert result.started == 5
    assert max_seen <= 2


@pytest.mark.asyncio
async def test_summary_reconciler_releases_active_claim_on_cancellation(tmp_path):
    _, factory = _make_db(tmp_path)
    db = factory()
    try:
        session = _seed_session(db)
        session_id = str(session.id)
    finally:
        db.close()

    entered = asyncio.Event()

    async def _generate(_session_id: str) -> None:
        entered.set()
        await asyncio.sleep(60)

    task = asyncio.create_task(
        reconcile_summaries_once(session_factory=factory, limit=10, concurrency=1, generate_summary=_generate)
    )
    await entered.wait()
    assert await active_summary_session_ids() == {session_id}

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await active_summary_session_ids() == set()


@pytest.mark.asyncio
async def test_summary_reconciler_finishes_siblings_before_raising_provider_error(tmp_path):
    _, factory = _make_db(tmp_path)
    db = factory()
    try:
        sessions = [_seed_session(db, project=f"session-{idx}") for idx in range(2)]
        session_ids = {str(session.id) for session in sessions}
        fail_id = str(sessions[0].id)
    finally:
        db.close()

    calls: set[str] = set()

    async def _generate(session_id: str) -> None:
        calls.add(session_id)
        await asyncio.sleep(0.01)
        if session_id == fail_id:
            raise RuntimeError("provider failed")

    with pytest.raises(RuntimeError, match="provider failed"):
        await reconcile_summaries_once(
            session_factory=factory,
            limit=10,
            concurrency=2,
            generate_summary=_generate,
        )

    assert calls == session_ids
    assert await active_summary_session_ids() == set()


@pytest.mark.asyncio
async def test_old_summary_task_backlog_is_ignored(tmp_path):
    _, factory = _make_db(tmp_path)
    db = factory()
    try:
        session = _seed_session(db)
        session_id = str(session.id)
        for _ in range(20):
            _seed_summary_task(db, session, status="pending")
    finally:
        db.close()

    calls: list[str] = []

    async def _generate(generated_session_id: str) -> None:
        calls.append(generated_session_id)

    result = await reconcile_summaries_once(
        session_factory=factory,
        limit=10,
        concurrency=2,
        generate_summary=_generate,
    )
    assert result.started == 1
    assert calls == [session_id]


@pytest.mark.asyncio
async def test_summary_reconciler_does_not_hold_db_connection_during_provider_call(tmp_path):
    engine, factory = _make_db(
        tmp_path,
        "summary_reconciler_connection_release.db",
        pool_size=1,
        max_overflow=0,
    )
    db = factory()
    try:
        _seed_session(db)
    finally:
        db.close()

    checked_out: list[int] = []

    async def _generate(_session_id: str) -> None:
        checked_out.append(engine.pool.checkedout())

    result = await reconcile_summaries_once(
        session_factory=factory,
        limit=10,
        concurrency=1,
        generate_summary=_generate,
    )
    assert result.started == 1
    assert checked_out == [0]
