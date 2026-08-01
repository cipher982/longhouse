"""Console unread acknowledgement (docs/specs/console-unread-acknowledgement.md).

Unread is derived, never stored: settle paths stamp last_console_result_at on
the session row, mark-read writes last_read_at from the client's observed
read_through, and the contract compares the two at projection time.
"""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from uuid import uuid4

import pytest

from zerg.database import Base
from zerg.database import make_engine
from zerg.database import make_sessionmaker
from zerg.models.agents import AgentSession
from zerg.services.agents.kernel_writes import ensure_primary_thread
from zerg.services.agents.kernel_writes import set_thread_execution_target
from zerg.services.console_turns import begin_console_turn_drain
from zerg.services.console_turns import claim_next_console_turn
from zerg.services.console_turns import enqueue_console_turn
from zerg.services.console_turns import mark_console_turn_active
from zerg.services.console_turns import settle_console_turn
from zerg.services.session_state_contract import _unread


def _db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'console-unread.db'}")
    Base.metadata.create_all(bind=engine)
    return make_sessionmaker(engine)()


def _session(db):
    session = AgentSession(
        id=uuid4(),
        provider="codex",
        environment="test",
        project="longhouse",
        origin_kind="console",
        started_at=datetime.now(timezone.utc),
        user_messages=0,
        assistant_messages=0,
        tool_calls=0,
    )
    db.add(session)
    db.flush()
    return session


def _run_console_turn_to(db, session, *, outcome: str | None = None, stop_at: str = "settled"):
    thread = ensure_primary_thread(db, session)
    set_thread_execution_target(thread, device_id="cinder", cwd="/tmp/longhouse")
    db.commit()
    enqueue_console_turn(db, session=session, owner_id=1, message="go", client_request_id=uuid4().hex)
    claimed = claim_next_console_turn(db, thread_id=thread.id)
    mark_console_turn_active(db, turn_id=claimed.turn_id)
    if stop_at == "active":
        return claimed
    begin_console_turn_drain(db, turn_id=claimed.turn_id)
    if stop_at == "draining":
        return claimed
    settle_console_turn(db, turn_id=claimed.turn_id, outcome=outcome or "completed")
    return claimed


# ---------------------------------------------------------------------------
# Derivation


def test_no_console_result_is_never_unread():
    assert _unread(last_console_result_at=None, last_read_at=None) is False
    assert _unread(last_console_result_at=None, last_read_at=datetime.now(timezone.utc)) is False


def test_unacknowledged_result_is_unread():
    now = datetime.now(timezone.utc)
    assert _unread(last_console_result_at=now, last_read_at=None) is True
    assert _unread(last_console_result_at=now, last_read_at=now - timedelta(minutes=5)) is True


def test_read_at_or_after_result_is_read():
    now = datetime.now(timezone.utc)
    assert _unread(last_console_result_at=now, last_read_at=now) is False
    assert _unread(last_console_result_at=now, last_read_at=now + timedelta(seconds=1)) is False


# ---------------------------------------------------------------------------
# Settle stamping


def test_settle_stamps_console_result_on_session(tmp_path):
    db = _db(tmp_path)
    session = _session(db)
    _run_console_turn_to(db, session, outcome="completed")

    db.refresh(session)
    assert session.last_console_result_at is not None
    assert session.last_console_result_outcome == "completed"


def test_failed_settle_stamps_failed_outcome(tmp_path):
    db = _db(tmp_path)
    session = _session(db)
    _run_console_turn_to(db, session, outcome="failed")

    db.refresh(session)
    assert session.last_console_result_outcome == "failed"


def test_draining_turn_does_not_stamp(tmp_path):
    # begin_console_turn_drain sets terminal_at early; the unread stamp must
    # wait for the terminal settle or unread flips on before completion.
    db = _db(tmp_path)
    session = _session(db)
    _run_console_turn_to(db, session, stop_at="draining")

    db.refresh(session)
    assert session.last_console_result_at is None
    assert session.last_console_result_outcome is None


# ---------------------------------------------------------------------------
# Mark-read endpoint (cold/sqlite path)


@pytest.mark.asyncio
async def test_mark_read_is_a_max_write(tmp_path, monkeypatch):
    import zerg.database as database_module
    from zerg.routers.agents_sessions import mark_session_read
    from zerg.services.session_views import SessionReadRequest

    db = _db(tmp_path)
    session = _session(db)
    _run_console_turn_to(db, session, outcome="completed")
    db.refresh(session)
    result_at = session.last_console_result_at

    monkeypatch.setattr(database_module, "live_catalog_enabled", lambda: False)

    response = await mark_session_read(
        session_id=session.id,
        body=SessionReadRequest(read_through=result_at),
        db=db,
        _auth=None,
        _single=None,
    )
    db.refresh(session)
    assert session.last_read_at is not None
    assert response.last_read_at == session.last_read_at.replace(tzinfo=timezone.utc)

    # Older read_through never moves last_read_at backwards.
    earlier = result_at - timedelta(minutes=10)
    await mark_session_read(
        session_id=session.id,
        body=SessionReadRequest(read_through=earlier),
        db=db,
        _auth=None,
        _single=None,
    )
    db.refresh(session)
    assert session.last_read_at >= result_at


@pytest.mark.asyncio
async def test_mark_read_clears_unread_only_up_to_observed_result(tmp_path, monkeypatch):
    import zerg.database as database_module
    from zerg.routers.agents_sessions import mark_session_read
    from zerg.services.session_views import SessionReadRequest

    db = _db(tmp_path)
    session = _session(db)
    _run_console_turn_to(db, session, outcome="completed")
    db.refresh(session)
    observed = session.last_console_result_at

    monkeypatch.setattr(database_module, "live_catalog_enabled", lambda: False)
    await mark_session_read(
        session_id=session.id,
        body=SessionReadRequest(read_through=observed),
        db=db,
        _auth=None,
        _single=None,
    )
    db.refresh(session)
    assert _unread(last_console_result_at=session.last_console_result_at, last_read_at=session.last_read_at) is False

    # A newer result the client never saw re-derives unread; the earlier
    # acknowledgement cannot clear it (the Sol race).
    session.last_console_result_at = observed + timedelta(minutes=1)
    db.commit()
    db.refresh(session)
    assert _unread(last_console_result_at=session.last_console_result_at, last_read_at=session.last_read_at) is True


# ---------------------------------------------------------------------------
# Listing union


def test_unread_sessions_union_into_listing_past_days_back(tmp_path):
    from zerg.services.timeline_session_listing import TimelineSessionListParams
    from zerg.services.timeline_session_listing import _unread_thread_rows

    db = _db(tmp_path)
    session = _session(db)
    _run_console_turn_to(db, session, outcome="completed")
    db.commit()

    params = TimelineSessionListParams(
        project=None,
        provider=None,
        environment=None,
        include_test=True,
        hide_autonomous=False,
        device_id=None,
        days_back=7,
        query=None,
        limit=20,
        offset=0,
        sort=None,
        mode=None,
        context_mode="forensic",
    )
    rows = _unread_thread_rows(db, params=params, exclude=())
    assert [row[1] for row in rows] == [str(session.id)]

    # Already-listed sessions are not duplicated.
    assert _unread_thread_rows(db, params=params, exclude=rows) == ()

    # Archived is the manual escape hatch: it leaves the union.
    session.user_state = "archived"
    db.commit()
    assert _unread_thread_rows(db, params=params, exclude=()) == ()


def test_read_session_leaves_the_union(tmp_path):
    from zerg.services.timeline_session_listing import TimelineSessionListParams
    from zerg.services.timeline_session_listing import _unread_thread_rows

    db = _db(tmp_path)
    session = _session(db)
    _run_console_turn_to(db, session, outcome="completed")
    db.refresh(session)
    session.last_read_at = session.last_console_result_at
    db.commit()

    params = TimelineSessionListParams(
        project=None,
        provider=None,
        environment=None,
        include_test=True,
        hide_autonomous=False,
        device_id=None,
        days_back=7,
        query=None,
        limit=20,
        offset=0,
        sort=None,
        mode=None,
        context_mode="forensic",
    )
    assert _unread_thread_rows(db, params=params, exclude=()) == ()
