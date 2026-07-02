"""Hermetic tests for the Cursor role backfill migration helper.

Validates that legacy Cursor ``role="user"`` rows (context injection, and
real turns wrapped in <user_query>) are repaired in-place, raw_json is left
as ground truth, the scan is id-cursored/resumable, dry_run does not write,
and non-Cursor sessions are never touched. Also exercises the
``scripts/ops/backfill-cursor-roles.py`` operator entrypoint end-to-end
against a temp SQLite DB.
"""

from __future__ import annotations

import importlib.util
import os as _os
import sys
from datetime import datetime
from datetime import timezone
from pathlib import Path
from uuid import uuid4

from cryptography.fernet import Fernet

_os.environ.setdefault("DATABASE_URL", "sqlite://")
_os.environ.setdefault("TESTING", "1")
_os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())
_os.environ.setdefault("JWT_SECRET", "test-jwt-secret-long-enough")
_os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret-long-enough")
_os.environ.setdefault("AUTH_DISABLED", "1")

from sqlalchemy.orm import sessionmaker

from zerg.database import Base
from zerg.database import make_engine
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.services.cursor_role_backfill import backfill_cursor_user_roles

_TS = datetime(2026, 7, 1, 16, 0, 0, tzinfo=timezone.utc)

_INJECTION = (
    "<user_info>\nOS Version: darwin 25.5.0\n\n"
    "<rules>\n<always_applied_workspace_rule>x</...>\n"
    "<agent_transcripts>past</agent_transcripts>"
)


def _make_db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'bf.db'}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return engine, sessionmaker(bind=engine)


def _add_session(db, provider="cursor") -> AgentSession:
    sess = AgentSession(
        id=uuid4(),
        provider=provider,
        environment="test",
        started_at=_TS,
    )
    db.add(sess)
    db.flush()
    return sess


def _add_event(db, sess, role, content_text, raw_json=None) -> AgentEvent:
    ev = AgentEvent(
        session_id=sess.id,
        role=role,
        content_text=content_text,
        timestamp=_TS,
        raw_json=raw_json if raw_json is not None else content_text,
    )
    db.add(ev)
    db.flush()
    return ev


def test_backfill_re_roles_context_injection_to_system(tmp_path):
    engine, SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        sess = _add_session(db)
        ev = _add_event(db, sess, "user", _INJECTION, raw_json='{"role":"user"}')
        db.commit()

        result = backfill_cursor_user_roles(db)
        db.commit()
        assert result.scanned == 1
        assert result.re_roleed == 1
        assert result.unwrapped == 0

        db.refresh(ev)
        assert ev.role == "system"
        assert ev.content_text == _INJECTION  # full injection preserved
        assert ev.raw_json == '{"role":"user"}'  # ground truth untouched


def test_backfill_unwraps_user_query(tmp_path):
    engine, SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        sess = _add_session(db)
        ev = _add_event(db, sess, "user", "<user_query>\nhello test, banana\n</user_query>")
        db.commit()

        result = backfill_cursor_user_roles(db)
        db.commit()
        assert result.scanned == 1
        assert result.re_roleed == 0
        assert result.unwrapped == 1

        db.refresh(ev)
        assert ev.role == "user"
        assert ev.content_text == "hello test, banana"


def test_backfill_plain_user_turn_unchanged(tmp_path):
    engine, SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        sess = _add_session(db)
        ev = _add_event(db, sess, "user", "just a follow-up")
        db.commit()

        result = backfill_cursor_user_roles(db)
        db.commit()
        assert result.scanned == 1
        assert result.re_roleed == 0
        assert result.unwrapped == 0

        db.refresh(ev)
        assert ev.role == "user"
        assert ev.content_text == "just a follow-up"


def test_backfill_idempotent(tmp_path):
    engine, SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        sess = _add_session(db)
        inj = _add_event(db, sess, "user", _INJECTION)
        q = _add_event(db, sess, "user", "<user_query>do thing</user_query>")
        db.commit()

        first = backfill_cursor_user_roles(db)
        db.commit()
        assert first.re_roleed == 1 and first.unwrapped == 1

        # Second pass: re-roled row no longer matches role='user'; unwrapped
        # row still matches but classifies to itself -> no changes.
        second = backfill_cursor_user_roles(db)
        db.commit()
        assert second.re_roleed == 0
        assert second.unwrapped == 0

        db.refresh(inj)
        db.refresh(q)
        assert inj.role == "system"
        assert q.content_text == "do thing"


def test_backfill_dry_run_does_not_write(tmp_path):
    engine, SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        sess = _add_session(db)
        ev = _add_event(db, sess, "user", _INJECTION)
        db.commit()

        result = backfill_cursor_user_roles(db, dry_run=True)
        db.commit()
        assert result.re_roleed == 1  # would re-role
        assert result.unwrapped == 0

        db.refresh(ev)
        assert ev.role == "user"  # unchanged


def test_backfill_skips_non_cursor_sessions(tmp_path):
    engine, SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        claude_sess = _add_session(db, provider="claude")
        ev = _add_event(db, claude_sess, "user", _INJECTION)
        db.commit()

        result = backfill_cursor_user_roles(db)
        db.commit()
        assert result.scanned == 0

        db.refresh(ev)
        assert ev.role == "user"  # untouched despite injection markers


def test_backfill_resumable_by_id_cursor(tmp_path):
    engine, SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        sess = _add_session(db)
        ids = []
        for i in range(3):
            ev = _add_event(db, sess, "user", _INJECTION)
            ids.append(ev.id)
        db.commit()

        # batch_size=1 forces three separate calls, advancing after_id each time.
        after = 0
        scanned_total = 0
        re_roleed_total = 0
        for _ in range(10):
            r = backfill_cursor_user_roles(db, after_id=after, batch_size=1)
            if r.scanned == 0:
                break
            after = r.last_id
            scanned_total += r.scanned
            re_roleed_total += r.re_roleed
        db.commit()
        assert scanned_total == 3
        assert re_roleed_total == 3


_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "ops" / "backfill-cursor-roles.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("cursor_role_backfill_runner", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_backfill_script_main_repairs_rows_against_temp_db(tmp_path, monkeypatch):
    engine, SessionLocal = _make_db(tmp_path)
    db_path = tmp_path / "bf.db"
    with SessionLocal() as db:
        sess = _add_session(db)
        ev = _add_event(db, sess, "user", _INJECTION)
        ev_id = ev.id
        db.commit()
    engine.dispose()

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BACKFILL_BATCH_SIZE", "100")
    monkeypatch.delenv("BACKFILL_DRY_RUN", raising=False)

    module = _load_script_module()
    rc = module.main()
    assert rc == 0

    # Reopen and verify the row was re-roled.
    engine2, SessionLocal2 = _make_db(tmp_path)
    with SessionLocal2() as db:
        ev = db.get(AgentEvent, ev_id)
        assert ev.role == "system"
    engine2.dispose()


def test_backfill_script_dry_run_reports_without_writing(tmp_path, monkeypatch, capsys):
    engine, SessionLocal = _make_db(tmp_path)
    db_path = tmp_path / "bf.db"
    with SessionLocal() as db:
        sess = _add_session(db)
        ev = _add_event(db, sess, "user", _INJECTION)
        ev_id = ev.id
        db.commit()
    engine.dispose()

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BACKFILL_DRY_RUN", "1")

    module = _load_script_module()
    rc = module.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry run" in out
    assert "re_roleed=1" in out

    engine2, SessionLocal2 = _make_db(tmp_path)
    with SessionLocal2() as db:
        ev = db.get(AgentEvent, ev_id)
        assert ev.role == "user"  # unchanged
    engine2.dispose()
