"""Tests for session discovery tools."""

from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy.orm import sessionmaker
from tests.helpers.agents_seed import seed_agent_session
from zerg.database import initialize_database
from zerg.database import make_engine
from zerg.tools.builtin import BUILTIN_TOOLS
from zerg.tools.builtin import session_tools
from zerg.tools.registry import ImmutableToolRegistry
from zerg.tools.unified_access import create_tool_resolver


def _seed_session(engine, **kwargs) -> str:
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        return seed_agent_session(db, **kwargs)


def _patch_db_session(monkeypatch, engine):
    SessionLocal = sessionmaker(bind=engine)

    @contextmanager
    def _db_session():
        with SessionLocal() as db:
            yield db
            db.commit()

    monkeypatch.setattr(session_tools, "db_session", _db_session)


def test_search_sessions_returns_matches(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    initialize_database(engine)
    session_id = _seed_session(engine)
    _patch_db_session(monkeypatch, engine)

    result = session_tools.search_sessions("alpha", limit=5)
    assert result["ok"] is True
    data = result["data"]
    assert data["sessions"]
    assert data["sessions"][0]["id"] == session_id
    assert "alpha" in (data["sessions"][0].get("match_snippet") or "").lower()


def test_filter_sessions_by_project(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'filter.db'}")
    initialize_database(engine)
    _seed_session(engine)
    _patch_db_session(monkeypatch, engine)

    result = session_tools.filter_sessions(project="session-tools", limit=5)
    assert result["ok"] is True
    data = result["data"]
    assert data["sessions"]
    assert data["sessions"][0]["project"] == "session-tools"


def test_grep_sessions_returns_tool_output_match(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'grep.db'}")
    initialize_database(engine)
    _seed_session(engine)
    _patch_db_session(monkeypatch, engine)

    result = session_tools.grep_sessions("needle", limit=5)
    assert result["ok"] is True
    data = result["data"]
    assert data["matches"]
    assert data["matches"][0]["field"] in {"tool_output_text", "content_text"}


def test_get_session_detail_validation(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'detail.db'}")
    initialize_database(engine)
    _patch_db_session(monkeypatch, engine)

    bad_result = session_tools.get_session_detail("not-a-uuid")
    assert bad_result["ok"] is False
    assert bad_result["error_type"] == "validation_error"


def test_get_session_detail_returns_events(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'detail_valid.db'}")
    initialize_database(engine)
    session_id = _seed_session(engine)
    _patch_db_session(monkeypatch, engine)

    result = session_tools.get_session_detail(session_id, limit=10)
    assert result["ok"] is True
    data = result["data"]
    assert data["session"]["id"] == session_id
    assert len(data["events"]) >= 1


def test_tool_resolver_invokes_search_sessions(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'resolver.db'}")
    initialize_database(engine)
    _seed_session(engine)
    _patch_db_session(monkeypatch, engine)

    registry = ImmutableToolRegistry.build([BUILTIN_TOOLS])
    resolver = create_tool_resolver(registry)

    tool = resolver.get_tool("search_sessions")
    assert tool is not None

    result = tool.invoke({"query": "alpha", "limit": 5})
    assert result["ok"] is True
    assert result["data"]["sessions"]
