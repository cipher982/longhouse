"""Tests for CommisJobProcessor session continuity integration.

These tests verify that the workspace job processing correctly integrates
with session continuity (prepare_session_for_resume and ship_session_to_life_hub).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import timezone
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from zerg.services.commis_job_processor import CommisJobProcessor


class TestWorkspaceJobSessionContinuity:
    """Tests for session continuity in workspace job processing."""

    @pytest.fixture
    def processor(self):
        """Create a CommisJobProcessor instance."""
        return CommisJobProcessor()

    @pytest.fixture
    def mock_workspace(self, tmp_path):
        """Create a mock workspace path."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        return workspace

    @pytest.fixture
    def mock_workspace_manager_class(self, mock_workspace):
        """Create mock WorkspaceManager class."""
        mock_ws = MagicMock()
        mock_ws.path = mock_workspace

        mock_class = MagicMock()
        mock_class.return_value.setup = AsyncMock(return_value=mock_ws)
        mock_class.return_value.capture_diff = AsyncMock(return_value="diff content")
        return mock_class

    @pytest.fixture
    def mock_cloud_executor_class(self):
        """Create mock CloudExecutor class."""
        result = MagicMock()
        result.status = "success"
        result.output = "Task completed"
        result.error = None
        result.duration_ms = 1000

        mock_class = MagicMock()
        mock_class.return_value.run_agent = AsyncMock(return_value=result)
        return mock_class

    @pytest.mark.asyncio
    async def test_workspace_job_prepares_session_when_resume_id_provided(
        self, processor, mock_workspace_manager_class, mock_cloud_executor_class, db_session
    ):
        """Test that prepare_session_for_resume is called when resume_session_id is in job config."""
        from zerg.crud import crud

        # Create a job with resume_session_id
        job = crud.CommisJob(
            task="Test task",
            model="claude-sonnet",
            status="running",
            config={
                "execution_mode": "workspace",
                "git_repo": "https://github.com/test/repo.git",
                "resume_session_id": "life-hub-session-123",
            },
            owner_id=1,
        )
        db_session.add(job)
        db_session.commit()
        job_id = job.id

        mock_prepare = AsyncMock(return_value="provider-session-abc")
        mock_ship = AsyncMock(return_value="shipped-session-456")

        with (
            patch(
                "zerg.services.workspace_manager.WorkspaceManager",
                mock_workspace_manager_class,
            ),
            patch(
                "zerg.services.cloud_executor.CloudExecutor",
                mock_cloud_executor_class,
            ),
            patch(
                "zerg.services.session_continuity.prepare_session_for_resume",
                mock_prepare,
            ),
            patch(
                "zerg.services.session_continuity.ship_session_to_life_hub",
                mock_ship,
            ),
            patch("zerg.services.commis_artifact_store.CommisArtifactStore"),
        ):
            await processor._process_workspace_job(job_id, concierge_course_id=None)

        # Verify prepare_session_for_resume was called with correct args
        mock_prepare.assert_called_once()
        call_args = mock_prepare.call_args
        assert call_args.kwargs["session_id"] == "life-hub-session-123"

        # Verify CloudExecutor.run_agent was called with the prepared session ID
        executor_instance = mock_cloud_executor_class.return_value
        executor_instance.run_agent.assert_called_once()
        run_args = executor_instance.run_agent.call_args
        assert run_args.kwargs["resume_session_id"] == "provider-session-abc"

    @pytest.mark.asyncio
    async def test_workspace_job_no_prepare_without_resume_id(
        self, processor, mock_workspace_manager_class, mock_cloud_executor_class, db_session
    ):
        """Test that prepare_session_for_resume is NOT called when no resume_session_id."""
        from zerg.crud import crud

        # Create a job WITHOUT resume_session_id
        job = crud.CommisJob(
            task="Test task",
            model="claude-sonnet",
            status="running",
            config={
                "execution_mode": "workspace",
                "git_repo": "https://github.com/test/repo.git",
                # No resume_session_id
            },
            owner_id=1,
        )
        db_session.add(job)
        db_session.commit()
        job_id = job.id

        mock_prepare = AsyncMock(return_value="provider-session-abc")
        mock_ship = AsyncMock(return_value="shipped-session-456")

        with (
            patch(
                "zerg.services.workspace_manager.WorkspaceManager",
                mock_workspace_manager_class,
            ),
            patch(
                "zerg.services.cloud_executor.CloudExecutor",
                mock_cloud_executor_class,
            ),
            patch(
                "zerg.services.session_continuity.prepare_session_for_resume",
                mock_prepare,
            ),
            patch(
                "zerg.services.session_continuity.ship_session_to_life_hub",
                mock_ship,
            ),
            patch("zerg.services.commis_artifact_store.CommisArtifactStore"),
        ):
            await processor._process_workspace_job(job_id, concierge_course_id=None)

        # Verify prepare_session_for_resume was NOT called
        mock_prepare.assert_not_called()

        # Verify CloudExecutor.run_agent was called without resume_session_id
        executor_instance = mock_cloud_executor_class.return_value
        executor_instance.run_agent.assert_called_once()
        run_args = executor_instance.run_agent.call_args
        assert run_args.kwargs.get("resume_session_id") is None

    @pytest.mark.asyncio
    async def test_workspace_job_ships_session_on_success(
        self, processor, mock_workspace_manager_class, mock_cloud_executor_class, db_session
    ):
        """Test that ship_session_to_life_hub is called after successful execution."""
        from zerg.crud import crud

        job = crud.CommisJob(
            task="Test task",
            model="claude-sonnet",
            status="running",
            config={
                "execution_mode": "workspace",
                "git_repo": "https://github.com/test/repo.git",
            },
            owner_id=1,
        )
        db_session.add(job)
        db_session.commit()
        job_id = job.id

        mock_ship = AsyncMock(return_value="shipped-session-456")

        with (
            patch(
                "zerg.services.workspace_manager.WorkspaceManager",
                mock_workspace_manager_class,
            ),
            patch(
                "zerg.services.cloud_executor.CloudExecutor",
                mock_cloud_executor_class,
            ),
            patch(
                "zerg.services.session_continuity.ship_session_to_life_hub",
                mock_ship,
            ),
            patch("zerg.services.commis_artifact_store.CommisArtifactStore"),
        ):
            await processor._process_workspace_job(job_id, concierge_course_id=None)

        # Verify ship_session_to_life_hub was called
        mock_ship.assert_called_once()
        call_args = mock_ship.call_args
        # Commis ID should be generated and passed
        assert "commis_id" in call_args.kwargs

    @pytest.mark.asyncio
    async def test_workspace_job_no_ship_on_failure(self, processor, mock_workspace_manager_class, db_session):
        """Test that ship_session_to_life_hub is NOT called when execution fails."""
        from zerg.crud import crud

        job = crud.CommisJob(
            task="Test task",
            model="claude-sonnet",
            status="running",
            config={
                "execution_mode": "workspace",
                "git_repo": "https://github.com/test/repo.git",
            },
            owner_id=1,
        )
        db_session.add(job)
        db_session.commit()
        job_id = job.id

        # Create a failing cloud executor
        failed_result = MagicMock()
        failed_result.status = "failed"
        failed_result.output = ""
        failed_result.error = "Execution failed"
        failed_result.duration_ms = 1000

        mock_executor_class = MagicMock()
        mock_executor_class.return_value.run_agent = AsyncMock(return_value=failed_result)

        mock_ship = AsyncMock(return_value="shipped-session-456")

        with (
            patch(
                "zerg.services.workspace_manager.WorkspaceManager",
                mock_workspace_manager_class,
            ),
            patch(
                "zerg.services.cloud_executor.CloudExecutor",
                mock_executor_class,
            ),
            patch(
                "zerg.services.session_continuity.ship_session_to_life_hub",
                mock_ship,
            ),
            patch("zerg.services.commis_artifact_store.CommisArtifactStore"),
        ):
            await processor._process_workspace_job(job_id, concierge_course_id=None)

        # Verify ship_session_to_life_hub was NOT called (only ships on success)
        mock_ship.assert_not_called()

    @pytest.mark.asyncio
    async def test_workspace_job_handles_prepare_failure_gracefully(
        self, processor, mock_workspace_manager_class, mock_cloud_executor_class, db_session
    ):
        """Test that job continues even if prepare_session_for_resume fails."""
        from zerg.crud import crud

        job = crud.CommisJob(
            task="Test task",
            model="claude-sonnet",
            status="running",
            config={
                "execution_mode": "workspace",
                "git_repo": "https://github.com/test/repo.git",
                "resume_session_id": "life-hub-session-123",
            },
            owner_id=1,
        )
        db_session.add(job)
        db_session.commit()
        job_id = job.id

        # Make prepare fail
        mock_prepare = AsyncMock(side_effect=ValueError("Session not found"))
        mock_ship = AsyncMock(return_value="shipped-session-456")

        with (
            patch(
                "zerg.services.workspace_manager.WorkspaceManager",
                mock_workspace_manager_class,
            ),
            patch(
                "zerg.services.cloud_executor.CloudExecutor",
                mock_cloud_executor_class,
            ),
            patch(
                "zerg.services.session_continuity.prepare_session_for_resume",
                mock_prepare,
            ),
            patch(
                "zerg.services.session_continuity.ship_session_to_life_hub",
                mock_ship,
            ),
            patch("zerg.services.commis_artifact_store.CommisArtifactStore"),
        ):
            # Should not raise - graceful degradation
            await processor._process_workspace_job(job_id, concierge_course_id=None)

        # Verify the job still ran (without resume)
        executor_instance = mock_cloud_executor_class.return_value
        executor_instance.run_agent.assert_called_once()
        run_args = executor_instance.run_agent.call_args
        assert run_args.kwargs.get("resume_session_id") is None

    @pytest.mark.asyncio
    async def test_workspace_job_handles_ship_failure_gracefully(
        self, processor, mock_workspace_manager_class, mock_cloud_executor_class, db_session
    ):
        """Test that job completes even if ship_session_to_life_hub fails."""
        from zerg.crud import crud

        job = crud.CommisJob(
            task="Test task",
            model="claude-sonnet",
            status="running",
            config={
                "execution_mode": "workspace",
                "git_repo": "https://github.com/test/repo.git",
            },
            owner_id=1,
        )
        db_session.add(job)
        db_session.commit()
        job_id = job.id

        # Make ship fail
        mock_ship = AsyncMock(side_effect=Exception("Network error"))

        with (
            patch(
                "zerg.services.workspace_manager.WorkspaceManager",
                mock_workspace_manager_class,
            ),
            patch(
                "zerg.services.cloud_executor.CloudExecutor",
                mock_cloud_executor_class,
            ),
            patch(
                "zerg.services.session_continuity.ship_session_to_life_hub",
                mock_ship,
            ),
            patch("zerg.services.commis_artifact_store.CommisArtifactStore"),
        ):
            # Should not raise - shipping is best-effort
            await processor._process_workspace_job(job_id, concierge_course_id=None)

        # Verify job was marked as success (execution succeeded, ship failure doesn't affect status)
        db_session.refresh(job)
        assert job.status == "success"
