from __future__ import annotations

import asyncio
import json
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from zerg.database import get_db
from zerg.database import make_engine
from zerg.dependencies.browser_route_auth import get_current_browser_route_user
from zerg.database import Base
from zerg.models.agents import AgentSession
from zerg.routers import session_chat
from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest
from zerg.services.agents_store import SourceLineIngest
from zerg.services.session_continuity import ResolvedWorkspace
from zerg.services.session_continuity import ShipSessionResult
from zerg.services.session_continuity import WorkspaceResolver
from zerg.services.session_continuity import encode_cwd_for_claude
from zerg.services.session_continuity import prepare_claude_session_for_resume
from zerg.services.session_continuity import ship_session_to_zerg


def _make_db(tmp_path):
    db_path = tmp_path / "resume_prep.db"
    engine = make_engine(f"sqlite:///{db_path}")
    engine = engine.execution_options(schema_translate_map={"agents": None})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _seed_session(
    db,
    *,
    session_id=None,
    provider_session_id="resume-root",
    cwd="/Users/example/git/zerg",
    git_repo="git@github.com:cipher982/longhouse.git",
    git_branch="main",
):
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
            cwd=cwd,
            git_repo=git_repo,
            git_branch=git_branch,
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


def test_prepare_claude_session_for_resume_uses_local_db_export(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    claude_config = tmp_path / ".claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_config))

    async def fail_get(*_args, **_kwargs):
        raise AssertionError("prepare_claude_session_for_resume should not self-fetch over HTTP")

    monkeypatch.setattr("zerg.services.session_continuity.httpx.AsyncClient.get", fail_get)

    with SessionLocal() as db:
        session_id = _seed_session(db)
        provider_session_id = asyncio.run(
            prepare_claude_session_for_resume(
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


def test_workspace_resolver_creates_managed_scratch_workspace_for_missing_non_repo_session(tmp_path):
    resolver = WorkspaceResolver(
        temp_base=tmp_path / "temp-clones",
        scratch_base=tmp_path / "managed-continuations",
    )

    resolved = asyncio.run(
        resolver.resolve(
            original_cwd="/Users/example/git/nonexistent/session-workspace",
            git_repo=None,
            git_branch=None,
            session_id="session-123",
        )
    )

    assert resolved.error is None
    assert resolved.is_temp is False
    assert resolved.path == tmp_path / "managed-continuations" / "session-session-123"
    assert resolved.path.exists()
    assert resolved.path.is_dir()
def test_ship_session_to_zerg_uses_local_ingest_when_db_provided(tmp_path, monkeypatch):
    SessionLocal = _make_db(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    claude_config = tmp_path / ".claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_config))

    async def fail_post(*_args, **_kwargs):
        raise AssertionError("ship_session_to_zerg should not self-post when db is provided")

    monkeypatch.setattr("zerg.services.session_continuity.httpx.AsyncClient.post", fail_post)

    with SessionLocal() as db:
        source_session_id = _seed_session(db)
        provider_session_id = asyncio.run(
            prepare_claude_session_for_resume(
                session_id=str(source_session_id),
                workspace_path=workspace,
                db=db,
            )
        )

        store = AgentsStore(db)
        target = store.create_continuation_session(
            source_session_id,
            continuation_kind="cloud",
            origin_label="Cloud",
            environment="Cloud",
            device_id="zerg-commis-cloud",
            provider_session_id=provider_session_id,
            branched_from_event_id=store.get_latest_event_id(source_session_id),
        )
        db.commit()

        # Session-identity-kernel cleanup: continuation lineage columns were
        # removed; thread_root/continued_from now derive (root = self.id,
        # continued_from = None). Coerce defensively so we don't pass the
        # literal string "None" to a UUID validator.
        continued_from = target.continued_from_session_id
        shipped = asyncio.run(
            ship_session_to_zerg(
                workspace_path=workspace,
                claude_config_dir=claude_config,
                commis_id="local-ship",
                db=db,
                session_id=str(target.id),
                thread_root_session_id=str(target.thread_root_session_id or target.id),
                continued_from_session_id=str(continued_from) if continued_from else None,
                continuation_kind="cloud",
                origin_label="Cloud",
                branched_from_event_id=target.branched_from_event_id,
            )
        )

        assert shipped is not None
        assert shipped.session_id == str(target.id)
        assert shipped.events_inserted >= 1

        db.expire_all()
        persisted_target = db.query(AgentSession).filter(AgentSession.id == target.id).one()
        assert persisted_target.user_messages >= 1


def test_find_latest_codex_session_file(tmp_path, monkeypatch):
    from zerg.services.session_continuity import _find_latest_codex_session_file

    sessions_dir = tmp_path / ".codex" / "sessions" / "2026" / "03" / "26"
    sessions_dir.mkdir(parents=True)

    old_file = sessions_dir / "rollout-2026-03-26T10-00-00-old-session-id.jsonl"
    old_file.write_text("{}")

    import time

    time.sleep(0.01)

    new_file = sessions_dir / "rollout-2026-03-26T10-30-00-new-session-id.jsonl"
    new_file.write_text("{}")

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))

    result = _find_latest_codex_session_file()

    assert result is not None
    assert result.name == new_file.name
