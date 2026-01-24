"""Tests for manifest loader and job registry.

Tests cover:
- Manifest loader behavior (load_jobs_manifest)
- Registry duplicate handling
- Metadata tracking for manifest jobs
- sys.path cleanup
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from zerg.jobs.loader import _execute_manifest
from zerg.jobs.loader import _manifest_metadata
from zerg.jobs.loader import get_manifest_metadata
from zerg.jobs.loader import load_jobs_manifest
from zerg.jobs.loader import set_manifest_metadata
from zerg.jobs.registry import JobConfig
from zerg.jobs.registry import JobRegistry


class TestManifestMetadata:
    """Tests for manifest metadata tracking."""

    def test_get_metadata_returns_none_for_unknown_job(self):
        """Should return None for jobs not in metadata."""
        result = get_manifest_metadata("nonexistent-job-id")
        assert result is None

    def test_set_and_get_metadata(self):
        """Should store and retrieve metadata."""
        test_metadata = {
            "script_source": "manifest",
            "git_sha": "abc123",
            "loaded_at": "2026-01-24T00:00:00Z",
        }
        set_manifest_metadata("test-job", test_metadata)
        result = get_manifest_metadata("test-job")
        assert result == test_metadata

    def test_metadata_overwrite(self):
        """Should overwrite existing metadata."""
        set_manifest_metadata("overwrite-test", {"version": 1})
        set_manifest_metadata("overwrite-test", {"version": 2})
        result = get_manifest_metadata("overwrite-test")
        assert result["version"] == 2


class TestJobRegistryDuplicates:
    """Tests for registry duplicate handling."""

    def test_register_returns_true_for_new_job(self):
        """Should return True when registering a new job."""
        registry = JobRegistry()

        async def dummy_func():
            return {}

        config = JobConfig(id="new-job", cron="0 * * * *", func=dummy_func)
        result = registry.register(config)
        assert result is True
        assert registry.get("new-job") is not None

    def test_register_returns_false_for_duplicate(self):
        """Should return False and skip duplicate job IDs."""
        registry = JobRegistry()

        async def dummy_func():
            return {}

        config1 = JobConfig(id="dup-job", cron="0 * * * *", func=dummy_func)
        config2 = JobConfig(id="dup-job", cron="*/5 * * * *", func=dummy_func)

        result1 = registry.register(config1)
        result2 = registry.register(config2)

        assert result1 is True
        assert result2 is False
        # First registration wins
        assert registry.get("dup-job").cron == "0 * * * *"

    def test_register_logs_warning_for_duplicate(self, caplog):
        """Should log warning when skipping duplicate."""
        registry = JobRegistry()

        async def dummy_func():
            return {}

        config1 = JobConfig(id="warn-job", cron="0 * * * *", func=dummy_func)
        config2 = JobConfig(id="warn-job", cron="*/5 * * * *", func=dummy_func)

        registry.register(config1)
        with caplog.at_level("WARNING"):
            registry.register(config2)

        assert "warn-job already registered" in caplog.text


class TestLoadJobsManifest:
    """Tests for load_jobs_manifest function."""

    @pytest.mark.asyncio
    async def test_returns_false_when_no_git_service(self):
        """Should return False when git service not configured."""
        with patch("zerg.jobs.git_sync.get_git_sync_service", return_value=None):
            result = await load_jobs_manifest()
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_manifest_missing(self, tmp_path):
        """Should return False when manifest.py doesn't exist."""
        mock_service = MagicMock()
        mock_service.local_path = str(tmp_path)  # Empty dir, no manifest.py

        with patch("zerg.jobs.git_sync.get_git_sync_service", return_value=mock_service):
            result = await load_jobs_manifest()
        assert result is False

    @pytest.mark.asyncio
    async def test_acquires_read_lock(self, tmp_path):
        """Should acquire read lock before executing manifest."""
        # Create a manifest file
        manifest = tmp_path / "manifest.py"
        manifest.write_text("# empty manifest")

        mock_service = MagicMock()
        mock_service.local_path = str(tmp_path)

        # Create async context manager
        mock_cm = AsyncMock()
        mock_cm.__aenter__.return_value = "abc123"
        mock_cm.__aexit__.return_value = None
        mock_service.read_lock.return_value = mock_cm

        with patch("zerg.jobs.git_sync.get_git_sync_service", return_value=mock_service):
            with patch("zerg.jobs.loader._execute_manifest", return_value=True):
                await load_jobs_manifest()

        mock_service.read_lock.assert_called_once()


class TestExecuteManifest:
    """Tests for _execute_manifest function."""

    def test_cleans_up_sys_path(self, tmp_path):
        """Should remove repo_root from sys.path after execution."""
        manifest = tmp_path / "manifest.py"
        manifest.write_text("# empty manifest")

        repo_root_str = str(tmp_path)
        assert repo_root_str not in sys.path

        _execute_manifest(manifest, tmp_path, "abc123")

        # Path should be cleaned up
        assert repo_root_str not in sys.path

    def test_cleans_up_sys_path_on_error(self, tmp_path):
        """Should clean up sys.path even when manifest raises."""
        manifest = tmp_path / "manifest.py"
        manifest.write_text("raise ValueError('test error')")

        repo_root_str = str(tmp_path)

        result = _execute_manifest(manifest, tmp_path, "abc123")

        assert result is False
        assert repo_root_str not in sys.path

    def test_tracks_new_jobs(self, tmp_path):
        """Should track jobs registered during manifest execution."""
        from zerg.jobs.registry import job_registry

        # Clear any existing test jobs
        test_job_id = "manifest-test-job-12345"
        if test_job_id in job_registry._jobs:
            del job_registry._jobs[test_job_id]

        manifest = tmp_path / "manifest.py"
        manifest.write_text(f"""
from zerg.jobs.registry import job_registry, JobConfig

async def test_func():
    return {{}}

job_registry.register(JobConfig(
    id="{test_job_id}",
    cron="0 * * * *",
    func=test_func,
))
""")

        result = _execute_manifest(manifest, tmp_path, "sha123")

        assert result is True

        # Check metadata was set
        metadata = get_manifest_metadata(test_job_id)
        assert metadata is not None
        assert metadata["git_sha"] == "sha123"
        assert metadata["script_source"] == "manifest"
        assert "loaded_at" in metadata

        # Cleanup
        if test_job_id in job_registry._jobs:
            del job_registry._jobs[test_job_id]
        if test_job_id in _manifest_metadata:
            del _manifest_metadata[test_job_id]

    def test_returns_false_on_manifest_error(self, tmp_path):
        """Should return False when manifest raises exception."""
        manifest = tmp_path / "manifest.py"
        manifest.write_text("raise RuntimeError('Intentional test error')")

        result = _execute_manifest(manifest, tmp_path, "abc123")
        assert result is False


class TestWorkerMetadataIntegration:
    """Tests for metadata propagation to worker."""

    def test_manifest_job_metadata_structure(self):
        """Verify manifest metadata has expected structure for worker."""
        set_manifest_metadata(
            "integration-test-job",
            {
                "script_source": "manifest",
                "git_sha": "abc123def456",
                "loaded_at": "2026-01-24T12:00:00Z",
                "manifest_path": "/path/to/manifest.py",
            },
        )

        metadata = get_manifest_metadata("integration-test-job")

        # Worker expects these fields
        assert "script_source" in metadata
        assert "git_sha" in metadata
        assert "loaded_at" in metadata
        assert metadata["script_source"] == "manifest"

        # Cleanup
        if "integration-test-job" in _manifest_metadata:
            del _manifest_metadata["integration-test-job"]
