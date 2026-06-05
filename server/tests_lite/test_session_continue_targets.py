"""Unit tests for the shared native-continue resolver."""

from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from uuid import uuid4

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from sqlalchemy.orm import sessionmaker

from zerg.database import Base
from zerg.database import make_engine
from zerg.models.agents import AgentSession
from zerg.models.agents import AgentSourceLine
from zerg.services.agents.kernel_writes import ensure_primary_thread
from zerg.services.agents.kernel_writes import record_run
from zerg.services.agents.kernel_writes import record_thread_alias
from zerg.services.agents.kernel_writes import upsert_connection_for_run
from zerg.services.session_continue_targets import resolve_native_continue_target


def _make_db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'resolver.db'}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _session(db, *, provider="claude"):
    sid = uuid4()
    now = datetime.now(timezone.utc)
    s = AgentSession(
        id=sid,
        provider=provider,
        environment="development",
        project="repo",
        device_id="cinder",
        cwd="/Users/me/repo",
        started_at=now,
        ended_at=now,
        last_activity_at=now,
        thread_root_session_id=sid,
        continuation_kind="local",
        origin_label="cinder",
        user_messages=1,
        assistant_messages=1,
        tool_calls=0,
        is_writable_head=1,
        is_sidechain=0,
    )
    db.add(s)
    db.flush()
    return s


def _alias(db, thread, provider, kind, value):
    record_thread_alias(db, thread=thread, provider=provider, alias_kind=kind, alias_value=value)


def _source_line(db, session, thread, path):
    db.add(
        AgentSourceLine(
            session_id=session.id,
            thread_id=thread.id,
            source_path=path,
            source_offset=0,
            branch_id=0,
            raw_json='{"type":"message"}',
            line_hash=f"h-{session.id}",
        )
    )


def test_managed_claude_resumes_by_alias_when_present(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        s = _session(db)
        thread = ensure_primary_thread(db, s)
        _alias(db, thread, "claude", "provider_session_id", "provider-xyz")
        run = record_run(db, thread=thread, provider="claude", host_id="cinder", cwd="/Users/me/repo")
        upsert_connection_for_run(
            db, run=run, control_plane="claude_channel_bridge", acquisition_kind="spawned_control",
            state="released", external_name="cinder", can_send_input=0, can_interrupt=0,
            can_terminate=0, can_tail_output=0, can_resume=1,
        )
        db.commit()
        res = resolve_native_continue_target(db, s)
        assert res is not None
        assert res.adoption_mode == "managed_resume"
        assert res.provider_resume_id == "provider-xyz"


def test_managed_claude_falls_back_to_session_id_without_alias(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        s = _session(db)
        thread = ensure_primary_thread(db, s)
        run = record_run(db, thread=thread, provider="claude", host_id="cinder", cwd="/Users/me/repo")
        upsert_connection_for_run(
            db, run=run, control_plane="claude_channel_bridge", acquisition_kind="spawned_control",
            state="released", external_name="cinder", can_send_input=0, can_interrupt=0,
            can_terminate=0, can_tail_output=0, can_resume=1,
        )
        db.commit()
        res = resolve_native_continue_target(db, s)
        assert res is not None
        assert res.adoption_mode == "managed_resume"
        assert res.provider_resume_id == str(s.id)


def test_unmanaged_claude_adoptable_with_alias_and_transcript(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        s = _session(db)
        thread = ensure_primary_thread(db, s)
        _alias(db, thread, "claude", "provider_session_id", "raw-provider-id")
        _source_line(db, s, thread, "/x/raw.jsonl")
        db.commit()
        res = resolve_native_continue_target(db, s)
        assert res is not None
        assert res.adoption_mode == "adopt_unmanaged"
        assert res.provider_resume_id == "raw-provider-id"
        assert res.source_path == "/x/raw.jsonl"


def test_unmanaged_claude_not_resolvable_without_transcript(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        s = _session(db)
        thread = ensure_primary_thread(db, s)
        _alias(db, thread, "claude", "provider_session_id", "raw-provider-id")
        db.commit()
        assert resolve_native_continue_target(db, s) is None


def test_unmanaged_claude_not_resolvable_without_alias(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        s = _session(db)
        thread = ensure_primary_thread(db, s)
        _source_line(db, s, thread, "/x/raw.jsonl")
        db.commit()
        assert resolve_native_continue_target(db, s) is None


def test_codex_requires_distinct_provider_id_and_transcript(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        s = _session(db, provider="codex")
        thread = ensure_primary_thread(db, s)
        _alias(db, thread, "codex", "provider_session_id", "codex-thread-1")
        _source_line(db, s, thread, "/x/codex.jsonl")
        db.commit()
        res = resolve_native_continue_target(db, s)
        assert res is not None
        assert res.adoption_mode == "managed_resume"
        assert res.provider_resume_id == "codex-thread-1"
        assert res.source_path == "/x/codex.jsonl"


def test_codex_uses_primary_thread_source_path_not_session_wide_latest(tmp_path):
    """Codex resumes by transcript PATH, so it must use the PRIMARY thread's
    path, not a newer child/subagent thread's source line."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        s = _session(db, provider="codex")
        primary = ensure_primary_thread(db, s)
        _alias(db, primary, "codex", "provider_session_id", "codex-thread-1")
        # Primary-thread source line (the correct resume target).
        db.add(
            AgentSourceLine(
                session_id=s.id, thread_id=primary.id, source_path="/x/primary.jsonl",
                source_offset=0, branch_id=0, raw_json='{"type":"message"}', line_hash="h-primary",
            )
        )
        # A LATER source line under a DIFFERENT (child) thread — must NOT win.
        child_thread_id = uuid4()
        db.add(
            AgentSourceLine(
                session_id=s.id, thread_id=child_thread_id, source_path="/x/child-subagent.jsonl",
                source_offset=999, branch_id=0, raw_json='{"type":"message"}', line_hash="h-child",
            )
        )
        db.commit()
        res = resolve_native_continue_target(db, s)
        assert res is not None
        assert res.source_path == "/x/primary.jsonl"


def test_codex_rejected_when_provider_id_equals_session_id(tmp_path):
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        s = _session(db, provider="codex")
        thread = ensure_primary_thread(db, s)
        _alias(db, thread, "codex", "provider_session_id", str(s.id))
        _source_line(db, s, thread, "/x/codex.jsonl")
        db.commit()
        assert resolve_native_continue_target(db, s) is None
