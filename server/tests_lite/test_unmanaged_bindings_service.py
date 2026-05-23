"""Tests for the unmanaged-bindings read-side service (Phase 5c + 6).

Retired: ``UnmanagedSessionBinding`` was removed in the session-identity-
kernel cleanup. Equivalent observation evidence now lives on
``SessionConnection`` rows (acquisition_kind='observe_only',
control_plane='log_tail'). The tests below were tightly coupled to the
deleted table and have been retired pending kernel-shaped replacements.
"""

from __future__ import annotations

import pytest

pytest.skip(
    "UnmanagedSessionBinding service replaced by kernel SessionConnection projection",
    allow_module_level=True,
)


def _make_db(tmp_path):
    db_path = tmp_path / "unmanaged_bindings_service.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _make_session(db, *, provider: str = "codex", provider_session_id: str = "sess-abc") -> AgentSession:
    now = datetime.now(timezone.utc)
    session = AgentSession(
        id=uuid4(),
        provider=provider,
        environment="laptop",
        started_at=now,
        last_activity_at=now,
        provider_session_id=provider_session_id,
        thread_root_session_id=None,
        user_messages=0,
        assistant_messages=0,
        tool_calls=0,
        is_writable_head=1,
    )
    db.add(session)
    db.flush()
    return session


def test_no_binding_means_no_overlay_entry(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        session = _make_session(db)
        db.commit()

        overlay = load_binding_overlay(db, [session.id])
        assert session.id not in overlay
        assert overlay == {}


def test_fresh_heartbeat_and_fresh_binding_is_online_open(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        session = _make_session(db)
        now = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
        db.add(AgentHeartbeat(device_id="cinder", received_at=now - timedelta(seconds=10)))
        db.add(
            UnmanagedSessionBinding(
                machine_id="cinder",
                device_id="cinder",
                provider="codex",
                provider_session_id="sess-abc",
                session_id=session.id,
                pid=1234,
                process_start_time=now - timedelta(hours=1),
                observed_at=now - timedelta(seconds=15),
                last_seen_at=now - timedelta(seconds=15),
                source_mtime=now - timedelta(seconds=10),
                binding_state="observed",
            )
        )
        db.commit()

        overlay = load_binding_overlay(db, [session.id], now=now)
        assert session.id in overlay
        entry = overlay[session.id]
        assert entry.host_state == "online"
        assert entry.terminal_reason is None


def test_stale_host_yields_stale_host_state_no_closure(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        session = _make_session(db)
        now = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
        # Between HOST_ONLINE_WINDOW (10m) and HOST_STALE_WINDOW (30m).
        db.add(AgentHeartbeat(device_id="cinder", received_at=now - timedelta(minutes=20)))
        db.add(
            UnmanagedSessionBinding(
                machine_id="cinder",
                device_id="cinder",
                provider="codex",
                provider_session_id="sess-abc",
                session_id=session.id,
                pid=1234,
                process_start_time=now - timedelta(hours=2),
                observed_at=now - timedelta(minutes=20),
                last_seen_at=now - timedelta(minutes=20),
                binding_state="observed",
            )
        )
        db.commit()

        overlay = load_binding_overlay(db, [session.id], now=now)
        assert overlay[session.id].host_state == "stale"
        assert overlay[session.id].terminal_reason is None


def test_offline_host_under_expiry_yields_offline_no_closure(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        session = _make_session(db)
        now = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
        db.add(AgentHeartbeat(device_id="cinder", received_at=now - timedelta(hours=2)))
        db.add(
            UnmanagedSessionBinding(
                machine_id="cinder",
                device_id="cinder",
                provider="codex",
                provider_session_id="sess-abc",
                session_id=session.id,
                pid=1234,
                process_start_time=now - timedelta(hours=3),
                observed_at=now - timedelta(hours=2),
                last_seen_at=now - timedelta(hours=2),
                binding_state="observed",
            )
        )
        db.commit()

        overlay = load_binding_overlay(db, [session.id], now=now)
        assert overlay[session.id].host_state == "offline"
        assert overlay[session.id].terminal_reason is None


def test_offline_host_past_expiry_promotes_host_expired_not_process_gone(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        session = _make_session(db)
        now = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
        db.add(AgentHeartbeat(device_id="cinder", received_at=now - timedelta(days=8)))
        db.add(
            UnmanagedSessionBinding(
                machine_id="cinder",
                device_id="cinder",
                provider="codex",
                provider_session_id="sess-abc",
                session_id=session.id,
                pid=1234,
                process_start_time=now - timedelta(days=9),
                observed_at=now - timedelta(days=8),
                last_seen_at=now - timedelta(days=8),
                binding_state="observed",
            )
        )
        db.commit()

        overlay = load_binding_overlay(db, [session.id], now=now)
        entry = overlay[session.id]
        assert entry.host_state == "offline"
        assert entry.terminal_reason == "host_expired"


def test_online_host_with_old_binding_promotes_process_gone(tmp_path):
    """Phase 6: when the host is still online but the binding hasn't been
    re-observed in the latest heartbeat window AND the transcript has
    stopped growing, the process is gone."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        session = _make_session(db)
        now = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
        db.add(AgentHeartbeat(device_id="cinder", received_at=now - timedelta(seconds=5)))
        db.add(
            UnmanagedSessionBinding(
                machine_id="cinder",
                device_id="cinder",
                provider="codex",
                provider_session_id="sess-abc",
                session_id=session.id,
                pid=1234,
                process_start_time=now - timedelta(hours=2),
                observed_at=now - timedelta(hours=2),
                last_seen_at=now - timedelta(hours=2),
                source_mtime=now - timedelta(hours=2),
                binding_state="observed",
            )
        )
        db.commit()

        overlay = load_binding_overlay(db, [session.id], now=now)
        entry = overlay[session.id]
        assert entry.host_state == "online"
        assert entry.terminal_reason == "process_gone"


def test_growing_transcript_stays_open_even_with_old_binding(tmp_path):
    """If the fd is closed between writes but the JSONL is still
    growing, do NOT promote process_gone."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        session = _make_session(db)
        now = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
        db.add(AgentHeartbeat(device_id="cinder", received_at=now - timedelta(seconds=5)))
        db.add(
            UnmanagedSessionBinding(
                machine_id="cinder",
                device_id="cinder",
                provider="codex",
                provider_session_id="sess-abc",
                session_id=session.id,
                pid=1234,
                process_start_time=now - timedelta(hours=2),
                observed_at=now - timedelta(minutes=20),
                last_seen_at=now - timedelta(minutes=20),
                source_mtime=now - timedelta(seconds=30),  # transcript still fresh
                binding_state="observed",
            )
        )
        db.commit()

        overlay = load_binding_overlay(db, [session.id], now=now)
        assert overlay[session.id].terminal_reason is None


def test_stale_binding_state_promotes_process_gone_regardless_of_host(tmp_path):
    """An engine can flag a binding 'stale' explicitly (superseded or
    expired); we trust that even if the heartbeat is old."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        session = _make_session(db)
        now = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
        # No fresh heartbeat — host would be 'unknown'.
        db.add(
            UnmanagedSessionBinding(
                machine_id="cinder",
                provider="codex",
                provider_session_id="sess-abc",
                session_id=session.id,
                pid=1234,
                process_start_time=now - timedelta(hours=1),
                observed_at=now - timedelta(minutes=20),
                last_seen_at=now - timedelta(minutes=20),
                binding_state="stale",
            )
        )
        db.commit()

        overlay = load_binding_overlay(db, [session.id], now=now)
        entry = overlay[session.id]
        assert entry.terminal_reason == "process_gone"
