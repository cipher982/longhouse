"""Capability matrix for the kernel projection.

Every combination of (thread, run, best connection) must produce a
deterministic ``KernelSessionCapabilities`` payload. These tests are the
source of truth for the projection's behavior.

See docs/specs/session-identity-kernel.md.
"""

from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pytest
from sqlalchemy.orm import sessionmaker

from zerg.database import Base
from zerg.database import make_engine
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionConnection
from zerg.models.agents import SessionRun
from zerg.models.agents import SessionThread
from zerg.services.agents.kernel_capabilities import KernelSessionCapabilities
from zerg.services.agents.kernel_capabilities import project_session_capabilities


def _engine(tmp_path):
    db_path = tmp_path / "kernel.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return engine


def _make_session(db, *, provider="codex"):
    s = AgentSession(
        provider=provider,
        environment="test",
        project="zerg",
        device_id="dev",
        started_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
    )
    db.add(s)
    db.flush()
    return s


def _make_thread(db, session, *, primary=1):
    t = SessionThread(
        session_id=session.id,
        provider=session.provider,
        branch_kind="root",
        is_primary=primary,
    )
    db.add(t)
    db.flush()
    return t


def _make_run(db, thread, **overrides):
    r = SessionRun(
        thread_id=thread.id,
        provider=thread.provider,
        host_id=overrides.pop("host_id", "laptop-1"),
        launch_origin=overrides.pop("launch_origin", "longhouse_spawned"),
        started_at=overrides.pop("started_at", datetime.now(timezone.utc)),
        ended_at=overrides.pop("ended_at", None),
    )
    db.add(r)
    db.flush()
    return r


def _make_conn(db, run, *, control_plane="pty", state="attached", caps=None, **overrides):
    caps = caps or {}
    c = SessionConnection(
        run_id=run.id,
        control_plane=control_plane,
        acquisition_kind=overrides.pop("acquisition_kind", "spawned_control"),
        state=state,
        can_send_input=int(caps.get("send", 0)),
        can_interrupt=int(caps.get("interrupt", 0)),
        can_terminate=int(caps.get("terminate", 0)),
        can_tail_output=int(caps.get("tail", 0)),
        can_resume=int(caps.get("resume", 0)),
        last_health_at=overrides.pop("last_health_at", datetime.now(timezone.utc)),
    )
    db.add(c)
    db.flush()
    return c


@pytest.fixture
def db(tmp_path):
    engine = _engine(tmp_path)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        yield db


def test_imported_session_no_thread(db):
    s = _make_session(db)
    db.commit()
    caps = project_session_capabilities(db, session_id=s.id)
    assert caps.control_label == "imported"
    assert caps.live_control_available is False
    assert caps.host_reattach_available is False
    assert caps.search_only is True
    assert caps.staleness_reason == "imported_only"


def test_thread_but_no_run(db):
    s = _make_session(db)
    _make_thread(db, s)
    db.commit()
    caps = project_session_capabilities(db, session_id=s.id)
    assert caps.control_label == "imported"
    assert caps.thread_id is not None
    assert caps.run_id is None
    assert caps.staleness_reason == "no_run"


def test_run_but_no_connection(db):
    s = _make_session(db)
    t = _make_thread(db, s)
    _make_run(db, t)
    db.commit()
    caps = project_session_capabilities(db, session_id=s.id)
    assert caps.control_label == "imported"
    assert caps.run_id is not None
    assert caps.connection_id is None
    assert caps.staleness_reason == "no_connection"


def test_managed_attached_grants_live(db):
    s = _make_session(db)
    t = _make_thread(db, s)
    r = _make_run(db, t)
    _make_conn(db, r, state="attached", caps={"send": 1, "interrupt": 1, "tail": 1})
    db.commit()
    caps = project_session_capabilities(db, session_id=s.id)
    assert caps.control_label == "live"
    assert caps.live_control_available is True
    assert caps.can_send_input is True
    assert caps.can_interrupt is True
    assert caps.can_tail_output is True


def test_managed_degraded_still_live(db):
    s = _make_session(db)
    t = _make_thread(db, s)
    r = _make_run(db, t)
    _make_conn(db, r, state="degraded", caps={"send": 1, "tail": 1})
    db.commit()
    caps = project_session_capabilities(db, session_id=s.id)
    assert caps.control_label == "live"
    assert caps.live_control_available is True


def test_managed_detached_offers_reattach(db):
    s = _make_session(db)
    t = _make_thread(db, s)
    r = _make_run(db, t)
    _make_conn(db, r, state="detached", caps={"send": 1, "interrupt": 1, "tail": 1})
    db.commit()
    caps = project_session_capabilities(db, session_id=s.id)
    assert caps.control_label == "reattach"
    assert caps.live_control_available is False
    assert caps.host_reattach_available is True
    # send/interrupt are gated off when not actually live
    assert caps.can_send_input is False


def test_managed_process_ended_imports(db):
    s = _make_session(db)
    t = _make_thread(db, s)
    r = _make_run(db, t, ended_at=datetime.now(timezone.utc))
    _make_conn(db, r, state="ended", caps={"tail": 1})
    db.commit()
    caps = project_session_capabilities(db, session_id=s.id)
    assert caps.control_label == "imported"
    assert caps.live_control_available is False
    assert caps.host_reattach_available is False


def test_unmanaged_log_tail_observe_only(db):
    """log_tail observe_only connection — search-only, can_tail surfaces."""
    s = _make_session(db)
    t = _make_thread(db, s)
    r = _make_run(db, t, launch_origin="external_adopted")
    _make_conn(db, r, control_plane="log_tail", acquisition_kind="observe_only", state="attached", caps={"tail": 1})
    db.commit()
    caps = project_session_capabilities(db, session_id=s.id)
    assert caps.control_label == "search-only"
    assert caps.live_control_available is False
    assert caps.observe_only is True
    assert caps.can_tail_output is True
    assert caps.can_send_input is False


def test_subagent_thread_does_not_become_session_primary(db):
    """A non-primary thread must not be picked as the session's projection."""
    s = _make_session(db)
    t = _make_thread(db, s, primary=1)
    subagent = SessionThread(session_id=s.id, provider=s.provider, branch_kind="subagent", is_primary=0)
    db.add(subagent)
    db.flush()
    # Subagent has its own run + attached connection
    sub_run = _make_run(db, subagent)
    _make_conn(db, sub_run, state="attached", caps={"send": 1, "tail": 1})
    db.commit()
    caps = project_session_capabilities(db, session_id=s.id)
    # Primary thread has no run → projection reports "no_run", not the subagent's live state.
    assert caps.thread_id == str(t.id)
    assert caps.control_label == "imported"
    assert caps.staleness_reason == "no_run"


def test_best_connection_state_priority(db):
    """attached beats degraded beats detached, regardless of insertion order."""
    s = _make_session(db)
    t = _make_thread(db, s)
    r = _make_run(db, t)
    # Insert detached first, then attached — projection must prefer attached.
    _make_conn(db, r, state="detached", caps={"send": 1, "interrupt": 1, "tail": 1})
    _make_conn(db, r, state="attached", control_plane="pty2", caps={"send": 1, "tail": 1})
    db.commit()
    caps = project_session_capabilities(db, session_id=s.id)
    assert caps.control_label == "live"
    assert caps.connection_state == "attached"


def test_best_connection_capability_count_tiebreak(db):
    """Same state — connection with more granted capability flags wins."""
    s = _make_session(db)
    t = _make_thread(db, s)
    r = _make_run(db, t)
    _make_conn(db, r, state="attached", control_plane="log_tail", caps={"tail": 1})
    _make_conn(db, r, state="attached", control_plane="pty", caps={"send": 1, "interrupt": 1, "tail": 1})
    db.commit()
    caps = project_session_capabilities(db, session_id=s.id)
    assert caps.control_label == "live"
    assert caps.control_plane == "pty"
    assert caps.can_send_input is True


def test_best_connection_recency_tiebreak(db):
    """Same state, same caps — newer last_health_at wins."""
    s = _make_session(db)
    t = _make_thread(db, s)
    r = _make_run(db, t)
    older = datetime.now(timezone.utc) - timedelta(minutes=10)
    newer = datetime.now(timezone.utc)
    _make_conn(db, r, state="attached", control_plane="a", caps={"tail": 1}, last_health_at=older)
    fresh = _make_conn(db, r, state="attached", control_plane="b", caps={"tail": 1}, last_health_at=newer)
    db.commit()
    caps = project_session_capabilities(db, session_id=s.id)
    assert caps.connection_id == fresh.id


def test_latest_run_wins_over_old_run(db):
    """Resumed session: stale ended run with attached connection must not win."""
    s = _make_session(db)
    t = _make_thread(db, s)
    old_run = _make_run(
        db, t,
        started_at=datetime.now(timezone.utc) - timedelta(hours=2),
        ended_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    _make_conn(db, old_run, state="ended", caps={"tail": 1})
    new_run = _make_run(db, t, started_at=datetime.now(timezone.utc))
    _make_conn(db, new_run, state="attached", caps={"send": 1, "tail": 1})
    db.commit()
    caps = project_session_capabilities(db, session_id=s.id)
    assert caps.run_id == str(new_run.id)
    assert caps.control_label == "live"


def test_payload_is_pure_function_of_kernel_rows(db):
    """No mutation of session/thread/run/connection rows during projection."""
    s = _make_session(db)
    t = _make_thread(db, s)
    r = _make_run(db, t)
    c = _make_conn(db, r, state="attached", caps={"send": 1, "tail": 1})
    db.commit()
    snap_thread_id = t.id
    snap_run_id = r.id
    snap_conn_id = c.id
    project_session_capabilities(db, session_id=s.id)
    project_session_capabilities(db, session_id=s.id)
    db.refresh(t)
    db.refresh(r)
    db.refresh(c)
    assert t.id == snap_thread_id
    assert r.id == snap_run_id
    assert c.id == snap_conn_id


def test_imported_returns_full_payload_shape(db):
    """Even fully-imported sessions return the same field set."""
    s = _make_session(db)
    db.commit()
    caps = project_session_capabilities(db, session_id=s.id)
    assert isinstance(caps, KernelSessionCapabilities)
    # All boolean fields populated, no None
    for field in ("live_control_available", "host_reattach_available", "observe_only",
                  "search_only", "can_send_input", "can_interrupt", "can_terminate",
                  "can_tail_output", "can_resume"):
        assert isinstance(getattr(caps, field), bool)
