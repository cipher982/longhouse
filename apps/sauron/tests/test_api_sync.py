"""Tests for Sauron /sync API endpoint.

Tests the FastAPI endpoint integration with job sync logic.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from sauron.api import app


@pytest.fixture
def client():
    """Create FastAPI test client."""
    return TestClient(app)


class TestSyncEndpoint:
    """Tests for POST /sync endpoint."""

    def test_sync_no_git_service(self, client):
        """Returns error when git sync not configured."""
        with patch("zerg.jobs.get_git_sync_service", return_value=None):
            response = client.post("/sync")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "not configured" in data["message"]

    def test_sync_success(self, client):
        """Successful sync returns job counts."""
        mock_git_service = MagicMock()
        mock_git_service.refresh = AsyncMock(return_value={"success": True, "message": "Updated"})
        mock_git_service.current_sha = "abc123"

        mock_registry = MagicMock()
        mock_registry.snapshot_jobs.return_value = {"builtin-job": "0 * * * *"}
        mock_registry.sync_jobs.return_value = {"added": 2, "removed": 1, "rescheduled": 1}

        with (
            patch("zerg.jobs.get_git_sync_service", return_value=mock_git_service),
            patch("zerg.jobs.job_registry", mock_registry),
            patch("zerg.jobs.loader.load_jobs_manifest", new_callable=AsyncMock, return_value=True) as mock_load,
            patch("zerg.jobs.loader.get_manifest_metadata", return_value=None),  # builtin job
            patch("sauron.job_definitions.publish_job_definitions"),
        ):
            response = client.post("/sync")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["sha"] == "abc123"
        assert data["jobs_added"] == 2
        assert data["jobs_removed"] == 1
        assert data["jobs_rescheduled"] == 1
        # Verify clear_existing was passed
        mock_load.assert_called_once()
        call_kwargs = mock_load.call_args[1]
        assert call_kwargs["clear_existing"] is True

    def test_sync_git_refresh_failure(self, client):
        """Git refresh returning success=False is handled gracefully."""
        mock_git_service = MagicMock()
        # Simulate git refresh failure (returns dict with success=False, not exception)
        mock_git_service.refresh = AsyncMock(return_value={
            "success": False,
            "error": "Failed to fetch: network timeout",
            "consecutive_failures": 3,
        })
        mock_git_service.current_sha = "old123"

        with patch("zerg.jobs.get_git_sync_service", return_value=mock_git_service):
            response = client.post("/sync")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "Git refresh failed" in data["message"]
        assert "network timeout" in data["message"]
        # Verify scheduler was NOT synced (no manifest reload)

    def test_sync_git_exception(self, client):
        """Git sync exceptions are handled gracefully."""
        mock_git_service = MagicMock()
        mock_git_service.refresh = AsyncMock(side_effect=Exception("Network error"))

        with patch("zerg.jobs.get_git_sync_service", return_value=mock_git_service):
            response = client.post("/sync")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "Network error" in data["message"]

    def test_sync_manifest_load_failure(self, client):
        """Manifest load failure stops sync and returns error."""
        mock_git_service = MagicMock()
        mock_git_service.refresh = AsyncMock(return_value={"success": True, "changed": True})
        mock_git_service.current_sha = "abc123"

        mock_registry = MagicMock()
        mock_registry.snapshot_jobs.return_value = {}

        with (
            patch("zerg.jobs.get_git_sync_service", return_value=mock_git_service),
            patch("zerg.jobs.job_registry", mock_registry),
            patch("zerg.jobs.loader.load_jobs_manifest", new_callable=AsyncMock, return_value=False),
            patch("zerg.jobs.loader.get_manifest_metadata", return_value=None),
        ):
            response = client.post("/sync")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "Manifest load failed" in data["message"]
        # Verify sync_jobs was NOT called
        mock_registry.sync_jobs.assert_not_called()

    def test_sync_preserves_builtin_jobs(self, client):
        """Builtin jobs are preserved during sync."""
        mock_git_service = MagicMock()
        mock_git_service.refresh = AsyncMock(return_value={"success": True, "message": "Updated"})
        mock_git_service.current_sha = "abc123"

        mock_registry = MagicMock()
        # Two jobs: one builtin (no metadata), one manifest (has metadata)
        mock_registry.snapshot_jobs.return_value = {
            "builtin-job": "0 * * * *",
            "manifest-job": "30 * * * *",
        }
        mock_registry.sync_jobs.return_value = {"added": 0, "removed": 0, "rescheduled": 0}

        def mock_metadata(job_id):
            if job_id == "manifest-job":
                return {"script_source": "git", "git_sha": "old123"}
            return None  # builtin has no metadata

        with (
            patch("zerg.jobs.get_git_sync_service", return_value=mock_git_service),
            patch("zerg.jobs.job_registry", mock_registry),
            patch("zerg.jobs.loader.load_jobs_manifest", new_callable=AsyncMock, return_value=True) as mock_load,
            patch("zerg.jobs.loader.get_manifest_metadata", side_effect=mock_metadata),
            patch("sauron.job_definitions.publish_job_definitions"),
        ):
            response = client.post("/sync")

        assert response.status_code == 200
        # Verify builtin job was in the preserved set
        call_kwargs = mock_load.call_args[1]
        assert "builtin-job" in call_kwargs["builtin_job_ids"]
        assert "manifest-job" not in call_kwargs["builtin_job_ids"]


class TestSyncResponseModel:
    """Tests for SyncResponse model."""

    def test_response_includes_job_counts(self, client):
        """Response model includes all job count fields."""
        mock_git_service = MagicMock()
        mock_git_service.refresh = AsyncMock(return_value={"success": True, "message": "OK"})
        mock_git_service.current_sha = "def456"

        mock_registry = MagicMock()
        mock_registry.snapshot_jobs.return_value = {}
        mock_registry.sync_jobs.return_value = {"added": 5, "removed": 3, "rescheduled": 2}

        with (
            patch("zerg.jobs.get_git_sync_service", return_value=mock_git_service),
            patch("zerg.jobs.job_registry", mock_registry),
            patch("zerg.jobs.loader.load_jobs_manifest", new_callable=AsyncMock, return_value=True),
            patch("zerg.jobs.loader.get_manifest_metadata", return_value=None),
            patch("sauron.job_definitions.publish_job_definitions"),
        ):
            response = client.post("/sync")

        data = response.json()
        assert "jobs_added" in data
        assert "jobs_removed" in data
        assert "jobs_rescheduled" in data
        assert data["jobs_added"] == 5
        assert data["jobs_removed"] == 3
        assert data["jobs_rescheduled"] == 2
