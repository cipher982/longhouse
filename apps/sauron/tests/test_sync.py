"""Tests for Sauron /sync job reschedule functionality.

Tests the core sync logic:
- Job diffing (added, removed, rescheduled)
- APScheduler integration
- Manifest reload with clear_existing
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from zerg.jobs.registry import JobConfig, JobRegistry


@pytest.fixture
def mock_scheduler():
    """Create a mock APScheduler."""
    scheduler = MagicMock()
    scheduler.add_job = MagicMock()
    scheduler.remove_job = MagicMock()
    scheduler.get_jobs = MagicMock(return_value=[])
    return scheduler


@pytest.fixture
def registry(mock_scheduler):
    """Create a JobRegistry with mock scheduler."""
    reg = JobRegistry()
    reg._scheduler = mock_scheduler
    reg._use_queue = True
    return reg


async def dummy_job():
    """Dummy job function for testing."""
    return {"status": "ok"}


class TestJobRegistrySnapshot:
    """Tests for snapshot_jobs functionality."""

    def test_snapshot_empty_registry(self, registry):
        """Snapshot of empty registry returns empty dict."""
        snapshot = registry.snapshot_jobs()
        assert snapshot == {}

    def test_snapshot_with_jobs(self, registry):
        """Snapshot captures job IDs and cron expressions."""
        registry.register(JobConfig(id="job-a", cron="0 * * * *", func=dummy_job))
        registry.register(JobConfig(id="job-b", cron="30 8 * * *", func=dummy_job))

        snapshot = registry.snapshot_jobs()

        assert snapshot == {
            "job-a": "0 * * * *",
            "job-b": "30 8 * * *",
        }


class TestJobRegistryClearManifest:
    """Tests for clear_manifest_jobs functionality."""

    def test_clear_preserves_builtin_jobs(self, registry):
        """Builtin jobs are preserved, manifest jobs are removed."""
        registry.register(JobConfig(id="builtin-1", cron="0 * * * *", func=dummy_job))
        registry.register(JobConfig(id="manifest-1", cron="0 * * * *", func=dummy_job))
        registry.register(JobConfig(id="manifest-2", cron="0 * * * *", func=dummy_job))

        removed = registry.clear_manifest_jobs(builtin_job_ids={"builtin-1"})

        assert removed == {"manifest-1", "manifest-2"}
        assert "builtin-1" in registry._jobs
        assert "manifest-1" not in registry._jobs
        assert "manifest-2" not in registry._jobs

    def test_clear_with_empty_builtins(self, registry):
        """All jobs removed when no builtins specified."""
        registry.register(JobConfig(id="job-a", cron="0 * * * *", func=dummy_job))
        registry.register(JobConfig(id="job-b", cron="0 * * * *", func=dummy_job))

        removed = registry.clear_manifest_jobs(builtin_job_ids=set())

        assert removed == {"job-a", "job-b"}
        assert len(registry._jobs) == 0


class TestJobRegistrySyncJobs:
    """Tests for sync_jobs functionality."""

    def test_sync_detects_added_jobs(self, registry, mock_scheduler):
        """New jobs are detected and scheduled."""
        # Start with one job
        old_snapshot = {"existing-job": "0 * * * *"}

        # Registry now has two jobs
        registry.register(JobConfig(id="existing-job", cron="0 * * * *", func=dummy_job))
        registry.register(JobConfig(id="new-job", cron="30 * * * *", func=dummy_job))

        result = registry.sync_jobs(old_snapshot)

        assert result["added"] == 1
        assert result["removed"] == 0
        assert result["rescheduled"] == 0
        # add_job called for new job
        assert mock_scheduler.add_job.call_count >= 1

    def test_sync_detects_removed_jobs(self, registry, mock_scheduler):
        """Removed jobs are detected and unscheduled."""
        # Start with two jobs
        old_snapshot = {"job-a": "0 * * * *", "job-b": "30 * * * *"}

        # Registry now has only one job
        registry.register(JobConfig(id="job-a", cron="0 * * * *", func=dummy_job))

        result = registry.sync_jobs(old_snapshot)

        assert result["added"] == 0
        assert result["removed"] == 1
        assert result["rescheduled"] == 0
        # remove_job called for removed job
        mock_scheduler.remove_job.assert_called_once_with("job_job-b")

    def test_sync_detects_rescheduled_jobs(self, registry, mock_scheduler):
        """Jobs with changed cron are detected and rescheduled."""
        # Start with old cron
        old_snapshot = {"job-a": "0 * * * *"}

        # Registry has new cron
        registry.register(JobConfig(id="job-a", cron="30 * * * *", func=dummy_job))

        result = registry.sync_jobs(old_snapshot)

        assert result["added"] == 0
        assert result["removed"] == 0
        assert result["rescheduled"] == 1
        # add_job called with replace_existing=True for reschedule
        assert mock_scheduler.add_job.call_count >= 1

    def test_sync_handles_mixed_changes(self, registry, mock_scheduler):
        """Mixed adds, removes, and reschedules are handled correctly."""
        # Start with jobs A (will be removed), B (will be rescheduled), C (unchanged)
        old_snapshot = {
            "job-a": "0 * * * *",
            "job-b": "0 8 * * *",
            "job-c": "0 12 * * *",
        }

        # Registry now has: B (new cron), C (same), D (new)
        registry.register(JobConfig(id="job-b", cron="30 8 * * *", func=dummy_job))  # changed
        registry.register(JobConfig(id="job-c", cron="0 12 * * *", func=dummy_job))  # same
        registry.register(JobConfig(id="job-d", cron="0 18 * * *", func=dummy_job))  # new

        result = registry.sync_jobs(old_snapshot)

        assert result["added"] == 1  # job-d
        assert result["removed"] == 1  # job-a
        assert result["rescheduled"] == 1  # job-b

    def test_sync_no_scheduler(self, registry):
        """Sync returns zeros when no scheduler available."""
        registry._scheduler = None
        old_snapshot = {"job-a": "0 * * * *"}

        result = registry.sync_jobs(old_snapshot)

        assert result == {"added": 0, "removed": 0, "rescheduled": 0}

    def test_sync_unchanged_jobs_not_touched(self, registry, mock_scheduler):
        """Unchanged jobs are not rescheduled."""
        old_snapshot = {"job-a": "0 * * * *", "job-b": "30 * * * *"}

        registry.register(JobConfig(id="job-a", cron="0 * * * *", func=dummy_job))
        registry.register(JobConfig(id="job-b", cron="30 * * * *", func=dummy_job))

        result = registry.sync_jobs(old_snapshot)

        assert result["added"] == 0
        assert result["removed"] == 0
        assert result["rescheduled"] == 0


class TestUnscheduleJob:
    """Tests for _unschedule_job functionality."""

    def test_unschedule_existing_job(self, registry, mock_scheduler):
        """Unscheduling existing job calls remove_job."""
        result = registry._unschedule_job("test-job")

        assert result is True
        mock_scheduler.remove_job.assert_called_once_with("job_test-job")

    def test_unschedule_nonexistent_job(self, registry, mock_scheduler):
        """Unscheduling nonexistent job returns False gracefully."""
        mock_scheduler.remove_job.side_effect = Exception("Job not found")

        result = registry._unschedule_job("nonexistent")

        assert result is False

    def test_unschedule_no_scheduler(self, registry):
        """Unschedule returns False when no scheduler."""
        registry._scheduler = None

        result = registry._unschedule_job("test-job")

        assert result is False


class TestScheduleJob:
    """Tests for _schedule_job functionality."""

    def test_schedule_enabled_job(self, registry, mock_scheduler):
        """Enabled job is scheduled successfully."""
        config = JobConfig(id="test-job", cron="0 * * * *", func=dummy_job, enabled=True)

        result = registry._schedule_job(config)

        assert result is True
        assert mock_scheduler.add_job.called

    def test_schedule_disabled_job(self, registry, mock_scheduler):
        """Disabled job is not scheduled."""
        config = JobConfig(id="test-job", cron="0 * * * *", func=dummy_job, enabled=False)

        result = registry._schedule_job(config)

        assert result is False
        assert not mock_scheduler.add_job.called

    def test_schedule_no_scheduler(self, registry):
        """Schedule returns False when no scheduler."""
        registry._scheduler = None
        config = JobConfig(id="test-job", cron="0 * * * *", func=dummy_job)

        result = registry._schedule_job(config)

        assert result is False


class TestUnregister:
    """Tests for unregister functionality."""

    def test_unregister_existing_job(self, registry):
        """Unregistering existing job removes it."""
        registry.register(JobConfig(id="job-a", cron="0 * * * *", func=dummy_job))

        result = registry.unregister("job-a")

        assert result is True
        assert "job-a" not in registry._jobs

    def test_unregister_nonexistent_job(self, registry):
        """Unregistering nonexistent job returns False."""
        result = registry.unregister("nonexistent")

        assert result is False
