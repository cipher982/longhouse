"""CRUD operations for Runners.

Provides database access functions for managing runners, enrollment tokens,
and runner jobs.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime
from datetime import timedelta
from typing import Any
from typing import Optional

from sqlalchemy import update
from sqlalchemy.orm import Session

from zerg.models.models import Runner
from zerg.models.models import RunnerEnrollToken
from zerg.models.models import RunnerJob
from zerg.utils.time import utc_now_naive


# ---------------------------------------------------------------------------
# Token/Secret Helpers
# ---------------------------------------------------------------------------


def generate_token() -> str:
    """Generate a secure random token."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Hash a token using SHA256."""
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Enrollment Tokens
# ---------------------------------------------------------------------------


def create_enroll_token(
    db: Session,
    owner_id: int,
    ttl_minutes: int = 10,
) -> tuple[RunnerEnrollToken, str]:
    """Create a new enrollment token.

    Args:
        db: Database session
        owner_id: ID of the user creating the token
        ttl_minutes: Token TTL in minutes (default 10)

    Returns:
        Tuple of (token_record, plaintext_token)
    """
    token = generate_token()
    token_hash = hash_token(token)

    db_token = RunnerEnrollToken(
        owner_id=owner_id,
        token_hash=token_hash,
        expires_at=utc_now_naive() + timedelta(minutes=ttl_minutes),
    )

    db.add(db_token)
    db.commit()
    db.refresh(db_token)

    return db_token, token


def get_enroll_token_by_hash(db: Session, token_hash: str) -> Optional[RunnerEnrollToken]:
    """Get an enrollment token by its hash."""
    return db.query(RunnerEnrollToken).filter(RunnerEnrollToken.token_hash == token_hash).first()


def validate_and_consume_enroll_token(
    db: Session,
    token: str,
) -> Optional[RunnerEnrollToken]:
    """Validate and consume an enrollment token atomically.

    Returns the token record if valid and unused, None otherwise.
    Uses atomic UPDATE...WHERE...RETURNING to prevent race conditions.
    """
    token_hash = hash_token(token)
    now = utc_now_naive()

    # Atomic update: only consume if unused and not expired
    stmt = (
        update(RunnerEnrollToken)
        .where(
            RunnerEnrollToken.token_hash == token_hash,
            RunnerEnrollToken.used_at.is_(None),
            RunnerEnrollToken.expires_at > now,
        )
        .values(used_at=now)
        .returning(RunnerEnrollToken)
    )

    result = db.execute(stmt)
    db.flush()  # Flush changes but let caller handle commit

    db_token = result.scalar_one_or_none()
    return db_token


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------


def create_runner(
    db: Session,
    owner_id: int,
    name: str,
    auth_secret: str,
    labels: Optional[dict[str, str]] = None,
    capabilities: Optional[list[str]] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> Runner:
    """Create a new runner.

    Args:
        db: Database session
        owner_id: ID of the user owning the runner
        name: Runner name (must be unique per owner)
        auth_secret: Plaintext secret (will be hashed)
        labels: Optional labels for targeting
        capabilities: Optional capabilities list (defaults to ["exec.readonly"])
        metadata: Optional metadata from runner

    Returns:
        Created runner record
    """
    secret_hash = hash_token(auth_secret)

    db_runner = Runner(
        owner_id=owner_id,
        name=name,
        auth_secret_hash=secret_hash,
        labels=labels,
        capabilities=capabilities or ["exec.readonly"],
        runner_metadata=metadata,
        status="offline",
    )

    db.add(db_runner)
    db.commit()
    db.refresh(db_runner)

    return db_runner


def get_runner(db: Session, runner_id: int) -> Optional[Runner]:
    """Get a runner by ID."""
    return db.query(Runner).filter(Runner.id == runner_id).first()


def get_runner_by_name(db: Session, owner_id: int, name: str) -> Optional[Runner]:
    """Get a runner by owner and name."""
    return db.query(Runner).filter(Runner.owner_id == owner_id, Runner.name == name).first()


def get_runners(
    db: Session,
    owner_id: int,
    skip: int = 0,
    limit: int = 100,
) -> list[Runner]:
    """Get all runners for a user.

    Args:
        db: Database session
        owner_id: ID of the user
        skip: Number of records to skip
        limit: Maximum number of records to return

    Returns:
        List of runners
    """
    return db.query(Runner).filter(Runner.owner_id == owner_id).offset(skip).limit(limit).all()


def update_runner(
    db: Session,
    runner_id: int,
    name: Optional[str] = None,
    labels: Optional[dict[str, str]] = None,
    capabilities: Optional[list[str]] = None,
) -> Optional[Runner]:
    """Update a runner's configuration.

    Args:
        db: Database session
        runner_id: ID of the runner to update
        name: New name (optional)
        labels: New labels (optional)
        capabilities: New capabilities (optional)

    Returns:
        Updated runner or None if not found
    """
    db_runner = get_runner(db, runner_id)
    if not db_runner:
        return None

    if name is not None:
        db_runner.name = name
    if labels is not None:
        db_runner.labels = labels
    if capabilities is not None:
        db_runner.capabilities = capabilities

    db.commit()
    db.refresh(db_runner)

    return db_runner


def revoke_runner(db: Session, runner_id: int) -> Optional[Runner]:
    """Revoke a runner (mark as revoked, cannot reconnect).

    Args:
        db: Database session
        runner_id: ID of the runner to revoke

    Returns:
        Revoked runner or None if not found
    """
    db_runner = get_runner(db, runner_id)
    if not db_runner:
        return None

    db_runner.status = "revoked"
    db.commit()
    db.refresh(db_runner)

    return db_runner


def delete_runner(db: Session, runner_id: int) -> bool:
    """Delete a runner permanently.

    Args:
        db: Database session
        runner_id: ID of the runner to delete

    Returns:
        True if deleted, False if not found
    """
    db_runner = get_runner(db, runner_id)
    if not db_runner:
        return False

    db.delete(db_runner)
    db.commit()

    return True


# ---------------------------------------------------------------------------
# Runner Jobs
# ---------------------------------------------------------------------------


def create_runner_job(
    db: Session,
    owner_id: int,
    runner_id: int,
    command: str,
    timeout_secs: int,
    worker_id: str | None = None,
    run_id: str | None = None,
) -> RunnerJob:
    """Create a new runner job record.

    Args:
        db: Database session
        owner_id: ID of the user owning the job
        runner_id: ID of the runner to execute the job
        command: Shell command to execute
        timeout_secs: Maximum execution time in seconds
        worker_id: Optional worker ID for correlation
        run_id: Optional run ID for correlation

    Returns:
        Created job record with status='queued'
    """
    import uuid

    job = RunnerJob(
        id=str(uuid.uuid4()),
        owner_id=owner_id,
        runner_id=runner_id,
        command=command,
        timeout_secs=timeout_secs,
        worker_id=worker_id,
        run_id=run_id,
        status="queued",
    )

    db.add(job)
    db.commit()
    db.refresh(job)

    return job


def get_job(db: Session, job_id: str) -> Optional[RunnerJob]:
    """Get a job by ID.

    Args:
        db: Database session
        job_id: Job UUID as string

    Returns:
        Job record or None if not found
    """
    return db.query(RunnerJob).filter(RunnerJob.id == job_id).first()


def update_job_started(db: Session, job_id: str) -> Optional[RunnerJob]:
    """Mark a job as running and set started_at.

    Args:
        db: Database session
        job_id: Job UUID as string

    Returns:
        Updated job record or None if not found
    """
    job = get_job(db, job_id)
    if not job:
        return None

    job.status = "running"
    job.started_at = utc_now_naive()

    db.commit()
    db.refresh(job)

    return job


def update_job_output(
    db: Session,
    job_id: str,
    stream: str,
    data: str,
) -> Optional[RunnerJob]:
    """Append output data to job stdout or stderr.

    Implements truncation at 50KB combined output to prevent
    unbounded database growth.

    Args:
        db: Database session
        job_id: Job UUID as string
        stream: "stdout" or "stderr"
        data: Output data to append

    Returns:
        Updated job record or None if not found
    """
    job = get_job(db, job_id)
    if not job:
        return None

    # Append to the appropriate stream
    if stream == "stdout":
        current = job.stdout_trunc or ""
        job.stdout_trunc = current + data
    elif stream == "stderr":
        current = job.stderr_trunc or ""
        job.stderr_trunc = current + data

    # Truncate combined output at 50KB
    MAX_COMBINED_OUTPUT = 50 * 1024
    stdout_len = len(job.stdout_trunc or "")
    stderr_len = len(job.stderr_trunc or "")
    combined_len = stdout_len + stderr_len

    if combined_len > MAX_COMBINED_OUTPUT:
        # Truncate the stream that was just updated
        if stream == "stdout" and job.stdout_trunc:
            # Keep as much stderr as possible, truncate stdout
            max_stdout = MAX_COMBINED_OUTPUT - stderr_len
            if max_stdout > 0:
                job.stdout_trunc = job.stdout_trunc[:max_stdout] + "\n[truncated]"
            else:
                job.stdout_trunc = "[truncated]"
        elif stream == "stderr" and job.stderr_trunc:
            # Keep as much stdout as possible, truncate stderr
            max_stderr = MAX_COMBINED_OUTPUT - stdout_len
            if max_stderr > 0:
                job.stderr_trunc = job.stderr_trunc[:max_stderr] + "\n[truncated]"
            else:
                job.stderr_trunc = "[truncated]"

    db.commit()
    db.refresh(job)

    return job


def update_job_completed(
    db: Session,
    job_id: str,
    exit_code: int,
    duration_ms: int,
) -> Optional[RunnerJob]:
    """Mark a job as completed (success or failed based on exit_code).

    Args:
        db: Database session
        job_id: Job UUID as string
        exit_code: Command exit code (0 = success, non-zero = failed)
        duration_ms: Execution duration in milliseconds

    Returns:
        Updated job record or None if not found
    """
    job = get_job(db, job_id)
    if not job:
        return None

    job.status = "success" if exit_code == 0 else "failed"
    job.exit_code = exit_code
    job.finished_at = utc_now_naive()

    db.commit()
    db.refresh(job)

    return job


def update_job_error(
    db: Session,
    job_id: str,
    error: str,
) -> Optional[RunnerJob]:
    """Mark a job as failed with an error message.

    Args:
        db: Database session
        job_id: Job UUID as string
        error: Error message

    Returns:
        Updated job record or None if not found
    """
    job = get_job(db, job_id)
    if not job:
        return None

    job.status = "failed"
    job.error = error
    job.finished_at = utc_now_naive()

    db.commit()
    db.refresh(job)

    return job


def update_job_timeout(db: Session, job_id: str) -> Optional[RunnerJob]:
    """Mark a job as timed out.

    Args:
        db: Database session
        job_id: Job UUID as string

    Returns:
        Updated job record or None if not found
    """
    job = get_job(db, job_id)
    if not job:
        return None

    job.status = "timeout"
    job.finished_at = utc_now_naive()

    db.commit()
    db.refresh(job)

    return job


def get_runner_jobs(
    db: Session,
    runner_id: int,
    skip: int = 0,
    limit: int = 100,
) -> list[RunnerJob]:
    """Get jobs for a specific runner.

    Args:
        db: Database session
        runner_id: ID of the runner
        skip: Number of records to skip
        limit: Maximum number of records to return

    Returns:
        List of runner jobs
    """
    return db.query(RunnerJob).filter(RunnerJob.runner_id == runner_id).offset(skip).limit(limit).all()
