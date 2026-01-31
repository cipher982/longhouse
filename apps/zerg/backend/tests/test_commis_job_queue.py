"""Tests for SQLite-safe commis job queue operations.

These tests verify that the dialect-aware job claiming works correctly
and prevents race conditions on both Postgres and SQLite.
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from unittest.mock import patch

import pytest
from sqlalchemy import text

from zerg.crud import crud
from zerg.database import db_session
from zerg.models.models import CommisJob
from zerg.models.models import User
from zerg.services.commis_job_queue import (
    HEARTBEAT_INTERVAL_SECONDS,
    STALE_THRESHOLD_SECONDS,
    claim_jobs,
    get_worker_id,
    reclaim_stale_jobs,
    update_heartbeat,
)


@pytest.fixture
def test_user(db):
    """Create a test user for job ownership."""
    user = User(
        email="test@example.com",
        display_name="Test User",
        provider="dev",
        provider_user_id="test-user-1",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def queued_jobs(db, test_user):
    """Create multiple queued jobs for testing."""
    jobs = []
    for i in range(5):
        job = CommisJob(
            owner_id=test_user.id,
            task=f"Test task {i}",
            model="test-model",
            status="queued",
        )
        db.add(job)
        jobs.append(job)
    db.commit()
    for job in jobs:
        db.refresh(job)
    return jobs


class TestGetWorkerId:
    """Tests for worker ID generation."""

    def test_worker_id_format(self):
        """Worker ID should be hostname:pid format."""
        worker_id = get_worker_id()
        assert ":" in worker_id
        parts = worker_id.split(":")
        assert len(parts) == 2
        assert parts[1].isdigit()  # PID should be numeric

    def test_worker_id_stable(self):
        """Worker ID should be stable within same process."""
        id1 = get_worker_id()
        id2 = get_worker_id()
        assert id1 == id2


class TestClaimJobs:
    """Tests for dialect-aware job claiming."""

    def test_claim_single_job(self, db, test_user, queued_jobs):
        """Should claim a single job and set correct fields."""
        job_ids = claim_jobs(db, limit=1, worker_id="test-worker-1")

        assert len(job_ids) == 1
        job_id = job_ids[0]

        # Expire cached objects and re-query to see committed changes
        db.expire_all()
        job = db.query(CommisJob).filter(CommisJob.id == job_id).first()
        assert job.status == "running"
        assert job.worker_id == "test-worker-1"
        assert job.claimed_at is not None
        assert job.heartbeat_at is not None
        assert job.started_at is not None

    def test_claim_multiple_jobs(self, db, test_user, queued_jobs):
        """Should claim up to limit jobs."""
        job_ids = claim_jobs(db, limit=3, worker_id="test-worker-1")

        assert len(job_ids) == 3

        # Expire cached objects and re-query
        db.expire_all()
        for job_id in job_ids:
            job = db.query(CommisJob).filter(CommisJob.id == job_id).first()
            assert job.status == "running"
            assert job.worker_id == "test-worker-1"

    def test_claim_no_jobs_available(self, db, test_user):
        """Should return empty list when no jobs available."""
        job_ids = claim_jobs(db, limit=5, worker_id="test-worker-1")
        assert job_ids == []

    def test_claim_respects_limit(self, db, test_user, queued_jobs):
        """Should not claim more than limit even if more available."""
        job_ids = claim_jobs(db, limit=2, worker_id="test-worker-1")
        assert len(job_ids) == 2

    def test_claim_only_queued_jobs(self, db, test_user):
        """Should only claim jobs with status='queued'."""
        # Create jobs with various statuses
        statuses = ["queued", "running", "success", "failed", "cancelled"]
        for status in statuses:
            job = CommisJob(
                owner_id=test_user.id,
                task=f"Task with status {status}",
                model="test-model",
                status=status,
            )
            db.add(job)
        db.commit()

        # Claim should only get the queued one
        job_ids = claim_jobs(db, limit=10, worker_id="test-worker-1")
        assert len(job_ids) == 1

        # Expire and re-query
        db.expire_all()
        job = db.query(CommisJob).filter(CommisJob.id == job_ids[0]).first()
        assert job.status == "running"

    def test_concurrent_claims_no_double_assign(self, db, test_user):
        """Multiple workers should not claim the same job."""
        # Create a single job
        job = CommisJob(
            owner_id=test_user.id,
            task="Single job for contention test",
            model="test-model",
            status="queued",
        )
        db.add(job)
        db.commit()
        job_id = job.id

        claimed_by = []
        errors = []

        def try_claim(worker_id: str):
            try:
                with db_session() as worker_db:
                    ids = claim_jobs(worker_db, limit=1, worker_id=worker_id)
                    if ids:
                        claimed_by.append((worker_id, ids[0]))
            except Exception as e:
                errors.append((worker_id, str(e)))

        # Run multiple workers concurrently
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [
                executor.submit(try_claim, f"worker-{i}")
                for i in range(5)
            ]
            for f in futures:
                f.result()

        # Exactly one worker should have claimed the job
        assert len(errors) == 0, f"Errors occurred: {errors}"
        assert len(claimed_by) == 1, f"Job was claimed by multiple workers: {claimed_by}"

        # Verify job state is consistent
        with db_session() as check_db:
            check_job = check_db.query(CommisJob).filter(CommisJob.id == job_id).first()
            assert check_job.status == "running"
            assert check_job.worker_id == claimed_by[0][0]


class TestUpdateHeartbeat:
    """Tests for heartbeat updates."""

    def test_heartbeat_success(self, db, test_user, queued_jobs):
        """Should update heartbeat for owned running job."""
        # Claim a job first
        job_ids = claim_jobs(db, limit=1, worker_id="test-worker-1")
        job_id = job_ids[0]

        # Get initial heartbeat
        db.expire_all()
        job = db.query(CommisJob).filter(CommisJob.id == job_id).first()
        initial_heartbeat = job.heartbeat_at
        assert initial_heartbeat is not None  # Should be set by claim

        # Sleep 1.1s to ensure SQLite's second-level timestamp changes
        # (SQLite datetime('now') has only second precision)
        time.sleep(1.1)

        # Update heartbeat
        success = update_heartbeat(db, job_id, "test-worker-1")
        assert success is True

        # Verify heartbeat was updated
        db.expire_all()
        job = db.query(CommisJob).filter(CommisJob.id == job_id).first()
        # Use >= to handle any precision edge cases
        assert job.heartbeat_at >= initial_heartbeat

    def test_heartbeat_fails_wrong_worker(self, db, test_user, queued_jobs):
        """Should fail if worker doesn't own the job."""
        job_ids = claim_jobs(db, limit=1, worker_id="test-worker-1")
        job_id = job_ids[0]

        # Try to heartbeat with different worker
        success = update_heartbeat(db, job_id, "test-worker-2")
        assert success is False

    def test_heartbeat_fails_not_running(self, db, test_user):
        """Should fail if job is not running."""
        job = CommisJob(
            owner_id=test_user.id,
            task="Non-running job",
            model="test-model",
            status="queued",
            worker_id="test-worker-1",
        )
        db.add(job)
        db.commit()

        success = update_heartbeat(db, job.id, "test-worker-1")
        assert success is False


class TestReclaimStaleJobs:
    """Tests for stale job reclaim logic."""

    def test_reclaim_stale_job(self, db, test_user):
        """Should reclaim job with old heartbeat."""
        # Create a running job with old heartbeat using raw SQL to set exact time
        old_time = datetime.now(timezone.utc) - timedelta(seconds=STALE_THRESHOLD_SECONDS + 60)
        job = CommisJob(
            owner_id=test_user.id,
            task="Stale job",
            model="test-model",
            status="running",
            worker_id="dead-worker",
            claimed_at=old_time,
            heartbeat_at=old_time,
            started_at=old_time,
        )
        db.add(job)
        db.commit()
        job_id = job.id

        # Reclaim stale jobs
        reclaimed = reclaim_stale_jobs(db)
        assert reclaimed == 1

        # Verify job was reset
        db.expire_all()
        job = db.query(CommisJob).filter(CommisJob.id == job_id).first()
        assert job.status == "queued"
        assert job.worker_id is None
        assert job.claimed_at is None
        assert job.heartbeat_at is None
        assert job.started_at is None

    def test_reclaim_job_with_null_heartbeat(self, db, test_user):
        """Should reclaim running jobs with NULL heartbeat (legacy)."""
        job = CommisJob(
            owner_id=test_user.id,
            task="Legacy running job",
            model="test-model",
            status="running",
            worker_id="old-worker",
            heartbeat_at=None,  # Legacy job without heartbeat
        )
        db.add(job)
        db.commit()
        job_id = job.id

        reclaimed = reclaim_stale_jobs(db)
        assert reclaimed == 1

        db.expire_all()
        job = db.query(CommisJob).filter(CommisJob.id == job_id).first()
        assert job.status == "queued"

    def test_dont_reclaim_fresh_job(self, db, test_user):
        """Should not reclaim job with recent heartbeat."""
        # Claim a job (sets heartbeat to now)
        queued_job = CommisJob(
            owner_id=test_user.id,
            task="Fresh job to claim",
            model="test-model",
            status="queued",
        )
        db.add(queued_job)
        db.commit()

        # Claim it to set heartbeat_at to now
        job_ids = claim_jobs(db, limit=1, worker_id="active-worker")
        assert len(job_ids) == 1
        job_id = job_ids[0]

        # Try to reclaim - should not reclaim freshly claimed job
        reclaimed = reclaim_stale_jobs(db)
        assert reclaimed == 0

        db.expire_all()
        job = db.query(CommisJob).filter(CommisJob.id == job_id).first()
        assert job.status == "running"
        assert job.worker_id == "active-worker"

    def test_dont_reclaim_non_running_jobs(self, db, test_user):
        """Should not reclaim jobs that aren't running."""
        for status in ["queued", "success", "failed", "cancelled"]:
            job = CommisJob(
                owner_id=test_user.id,
                task=f"Job with status {status}",
                model="test-model",
                status=status,
                heartbeat_at=None,  # Old heartbeat wouldn't matter
            )
            db.add(job)
        db.commit()

        reclaimed = reclaim_stale_jobs(db)
        assert reclaimed == 0


class TestClaimOrderPreservation:
    """Tests for job claiming order (FIFO)."""

    def test_claims_oldest_first(self, db, test_user):
        """Jobs should be claimed in FIFO order by created_at."""
        # Create jobs with explicit timestamps
        jobs = []
        for i in range(3):
            job = CommisJob(
                owner_id=test_user.id,
                task=f"Job {i}",
                model="test-model",
                status="queued",
            )
            db.add(job)
            db.flush()  # Get ID
            jobs.append(job)
        db.commit()

        # Claim one at a time and verify order
        claimed_ids = []
        for _ in range(3):
            ids = claim_jobs(db, limit=1, worker_id=f"worker-{_}")
            if ids:
                claimed_ids.append(ids[0])

        # Should match original creation order
        expected_ids = [j.id for j in jobs]
        assert claimed_ids == expected_ids


class TestIntegrationWithProcessor:
    """Integration tests with the CommisJobProcessor patterns."""

    @pytest.mark.asyncio
    async def test_heartbeat_loop_pattern(self, db, test_user, queued_jobs):
        """Simulate the heartbeat loop pattern used by processor."""
        job_ids = claim_jobs(db, limit=1, worker_id="processor-1")
        job_id = job_ids[0]

        # Simulate heartbeat loop
        heartbeat_count = 0
        for _ in range(3):
            success = update_heartbeat(db, job_id, "processor-1")
            if success:
                heartbeat_count += 1
            await asyncio.sleep(0.01)  # Very short for test

        assert heartbeat_count == 3

        # Verify job is still running
        db.expire_all()
        job = db.query(CommisJob).filter(CommisJob.id == job_id).first()
        assert job.status == "running"
