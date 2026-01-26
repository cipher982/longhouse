"""Tests for spawn_commis idempotency during concierge resume replay.

These tests verify the idempotency fix that prevents duplicate commis when:
1. The concierge loop replays tool calls after interrupt/resume
2. The LLM slightly rephrases the task on replay (e.g., "check disk" → "check disk space")

The fix uses:
- Exact match for in-progress commis (allows different commis in same run)
- Prefix match (first 50 chars) for completed commis (handles replay rephrasing)
"""

import tempfile

import pytest

from tests.conftest import TEST_COMMIS_MODEL
from zerg.connectors.context import set_credential_resolver
from zerg.connectors.resolver import CredentialResolver
from zerg.models.enums import CourseStatus
from zerg.models.models import Course
from zerg.models.models import CommisJob
from zerg.services.concierge_context import reset_concierge_context
from zerg.services.concierge_context import set_concierge_context
from zerg.tools.builtin.concierge_tools import spawn_commis_async


@pytest.fixture
def temp_artifact_path(monkeypatch):
    """Create temporary artifact store path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setenv("SWARMLET_DATA_PATH", tmpdir)
        yield tmpdir


@pytest.fixture
def credential_context(db_session, test_user):
    """Set up credential resolver context for tools."""
    resolver = CredentialResolver(fiche_id=1, db=db_session, owner_id=test_user.id)
    token = set_credential_resolver(resolver)
    yield resolver
    set_credential_resolver(None)


@pytest.fixture
def concierge_run(db_session, test_user, sample_fiche, sample_thread):
    """Create a concierge run for testing."""
    run = Course(
        fiche_id=sample_fiche.id,
        thread_id=sample_thread.id,
        status=CourseStatus.RUNNING,
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    return run


class TestSpawnCommisIdempotency:
    """Tests for spawn_commis idempotency during concierge replay."""

    @pytest.mark.asyncio
    async def test_exact_task_reuse_while_in_progress(
        self, db_session, test_user, credential_context, temp_artifact_path, concierge_run
    ):
        """Verify spawn_commis with exact same task during in-progress reuses job."""
        token = set_concierge_context(
            course_id=concierge_run.id,
            owner_id=test_user.id,
            message_id="test-msg-1",
        )

        try:
            # First call creates queued CommisJob
            result1 = await spawn_commis_async("Check disk space on cube", model=TEST_COMMIS_MODEL)
            assert "queued successfully" in result1

            # Second call with EXACT same task should reuse the existing job
            result2 = await spawn_commis_async("Check disk space on cube", model=TEST_COMMIS_MODEL)
            assert "queued successfully" in result2

            # Assert: still only ONE job for this run
            jobs = (
                db_session.query(CommisJob)
                .filter(CommisJob.concierge_course_id == concierge_run.id)
                .all()
            )
            assert len(jobs) == 1, f"Expected 1 job, got {len(jobs)} - duplicate spawned on replay"
        finally:
            reset_concierge_context(token)

    @pytest.mark.asyncio
    async def test_different_tasks_create_separate_commis(
        self, db_session, test_user, credential_context, temp_artifact_path, concierge_run
    ):
        """Verify spawn_commis with genuinely different tasks creates separate commis."""
        token = set_concierge_context(
            course_id=concierge_run.id,
            owner_id=test_user.id,
            message_id="test-msg-2",
        )

        try:
            # First task
            result1 = await spawn_commis_async("Check disk space on cube", model=TEST_COMMIS_MODEL)
            assert "queued successfully" in result1

            # Different task should create a NEW job (not a duplicate - this is legitimate)
            result2 = await spawn_commis_async("Check memory usage on cube", model=TEST_COMMIS_MODEL)
            assert "queued successfully" in result2

            # Assert: TWO jobs since tasks are different
            jobs = (
                db_session.query(CommisJob)
                .filter(CommisJob.concierge_course_id == concierge_run.id)
                .all()
            )
            assert len(jobs) == 2, f"Expected 2 jobs for different tasks, got {len(jobs)}"
        finally:
            reset_concierge_context(token)

    @pytest.mark.asyncio
    async def test_completed_job_returns_cached_result(
        self, db_session, test_user, credential_context, temp_artifact_path, concierge_run
    ):
        """Verify spawn_commis with matching completed job returns cached result."""
        import os

        from zerg.services.commis_artifact_store import CommisArtifactStore

        token = set_concierge_context(
            course_id=concierge_run.id,
            owner_id=test_user.id,
            message_id="test-msg-3",
        )

        try:
            # Create a completed job with artifacts
            commis_id = "test-commis-completed-001"
            job = CommisJob(
                owner_id=test_user.id,
                concierge_course_id=concierge_run.id,
                task="Check disk space on cube",
                model=TEST_COMMIS_MODEL,
                status="success",
                commis_id=commis_id,
            )
            db_session.add(job)
            db_session.commit()
            db_session.refresh(job)

            # Create artifact files (simulating completed commis)
            artifact_store = CommisArtifactStore()
            commis_dir = artifact_store._get_commis_dir(commis_id)
            os.makedirs(commis_dir, exist_ok=True)

            # Write result and metadata
            with open(commis_dir / "result.txt", "w") as f:
                f.write("Disk usage is 45%")

            import json

            with open(commis_dir / "metadata.json", "w") as f:
                json.dump(
                    {
                        "commis_id": commis_id,
                        "status": "success",
                        "summary": "Disk is at 45% capacity",
                        "owner_id": test_user.id,
                    },
                    f,
                )

            # Now call spawn_commis with same task - should return cached result
            result = await spawn_commis_async("Check disk space on cube", model=TEST_COMMIS_MODEL)

            # Should return cached result, not create new job
            assert "completed" in result
            assert "Disk is at 45%" in result or "45%" in result

            # Verify no new job was created
            jobs = (
                db_session.query(CommisJob)
                .filter(CommisJob.concierge_course_id == concierge_run.id)
                .all()
            )
            assert len(jobs) == 1, f"Expected 1 job (cached), got {len(jobs)}"
        finally:
            reset_concierge_context(token)

    @pytest.mark.asyncio
    async def test_no_fuzzy_matching_for_similar_tasks(
        self, db_session, test_user, credential_context, temp_artifact_path, concierge_run
    ):
        """Verify spawn_commis uses EXACT task matching only.

        Prefix/fuzzy matching was removed as unsafe - near-matches could return
        the wrong commis result if tasks share prefixes. Only exact task matches
        and tool_call_id matches are supported for idempotency.
        """
        import json
        import os

        from zerg.services.commis_artifact_store import CommisArtifactStore

        token = set_concierge_context(
            course_id=concierge_run.id,
            owner_id=test_user.id,
            message_id="test-msg-5",
        )

        try:
            artifact_store = CommisArtifactStore()

            # Create TWO completed jobs with similar tasks
            for i in range(2):
                commis_id = f"test-commis-collision-{i:03d}"
                task = f"Check disk space on cube server - variant {i}"
                job = CommisJob(
                    owner_id=test_user.id,
                    concierge_course_id=concierge_run.id,
                    task=task,
                    model=TEST_COMMIS_MODEL,
                    status="success",
                    commis_id=commis_id,
                )
                db_session.add(job)
                db_session.commit()

                # Create artifacts
                commis_dir = artifact_store._get_commis_dir(commis_id)
                os.makedirs(commis_dir, exist_ok=True)
                with open(commis_dir / "result.txt", "w") as f:
                    f.write(f"Result from variant {i}")
                with open(commis_dir / "metadata.json", "w") as f:
                    json.dump(
                        {
                            "commis_id": commis_id,
                            "status": "success",
                            "summary": f"Variant {i} summary",
                            "owner_id": test_user.id,
                        },
                        f,
                    )

            # Query with a similar but different task
            # Since we only match EXACT tasks, this should create a new job
            result = await spawn_commis_async(
                "Check disk space on cube server - new request", model=TEST_COMMIS_MODEL
            )

            # Should create a new job (task doesn't match exactly)
            assert "queued successfully" in result

            # Verify we now have 3 jobs (2 completed + 1 new queued)
            jobs = (
                db_session.query(CommisJob)
                .filter(CommisJob.concierge_course_id == concierge_run.id)
                .all()
            )
            assert len(jobs) == 3, f"Expected 3 jobs (2 completed + 1 new), got {len(jobs)}"
        finally:
            reset_concierge_context(token)

    @pytest.mark.asyncio
    async def test_cross_run_isolation(
        self, db_session, test_user, credential_context, temp_artifact_path, sample_fiche, sample_thread
    ):
        """Verify idempotency is scoped to concierge_course_id.

        Jobs from different runs should NOT interfere with each other.
        """
        # Create two separate runs
        run1 = Course(
            fiche_id=sample_fiche.id,
            thread_id=sample_thread.id,
            status=CourseStatus.RUNNING,
        )
        run2 = Course(
            fiche_id=sample_fiche.id,
            thread_id=sample_thread.id,
            status=CourseStatus.RUNNING,
        )
        db_session.add_all([run1, run2])
        db_session.commit()
        db_session.refresh(run1)
        db_session.refresh(run2)

        # Spawn commis in run1
        token1 = set_concierge_context(
            course_id=run1.id,
            owner_id=test_user.id,
            message_id="test-msg-run1",
        )
        try:
            result1 = await spawn_commis_async("Check disk space", model=TEST_COMMIS_MODEL)
            assert "queued successfully" in result1
        finally:
            reset_concierge_context(token1)

        # Spawn same task in run2 - should create separate job (not dedupe across runs)
        token2 = set_concierge_context(
            course_id=run2.id,
            owner_id=test_user.id,
            message_id="test-msg-run2",
        )
        try:
            result2 = await spawn_commis_async("Check disk space", model=TEST_COMMIS_MODEL)
            assert "queued successfully" in result2
        finally:
            reset_concierge_context(token2)

        # Verify each run has its own job
        jobs_run1 = db_session.query(CommisJob).filter(CommisJob.concierge_course_id == run1.id).all()
        jobs_run2 = db_session.query(CommisJob).filter(CommisJob.concierge_course_id == run2.id).all()

        assert len(jobs_run1) == 1, f"Run 1 should have 1 job, got {len(jobs_run1)}"
        assert len(jobs_run2) == 1, f"Run 2 should have 1 job, got {len(jobs_run2)}"
