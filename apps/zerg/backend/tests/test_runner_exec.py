"""Tests for runner_exec tool and job management.

Tests the complete runner execution flow:
- Job creation and management
- Target resolution (by name and ID)
- Command execution via runners
- Output streaming and collection
- Error handling
- Concurrency control
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy.orm import Session

from zerg.context import WorkerContext, set_worker_context, reset_worker_context
from zerg.crud import runner_crud
from zerg.models.models import Runner, User
from zerg.services.runner_job_dispatcher import get_runner_job_dispatcher
from zerg.tools.builtin.runner_tools import runner_exec


@pytest.fixture
def test_runner(db: Session, test_user: User) -> tuple[Runner, str]:
    """Create a test runner with auth secret.

    Returns:
        Tuple of (runner, plaintext_secret)
    """
    secret = runner_crud.generate_token()
    runner = runner_crud.create_runner(
        db=db,
        owner_id=test_user.id,
        name="test-laptop",
        auth_secret=secret,
        labels={"env": "test"},
        capabilities=["exec.readonly"],
        metadata={"hostname": "test-host"},
    )
    # Mark as online for tests
    runner.status = "online"
    db.commit()
    return runner, secret


@pytest.fixture
def worker_context(test_user: User):
    """Create and set a test worker context."""
    ctx = WorkerContext(
        worker_id="test-worker",
        owner_id=test_user.id,
        run_id="test-run",
        task="test task",
    )
    token = set_worker_context(ctx)
    yield ctx
    reset_worker_context(token)


class TestJobCRUD:
    """Tests for runner job CRUD operations."""

    def test_create_runner_job(self, db: Session, test_user: User, test_runner: tuple[Runner, str]):
        """Test creating a runner job record."""
        runner, _ = test_runner

        job = runner_crud.create_runner_job(
            db=db,
            owner_id=test_user.id,
            runner_id=runner.id,
            command="echo 'test'",
            timeout_secs=30,
            worker_id="test-worker",
            run_id="test-run",
        )

        assert job.id is not None
        assert job.owner_id == test_user.id
        assert job.runner_id == runner.id
        assert job.command == "echo 'test'"
        assert job.timeout_secs == 30
        assert job.status == "queued"
        assert job.worker_id == "test-worker"
        assert job.run_id == "test-run"

    def test_update_job_started(self, db: Session, test_user: User, test_runner: tuple[Runner, str]):
        """Test marking job as running."""
        runner, _ = test_runner

        job = runner_crud.create_runner_job(
            db=db,
            owner_id=test_user.id,
            runner_id=runner.id,
            command="echo 'test'",
            timeout_secs=30,
        )

        updated = runner_crud.update_job_started(db, job.id)

        assert updated is not None
        assert updated.status == "running"
        assert updated.started_at is not None

    def test_update_job_output(self, db: Session, test_user: User, test_runner: tuple[Runner, str]):
        """Test appending output to job."""
        runner, _ = test_runner

        job = runner_crud.create_runner_job(
            db=db,
            owner_id=test_user.id,
            runner_id=runner.id,
            command="echo 'test'",
            timeout_secs=30,
        )

        # Add stdout
        runner_crud.update_job_output(db, job.id, "stdout", "line 1\n")
        runner_crud.update_job_output(db, job.id, "stdout", "line 2\n")

        # Add stderr
        runner_crud.update_job_output(db, job.id, "stderr", "error\n")

        job = runner_crud.get_job(db, job.id)
        assert job.stdout_trunc == "line 1\nline 2\n"
        assert job.stderr_trunc == "error\n"

    def test_update_job_output_truncation(self, db: Session, test_user: User, test_runner: tuple[Runner, str]):
        """Test that output is truncated at 50KB combined."""
        runner, _ = test_runner

        job = runner_crud.create_runner_job(
            db=db,
            owner_id=test_user.id,
            runner_id=runner.id,
            command="echo 'test'",
            timeout_secs=30,
        )

        # Add 30KB to stdout
        large_output = "x" * (30 * 1024)
        runner_crud.update_job_output(db, job.id, "stdout", large_output)

        # Add 25KB to stderr (total would be > 50KB)
        large_error = "y" * (25 * 1024)
        runner_crud.update_job_output(db, job.id, "stderr", large_error)

        job = runner_crud.get_job(db, job.id)

        # Combined output should be truncated to <= 50KB
        # Account for the "[truncated]" suffix
        combined_len = len(job.stdout_trunc or "") + len(job.stderr_trunc or "")
        assert combined_len <= 51 * 1024, f"Combined output {combined_len} exceeds 51KB (50KB + truncation message buffer)"

    def test_update_job_completed_success(self, db: Session, test_user: User, test_runner: tuple[Runner, str]):
        """Test marking job as completed with success."""
        runner, _ = test_runner

        job = runner_crud.create_runner_job(
            db=db,
            owner_id=test_user.id,
            runner_id=runner.id,
            command="echo 'test'",
            timeout_secs=30,
        )

        updated = runner_crud.update_job_completed(db, job.id, exit_code=0, duration_ms=1234)

        assert updated is not None
        assert updated.status == "success"
        assert updated.exit_code == 0
        assert updated.finished_at is not None

    def test_update_job_completed_failed(self, db: Session, test_user: User, test_runner: tuple[Runner, str]):
        """Test marking job as completed with failure."""
        runner, _ = test_runner

        job = runner_crud.create_runner_job(
            db=db,
            owner_id=test_user.id,
            runner_id=runner.id,
            command="false",
            timeout_secs=30,
        )

        updated = runner_crud.update_job_completed(db, job.id, exit_code=1, duration_ms=567)

        assert updated is not None
        assert updated.status == "failed"
        assert updated.exit_code == 1

    def test_update_job_error(self, db: Session, test_user: User, test_runner: tuple[Runner, str]):
        """Test marking job as failed with error."""
        runner, _ = test_runner

        job = runner_crud.create_runner_job(
            db=db,
            owner_id=test_user.id,
            runner_id=runner.id,
            command="echo 'test'",
            timeout_secs=30,
        )

        updated = runner_crud.update_job_error(db, job.id, "Connection lost")

        assert updated is not None
        assert updated.status == "failed"
        assert updated.error == "Connection lost"
        assert updated.finished_at is not None

    def test_update_job_timeout(self, db: Session, test_user: User, test_runner: tuple[Runner, str]):
        """Test marking job as timed out."""
        runner, _ = test_runner

        job = runner_crud.create_runner_job(
            db=db,
            owner_id=test_user.id,
            runner_id=runner.id,
            command="sleep 100",
            timeout_secs=5,
        )

        updated = runner_crud.update_job_timeout(db, job.id)

        assert updated is not None
        assert updated.status == "timeout"
        assert updated.finished_at is not None


class TestRunnerExecTool:
    """Tests for runner_exec tool."""

    def test_runner_exec_requires_worker_context(self):
        """Test that runner_exec requires worker context."""
        result = runner_exec("test-laptop", "echo 'hello'")

        assert result.get("ok") is False
        assert "worker context" in result["user_message"].lower()

    def test_runner_exec_validates_parameters(self, worker_context):
        """Test parameter validation."""
        # Missing target
        result = runner_exec("", "echo 'hello'")
        assert result["ok"] is False
        assert "target parameter is required" in result["user_message"]

        # Missing command
        result = runner_exec("test-laptop", "")
        assert result["ok"] is False
        assert "command parameter is required" in result["user_message"]

        # Invalid timeout
        result = runner_exec("test-laptop", "echo 'hello'", timeout_secs=0)
        assert result["ok"] is False
        assert "timeout_secs must be positive" in result["user_message"]

    def test_runner_exec_resolves_target_by_name(self, worker_context, test_runner: tuple[Runner, str]):
        """Test target resolution by name."""
        runner, _ = test_runner

        # Mock the dispatcher to avoid actual execution
        with patch('zerg.tools.builtin.runner_tools.get_runner_job_dispatcher') as mock_dispatcher:
            mock_dispatcher.return_value.dispatch_job = AsyncMock(return_value={
                "ok": True,
                "data": {
                    "exit_code": 0,
                    "stdout": "test output",
                    "stderr": "",
                    "duration_ms": 100,
                },
            })

            result = runner_exec("test-laptop", "echo 'hello'")

            assert result["ok"] is True
            assert result["data"]["target"] == "test-laptop"
            assert result["data"]["command"] == "echo 'hello'"

    def test_runner_exec_resolves_target_by_id(self, worker_context, test_runner: tuple[Runner, str]):
        """Test target resolution by explicit ID."""
        runner, _ = test_runner

        with patch('zerg.tools.builtin.runner_tools.get_runner_job_dispatcher') as mock_dispatcher:
            mock_dispatcher.return_value.dispatch_job = AsyncMock(return_value={
                "ok": True,
                "data": {
                    "exit_code": 0,
                    "stdout": "test output",
                    "stderr": "",
                    "duration_ms": 100,
                },
            })

            result = runner_exec(f"runner:{runner.id}", "echo 'hello'")

            assert result["ok"] is True
            assert result["data"]["target"] == "test-laptop"

    def test_runner_exec_unknown_runner(self, worker_context):
        """Test execution on unknown runner."""
        result = runner_exec("unknown-runner", "echo 'hello'")

        assert result["ok"] is False
        assert "not found" in result["user_message"].lower()

    def test_runner_exec_revoked_runner(self, worker_context, test_runner: tuple[Runner, str], db: Session):
        """Test execution on revoked runner."""
        runner, _ = test_runner
        runner.status = "revoked"
        db.commit()

        result = runner_exec("test-laptop", "echo 'hello'")

        assert result["ok"] is False
        assert "revoked" in result["user_message"].lower()

    def test_runner_exec_offline_runner(self, worker_context, test_runner: tuple[Runner, str], db: Session):
        """Test execution on offline runner."""
        runner, _ = test_runner
        runner.status = "offline"
        db.commit()

        result = runner_exec("test-laptop", "echo 'hello'")

        assert result["ok"] is False
        assert "offline" in result["user_message"].lower()

    def test_runner_exec_success(self, worker_context, test_runner: tuple[Runner, str]):
        """Test successful command execution."""
        runner, _ = test_runner

        with patch('zerg.tools.builtin.runner_tools.get_runner_job_dispatcher') as mock_dispatcher:
            mock_dispatcher.return_value.dispatch_job = AsyncMock(return_value={
                "ok": True,
                "data": {
                    "exit_code": 0,
                    "stdout": "test output\n",
                    "stderr": "",
                    "duration_ms": 234,
                },
            })

            result = runner_exec("test-laptop", "echo 'test'")

            assert result["ok"] is True
            assert result["data"]["exit_code"] == 0
            assert result["data"]["stdout"] == "test output\n"
            assert result["data"]["stderr"] == ""
            assert result["data"]["duration_ms"] == 234

    def test_runner_exec_nonzero_exit_code(self, worker_context, test_runner: tuple[Runner, str]):
        """Test command with non-zero exit code (not an error)."""
        runner, _ = test_runner

        with patch('zerg.tools.builtin.runner_tools.get_runner_job_dispatcher') as mock_dispatcher:
            mock_dispatcher.return_value.dispatch_job = AsyncMock(return_value={
                "ok": True,
                "data": {
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": "error message\n",
                    "duration_ms": 123,
                },
            })

            result = runner_exec("test-laptop", "false")

            # Non-zero exit code is still a success envelope
            assert result["ok"] is True
            assert result["data"]["exit_code"] == 1
            assert result["data"]["stderr"] == "error message\n"

    def test_runner_exec_execution_error(self, worker_context, test_runner: tuple[Runner, str]):
        """Test handling of execution errors."""
        runner, _ = test_runner

        with patch('zerg.tools.builtin.runner_tools.get_runner_job_dispatcher') as mock_dispatcher:
            mock_dispatcher.return_value.dispatch_job = AsyncMock(return_value={
                "ok": False,
                "error": {
                    "type": "execution_error",
                    "message": "Runner is busy with another job",
                },
            })

            result = runner_exec("test-laptop", "echo 'test'")

            assert result["ok"] is False
            assert "busy" in result["user_message"].lower()


class TestJobDispatcher:
    """Tests for RunnerJobDispatcher."""

    def test_concurrency_control(self):
        """Test that dispatcher enforces one job per runner."""
        dispatcher = get_runner_job_dispatcher()

        # Clear any previous state
        dispatcher._runner_active_jobs.clear()

        runner_id = 1

        # Initially can accept job
        assert dispatcher.can_accept_job(runner_id) is True

        # Mark job active
        dispatcher.mark_job_active(runner_id, "job-1")

        # Now cannot accept another job
        assert dispatcher.can_accept_job(runner_id) is False

        # Clear active job
        dispatcher.clear_active_job(runner_id)

        # Can accept jobs again
        assert dispatcher.can_accept_job(runner_id) is True

    @pytest.mark.asyncio
    async def test_dispatch_job_runner_busy(self, db: Session, test_user: User, test_runner: tuple[Runner, str]):
        """Test dispatching job when runner is busy."""
        runner, _ = test_runner
        dispatcher = get_runner_job_dispatcher()

        # Mark runner as busy
        dispatcher.mark_job_active(runner.id, "existing-job")

        try:
            result = await dispatcher.dispatch_job(
                db=db,
                owner_id=test_user.id,
                runner_id=runner.id,
                command="echo 'test'",
                timeout_secs=30,
            )

            assert result["ok"] is False
            assert "busy" in result["error"]["message"].lower()
        finally:
            # Cleanup
            dispatcher.clear_active_job(runner.id)

    @pytest.mark.asyncio
    async def test_dispatch_job_runner_offline(self, db: Session, test_user: User, test_runner: tuple[Runner, str]):
        """Test dispatching job to offline runner."""
        runner, _ = test_runner
        runner.status = "offline"
        db.commit()

        dispatcher = get_runner_job_dispatcher()

        result = await dispatcher.dispatch_job(
            db=db,
            owner_id=test_user.id,
            runner_id=runner.id,
            command="echo 'test'",
            timeout_secs=30,
        )

        assert result["ok"] is False
        assert "offline" in result["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_complete_job(self):
        """Test completing a pending job."""
        dispatcher = get_runner_job_dispatcher()

        # Create a pending job future
        job_id = "test-job-123"
        future = asyncio.Future()
        dispatcher._pending_jobs[job_id] = future

        # Mark runner active
        runner_id = 1
        dispatcher.mark_job_active(runner_id, job_id)

        # Complete the job
        result = {"ok": True, "data": {"exit_code": 0}}
        dispatcher.complete_job(job_id, result, runner_id)

        # Future should be resolved
        assert future.done()
        assert await future == result

        # Active job should be cleared
        assert dispatcher.can_accept_job(runner_id) is True

        # Job should be removed from pending
        assert job_id not in dispatcher._pending_jobs
