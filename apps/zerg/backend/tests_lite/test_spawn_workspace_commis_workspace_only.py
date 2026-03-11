"""Guardrails for workspace-only commis job creation."""

from types import SimpleNamespace

import pytest
from sqlalchemy.orm import sessionmaker

from zerg.database import Base
from zerg.database import make_engine
from zerg.models.models import CommisJob
from zerg.models.user import User
from zerg.tools.builtin import oikos_commis_job_tools
from zerg.tools.builtin import oikos_tools


def _make_db(tmp_path):
    db_path = tmp_path / "spawn_workspace_commis_workspace.db"
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
async def test_spawn_workspace_commis_creates_workspace_job_in_scratch_mode(tmp_path, monkeypatch):
    """Primary spawn tool creates workspace-mode jobs in scratch mode without git_repo."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user = _create_user(db)
        resolver = SimpleNamespace(db=db, owner_id=user.id)
        monkeypatch.setattr(oikos_commis_job_tools, "get_credential_resolver", lambda: resolver)
        monkeypatch.setattr(oikos_commis_job_tools, "get_oikos_context", lambda: None)

        result = await oikos_tools.spawn_workspace_commis_async(
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
        monkeypatch.setattr(oikos_commis_job_tools, "get_credential_resolver", lambda: resolver)
        monkeypatch.setattr(oikos_commis_job_tools, "get_oikos_context", lambda: None)

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


@pytest.mark.asyncio
async def test_spawn_workspace_commis_rejects_operator_resume_when_continue_disabled(tmp_path, monkeypatch):
    """Operator-triggered resume is blocked unless allow_continue is enabled."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user = User(
            email="operator-denied@example.com",
            context={"preferences": {"operator_mode": {"enabled": True, "allow_continue": False}}},
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        resolver = SimpleNamespace(db=db, owner_id=user.id)
        monkeypatch.setattr(oikos_commis_job_tools, "get_credential_resolver", lambda: resolver)
        monkeypatch.setattr(
            oikos_commis_job_tools,
            "get_oikos_context",
            lambda: SimpleNamespace(
                run_id=None,
                trace_id=None,
                reasoning_effort="none",
                source_surface_id="operator",
            ),
        )
        monkeypatch.setenv("OIKOS_OPERATOR_MODE_ENABLED", "1")

        result = await oikos_tools.spawn_workspace_commis_async(
            task="Run the pending targeted tests",
            resume_session_id="session-123",
            _return_structured=True,
        )

        assert isinstance(result, dict)
        assert result["ok"] is False
        assert result["error_type"] == "permission_denied"
        assert db.query(CommisJob).count() == 0


@pytest.mark.asyncio
async def test_spawn_workspace_commis_allows_operator_resume_when_continue_enabled(tmp_path, monkeypatch):
    """Operator-triggered resume works when allow_continue is enabled."""
    SessionLocal = _make_db(tmp_path)
    with SessionLocal() as db:
        user = User(
            email="operator-allowed@example.com",
            context={"preferences": {"operator_mode": {"enabled": True, "allow_continue": True}}},
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        resolver = SimpleNamespace(db=db, owner_id=user.id)
        monkeypatch.setattr(oikos_commis_job_tools, "get_credential_resolver", lambda: resolver)
        monkeypatch.setattr(
            oikos_commis_job_tools,
            "get_oikos_context",
            lambda: SimpleNamespace(
                run_id=None,
                trace_id=None,
                reasoning_effort="none",
                source_surface_id="operator",
            ),
        )
        monkeypatch.setenv("OIKOS_OPERATOR_MODE_ENABLED", "1")

        result = await oikos_tools.spawn_workspace_commis_async(
            task="Run the pending targeted tests",
            resume_session_id="session-abc",
            _return_structured=True,
        )

        assert isinstance(result, dict)
        assert result["status"] == "queued"
        job = db.query(CommisJob).filter(CommisJob.id == result["job_id"]).one()
        assert job.config is not None
        assert job.config.get("execution_mode") == "workspace"
        assert job.config.get("resume_session_id") == "session-abc"
