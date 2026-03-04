"""Guardrails for workspace-only commis job creation."""

from types import SimpleNamespace

import pytest
from sqlalchemy.orm import sessionmaker

from zerg.database import Base
from zerg.database import make_engine
from zerg.models.models import CommisJob
from zerg.models.user import User
from zerg.tools.builtin import oikos_tools


def _make_db(tmp_path):
    db_path = tmp_path / "spawn_commis_workspace.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _create_user(db, email: str = "commis-test@example.com") -> User:
    user = User(email=email)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.mark.asyncio
async def test_spawn_commis_alias_always_creates_workspace_job(tmp_path, monkeypatch):
    """Legacy alias still creates workspace-mode jobs (scratch workspace path)."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user = _create_user(db)
        resolver = SimpleNamespace(db=db, owner_id=user.id)
        monkeypatch.setattr(oikos_tools, "get_credential_resolver", lambda: resolver)
        monkeypatch.setattr(oikos_tools, "get_oikos_context", lambda: None)

        result = await oikos_tools.spawn_commis_async(
            task="Investigate flaky tests",
            backend="gemini",
            _return_structured=True,
        )

        assert isinstance(result, dict)
        assert result["status"] == "queued"
        job = db.query(CommisJob).filter(CommisJob.id == result["job_id"]).one()
        assert job.config is not None
        assert job.config.get("execution_mode") == "workspace"
        assert job.config.get("backend") == "gemini"
        assert "git_repo" not in job.config


@pytest.mark.asyncio
async def test_spawn_workspace_commis_keeps_workspace_mode_and_repo_config(tmp_path, monkeypatch):
    """Primary spawn tool stores git repo and resume metadata in workspace config."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user = _create_user(db, email="commis-workspace@example.com")
        resolver = SimpleNamespace(db=db, owner_id=user.id)
        monkeypatch.setattr(oikos_tools, "get_credential_resolver", lambda: resolver)
        monkeypatch.setattr(oikos_tools, "get_oikos_context", lambda: None)

        result = await oikos_tools.spawn_workspace_commis_async(
            task="Update docs",
            git_repo="https://github.com/org/repo.git",
            backend="codex",
            resume_session_id="session-123",
            _return_structured=True,
        )

        assert isinstance(result, dict)
        assert result["status"] == "queued"
        job = db.query(CommisJob).filter(CommisJob.id == result["job_id"]).one()
        assert job.config is not None
        assert job.config.get("execution_mode") == "workspace"
        assert job.config.get("git_repo") == "https://github.com/org/repo.git"
        assert job.config.get("resume_session_id") == "session-123"
        assert job.config.get("backend") == "codex"
