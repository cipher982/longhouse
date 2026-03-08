from __future__ import annotations

import asyncio
import json
from datetime import datetime
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from zerg.database import get_db
from zerg.database import make_engine
from zerg.models.agents import AgentsBase
from zerg.models.agents import AgentSession
from zerg.routers import session_chat
from zerg.routers.oikos_auth import get_current_oikos_user
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest
from zerg.services.agents_store import SourceLineIngest
from zerg.services.session_continuity import ResolvedWorkspace
from zerg.services.session_continuity import encode_cwd_for_claude
from zerg.services.session_continuity import prepare_session_for_resume


def _make_db(tmp_path):
    db_path = tmp_path / "resume_prep.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    AgentsBase.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _seed_session(db, *, session_id=None, provider_session_id="resume-root"):
    session_id = session_id or uuid4()
    started_at = datetime(2026, 3, 8, 21, 30, tzinfo=timezone.utc)
    raw_line = json.dumps(
        {
            "type": "user",
            "message": {"content": [{"type": "text", "text": "hello from cinder"}]},
            "timestamp": started_at.isoformat(),
        }
    )
    result = AgentsStore(db).ingest_session(
        SessionIngest(
            id=session_id,
            provider="claude",
            environment="Cinder",
            project="zerg",
            device_id="shipper-cinder",
            cwd="/Users/davidrose/git/zerg",
            git_repo="git@github.com:cipher982/longhouse.git",
            git_branch="main",
            started_at=started_at,
            provider_session_id=provider_session_id,
            events=[
                EventIngest(
                    role="user",
                    content_text="hello from cinder",
                    timestamp=started_at,
                    source_path="/tmp/session.jsonl",
                    source_offset=0,
                    raw_json=raw_line,
                )
            ],
            source_lines=[
                SourceLineIngest(
                    source_path="/tmp/session.jsonl",
                    source_offset=0,
                    raw_json=raw_line,
                )
            ],
        )
    )
    db.commit()
    return result.session_id


def test_prepare_session_for_resume_uses_local_db_export(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    claude_config = tmp_path / ".claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_config))

    async def fail_get(*_args, **_kwargs):
        raise AssertionError("prepare_session_for_resume should not self-fetch over HTTP")

    monkeypatch.setattr("zerg.services.session_continuity.httpx.AsyncClient.get", fail_get)

    with SessionLocal() as db:
        session_id = _seed_session(db)
        provider_session_id = asyncio.run(
            prepare_session_for_resume(
                session_id=str(session_id),
                workspace_path=workspace,
                db=db,
            )
        )

    assert provider_session_id == "resume-root"
    encoded_cwd = encode_cwd_for_claude(str(workspace.absolute()))
    session_file = claude_config / "projects" / encoded_cwd / "resume-root.jsonl"
    assert session_file.exists()
    assert "hello from cinder" in session_file.read_text()


def test_chat_with_session_prepares_resume_without_http_self_fetch(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    claude_config = tmp_path / ".claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_config))

    async def fail_get(*_args, **_kwargs):
        raise AssertionError("session chat should not self-fetch exported sessions over HTTP")

    async def fake_resolve(*_args, **_kwargs):
        return ResolvedWorkspace(path=workspace, is_temp=False)

    async def fake_stream_claude_output(**kwargs):
        yield session_chat.SSEEvent(
            event="system",
            data=json.dumps(
                {
                    "type": "session_started",
                    "source_session_id": kwargs["source_session_id"],
                    "session_id": kwargs["target_session_id"],
                    "created_continuation": kwargs["created_continuation"],
                }
            ),
        ).encode()
        yield session_chat.SSEEvent(
            event="done",
            data=json.dumps(
                {
                    "session_id": kwargs["target_session_id"],
                    "source_session_id": kwargs["source_session_id"],
                    "shipped_session_id": kwargs["target_session_id"],
                    "created_continuation": kwargs["created_continuation"],
                    "exit_code": 0,
                    "total_text_length": 0,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ),
        ).encode()

    monkeypatch.setattr("zerg.services.session_continuity.httpx.AsyncClient.get", fail_get)
    monkeypatch.setattr(session_chat.workspace_resolver, "resolve", fake_resolve)
    monkeypatch.setattr(session_chat, "stream_claude_output", fake_stream_claude_output)

    from zerg.main import api_app
    from zerg.main import app

    with SessionLocal() as db:
        source_session_id = _seed_session(db)

        def override_get_db():
            try:
                yield db
            finally:
                pass

        def override_current_user():
            return SimpleNamespace(id=1, email="owner@local")

        api_app.dependency_overrides[get_db] = override_get_db
        api_app.dependency_overrides[get_current_oikos_user] = override_current_user

        try:
            client = TestClient(app, backend="asyncio")
            response = client.post(
                f"/api/sessions/{source_session_id}/chat",
                json={"message": "anything else?"},
            )
            assert response.status_code == 200
            body = response.text
            assert '"created_continuation": true' in body
            assert 'event: done' in body

            sessions = db.query(AgentSession).filter(AgentSession.thread_root_session_id == source_session_id).all()
            assert len(sessions) == 2
            source = next(s for s in sessions if s.id == source_session_id)
            target = next(s for s in sessions if s.id != source_session_id)
            assert source.is_writable_head == 0
            assert target.is_writable_head == 1
            assert target.continued_from_session_id == source_session_id
            assert target.continuation_kind == "cloud"
        finally:
            api_app.dependency_overrides.clear()
