"""Tests for CommisJobProcessor session continuity integration.

These tests verify that the workspace job processing correctly integrates
with session continuity (prepare_session_for_resume) and direct DB ingestion (_ingest_workspace_session).
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
        mock_class.return_value.run_commis = AsyncMock(return_value=result)
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
                "resume_session_id": "longhouse-session-123",
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
                "zerg.services.session_continuity.ship_session_to_zerg",
                mock_ship,
            ),
            patch("zerg.services.commis_artifact_store.CommisArtifactStore"),
        ):
            await processor._process_workspace_job(job_id, oikos_run_id=None)

        # Verify prepare_session_for_resume was called with correct args
        mock_prepare.assert_called_once()
        call_args = mock_prepare.call_args
        assert call_args.kwargs["session_id"] == "longhouse-session-123"

        # Verify CloudExecutor.run_commis was called with the prepared session ID
        executor_instance = mock_cloud_executor_class.return_value
        executor_instance.run_commis.assert_called_once()
        run_args = executor_instance.run_commis.call_args
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
                "zerg.services.session_continuity.ship_session_to_zerg",
                mock_ship,
            ),
            patch("zerg.services.commis_artifact_store.CommisArtifactStore"),
        ):
            await processor._process_workspace_job(job_id, oikos_run_id=None)

        # Verify prepare_session_for_resume was NOT called
        mock_prepare.assert_not_called()

        # Verify CloudExecutor.run_commis was called without resume_session_id
        executor_instance = mock_cloud_executor_class.return_value
        executor_instance.run_commis.assert_called_once()
        run_args = executor_instance.run_commis.call_args
        assert run_args.kwargs.get("resume_session_id") is None

    @pytest.mark.asyncio
    async def test_workspace_job_ingests_session_on_success(
        self, processor, mock_workspace_manager_class, mock_cloud_executor_class, db_session
    ):
        """Test that _ingest_workspace_session is called after successful execution."""
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

        mock_ingest = MagicMock()

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
                "zerg.services.commis_job_processor._ingest_workspace_session",
                mock_ingest,
            ),
            patch("zerg.services.commis_artifact_store.CommisArtifactStore"),
        ):
            await processor._process_workspace_job(job_id, oikos_run_id=None)

        # Verify _ingest_workspace_session was called
        mock_ingest.assert_called_once()
        call_kwargs = mock_ingest.call_args.kwargs
        # Commis ID and job ID should be passed
        assert call_kwargs["job_id"] == job_id
        assert call_kwargs["commis_id"].startswith("ws-")

    @pytest.mark.asyncio
    async def test_workspace_job_no_ingest_on_failure(self, processor, mock_workspace_manager_class, db_session):
        """Test that _ingest_workspace_session is NOT called when execution fails."""
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
        mock_executor_class.return_value.run_commis = AsyncMock(return_value=failed_result)

        mock_ingest = MagicMock()

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
                "zerg.services.commis_job_processor._ingest_workspace_session",
                mock_ingest,
            ),
            patch("zerg.services.commis_artifact_store.CommisArtifactStore"),
        ):
            await processor._process_workspace_job(job_id, oikos_run_id=None)

        # Verify _ingest_workspace_session was NOT called (only ingests on success)
        mock_ingest.assert_not_called()

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
                "resume_session_id": "longhouse-session-123",
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
                "zerg.services.session_continuity.ship_session_to_zerg",
                mock_ship,
            ),
            patch("zerg.services.commis_artifact_store.CommisArtifactStore"),
        ):
            # Should not raise - graceful degradation
            await processor._process_workspace_job(job_id, oikos_run_id=None)

        # Verify the job still ran (without resume)
        executor_instance = mock_cloud_executor_class.return_value
        executor_instance.run_commis.assert_called_once()
        run_args = executor_instance.run_commis.call_args
        assert run_args.kwargs.get("resume_session_id") is None

    @pytest.mark.asyncio
    async def test_workspace_job_handles_ingest_failure_gracefully(
        self, processor, mock_workspace_manager_class, mock_cloud_executor_class, db_session
    ):
        """Test that job completes even if _ingest_workspace_session fails."""
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

        # Make ingest fail
        mock_ingest = MagicMock(side_effect=Exception("Ingest error"))

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
                "zerg.services.commis_job_processor._ingest_workspace_session",
                mock_ingest,
            ),
            patch("zerg.services.commis_artifact_store.CommisArtifactStore"),
        ):
            # Should not raise - ingestion is best-effort
            await processor._process_workspace_job(job_id, oikos_run_id=None)

        # Verify job was marked as success (execution succeeded, ingest failure doesn't affect status)
        db_session.refresh(job)
        assert job.status == "success"


class TestCommisStartedEventEmission:
    """Tests for commis_started event emission."""

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
        mock_class.return_value.run_commis = AsyncMock(return_value=result)
        return mock_class

    @pytest.mark.asyncio
    async def test_commis_started_event_emitted_when_job_starts(
        self, processor, mock_workspace_manager_class, mock_cloud_executor_class, db_session
    ):
        """Test that commis_started event is emitted when job processing begins.

        The commis_started event should be emitted after workspace setup but before
        hatch execution begins. It provides the UI with immediate feedback that
        the commis has transitioned from 'spawned' to 'running'.
        """
        from zerg.crud import crud
        from zerg.models.enums import RunStatus, RunTrigger
        from zerg.models.models import Fiche
        from zerg.models.run import Run
        from zerg.models.thread import Thread
        from tests.conftest import TEST_MODEL

        # Create test user
        owner = crud.get_user_by_email(db_session, "dev@local") or crud.create_user(
            db_session, email="dev@local", provider=None, role="ADMIN"
        )

        # Create fiche for run
        fiche = Fiche(
            owner_id=owner.id,
            name="Test Fiche",
            system_instructions="Test",
            task_instructions="Test",
            model=TEST_MODEL,
            status="idle",
        )
        db_session.add(fiche)
        db_session.commit()
        db_session.refresh(fiche)

        # Create thread for run
        thread = Thread(
            fiche_id=fiche.id,
            title="Test Thread",
            active=True,
        )
        db_session.add(thread)
        db_session.commit()
        db_session.refresh(thread)

        # Create oikos run for event correlation
        oikos_run = Run(
            fiche_id=fiche.id,
            thread_id=thread.id,
            status=RunStatus.RUNNING,
            trigger=RunTrigger.MANUAL,
        )
        db_session.add(oikos_run)
        db_session.commit()
        db_session.refresh(oikos_run)

        # Create commis job with oikos_run_id
        job = crud.CommisJob(
            task="Analyze code and suggest improvements",
            model="claude-sonnet",
            status="running",
            config={
                "execution_mode": "workspace",
                "git_repo": "https://github.com/test/repo.git",
            },
            owner_id=owner.id,
            oikos_run_id=oikos_run.id,
        )
        db_session.add(job)
        db_session.commit()
        job_id = job.id

        mock_append_run_event = AsyncMock(return_value=1)

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
                "zerg.services.event_store.append_run_event",
                mock_append_run_event,
            ),
            patch("zerg.services.commis_artifact_store.CommisArtifactStore"),
            patch("zerg.services.event_store.emit_run_event", AsyncMock(return_value=1)),
        ):
            await processor._process_workspace_job(job_id, oikos_run_id=oikos_run.id)

        # Find the commis_started call among all append_run_event calls
        commis_started_calls = [
            call for call in mock_append_run_event.call_args_list
            if call.kwargs.get("event_type") == "commis_started"
        ]

        assert len(commis_started_calls) == 1, "commis_started event should be emitted exactly once"

        # Verify the event payload contains correct data
        started_call = commis_started_calls[0]
        assert started_call.kwargs["run_id"] == oikos_run.id
        payload = started_call.kwargs["payload"]
        assert payload["job_id"] == job_id
        assert "commis_id" in payload
        assert payload["commis_id"].startswith("ws-")
        assert payload["task"] == "Analyze code and suggest improvements"

    @pytest.mark.asyncio
    async def test_commis_started_not_emitted_without_oikos_run_id(
        self, processor, mock_workspace_manager_class, mock_cloud_executor_class, db_session
    ):
        """Test that commis_started is not emitted when there's no oikos_run_id."""
        from zerg.crud import crud

        # Create job WITHOUT oikos_run_id
        job = crud.CommisJob(
            task="Test task",
            model="claude-sonnet",
            status="running",
            config={
                "execution_mode": "workspace",
                "git_repo": "https://github.com/test/repo.git",
            },
            owner_id=1,
            oikos_run_id=None,
        )
        db_session.add(job)
        db_session.commit()
        job_id = job.id

        mock_append_run_event = AsyncMock(return_value=1)

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
                "zerg.services.event_store.append_run_event",
                mock_append_run_event,
            ),
            patch("zerg.services.commis_artifact_store.CommisArtifactStore"),
        ):
            await processor._process_workspace_job(job_id, oikos_run_id=None)

        # Should not have emitted commis_started
        commis_started_calls = [
            call for call in mock_append_run_event.call_args_list
            if call.kwargs.get("event_type") == "commis_started"
        ]
        assert len(commis_started_calls) == 0, "commis_started should not be emitted without oikos_run_id"

    @pytest.mark.asyncio
    async def test_commis_started_failure_does_not_fail_job(
        self, processor, mock_workspace_manager_class, mock_cloud_executor_class, db_session
    ):
        """Test that failure to emit commis_started does not fail the job."""
        from zerg.crud import crud
        from zerg.models.enums import RunStatus, RunTrigger
        from zerg.models.models import Fiche
        from zerg.models.run import Run
        from zerg.models.thread import Thread
        from tests.conftest import TEST_MODEL

        # Create test user
        owner = crud.get_user_by_email(db_session, "dev@local") or crud.create_user(
            db_session, email="dev@local", provider=None, role="ADMIN"
        )

        # Create fiche for run
        fiche = Fiche(
            owner_id=owner.id,
            name="Test Fiche",
            system_instructions="Test",
            task_instructions="Test",
            model=TEST_MODEL,
            status="idle",
        )
        db_session.add(fiche)
        db_session.commit()
        db_session.refresh(fiche)

        # Create thread for run
        thread = Thread(
            fiche_id=fiche.id,
            title="Test Thread",
            active=True,
        )
        db_session.add(thread)
        db_session.commit()
        db_session.refresh(thread)

        # Create oikos run
        oikos_run = Run(
            fiche_id=fiche.id,
            thread_id=thread.id,
            status=RunStatus.RUNNING,
            trigger=RunTrigger.MANUAL,
        )
        db_session.add(oikos_run)
        db_session.commit()
        db_session.refresh(oikos_run)

        # Create commis job
        job = crud.CommisJob(
            task="Test task",
            model="claude-sonnet",
            status="running",
            config={
                "execution_mode": "workspace",
                "git_repo": "https://github.com/test/repo.git",
            },
            owner_id=owner.id,
            oikos_run_id=oikos_run.id,
        )
        db_session.add(job)
        db_session.commit()
        job_id = job.id

        # Make commis_started event emission fail
        mock_append_run_event = AsyncMock(side_effect=Exception("Event emission failed"))

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
                "zerg.services.event_store.append_run_event",
                mock_append_run_event,
            ),
            patch("zerg.services.commis_artifact_store.CommisArtifactStore"),
            patch("zerg.services.event_store.emit_run_event", AsyncMock(return_value=1)),
        ):
            # Should not raise - event emission failure is caught
            await processor._process_workspace_job(job_id, oikos_run_id=oikos_run.id)

        # Verify job still completed successfully despite event emission failure
        db_session.refresh(job)
        assert job.status == "success", "Job should succeed even if commis_started emission fails"
