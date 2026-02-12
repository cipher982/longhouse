"""Job Secrets & Repo Config API.

REST endpoints for managing:
- Arbitrary key-value secrets for scheduled jobs (encrypted at rest)
- Git repo configuration for job scripts (DB-first, env-var fallback)

All endpoints require authentication via ``get_current_user``.
Secret values are never returned in responses.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Response
from fastapi import status
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.dependencies.auth import get_current_user
from zerg.models.models import JobRepoConfig
from zerg.models.models import JobSecret
from zerg.models.models import User
from zerg.schemas.job_settings_schemas import JobRepoConfigRequest
from zerg.schemas.job_settings_schemas import JobRepoConfigResponse
from zerg.schemas.job_settings_schemas import JobRepoVerifyResponse
from zerg.schemas.job_settings_schemas import JobSecretListItem
from zerg.schemas.job_settings_schemas import JobSecretUpsertRequest
from zerg.utils.crypto import encrypt

logger = logging.getLogger(__name__)

router = APIRouter(tags=["job-settings"])


# ---------------------------------------------------------------------------
# Secrets endpoints
# ---------------------------------------------------------------------------


@router.get("/jobs/secrets", response_model=list[JobSecretListItem])
def list_job_secrets(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[JobSecretListItem]:
    """List all secret keys for the current user (values never returned)."""
    rows = db.query(JobSecret).filter(JobSecret.owner_id == current_user.id).order_by(JobSecret.key).all()
    return [
        JobSecretListItem(
            key=row.key,
            description=row.description,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in rows
    ]


@router.put("/jobs/secrets/{key}", status_code=status.HTTP_200_OK)
def upsert_job_secret(
    key: str,
    request: JobSecretUpsertRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Create or update a secret (value encrypted at rest)."""
    if not key or len(key) > 255:
        raise HTTPException(status_code=400, detail="Key must be 1-255 characters")

    encrypted = encrypt(request.value)

    existing = db.query(JobSecret).filter(JobSecret.owner_id == current_user.id, JobSecret.key == key).first()

    if existing:
        existing.encrypted_value = encrypted
        existing.description = request.description
        logger.info("Updated job secret '%s' for user %d", key, current_user.id)
    else:
        secret = JobSecret(
            owner_id=current_user.id,
            key=key,
            encrypted_value=encrypted,
            description=request.description,
        )
        db.add(secret)
        logger.info("Created job secret '%s' for user %d", key, current_user.id)

    db.commit()
    return {"success": True}


@router.delete("/jobs/secrets/{key}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job_secret(
    key: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    """Delete a secret."""
    existing = db.query(JobSecret).filter(JobSecret.owner_id == current_user.id, JobSecret.key == key).first()
    if not existing:
        raise HTTPException(status_code=404, detail=f"Secret '{key}' not found")

    db.delete(existing)
    db.commit()
    logger.info("Deleted job secret '%s' for user %d", key, current_user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Repo config endpoints
# ---------------------------------------------------------------------------


@router.get("/jobs/repo/config", response_model=JobRepoConfigResponse)
def get_repo_config(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> JobRepoConfigResponse:
    """Get current repo config. Falls back to env vars if no DB config."""
    row = db.query(JobRepoConfig).filter(JobRepoConfig.owner_id == current_user.id).first()

    if row:
        return JobRepoConfigResponse(
            repo_url=row.repo_url,
            branch=row.branch,
            has_token=row.encrypted_token is not None,
            last_sync_sha=row.last_sync_sha,
            last_sync_at=row.last_sync_at,
            last_sync_error=row.last_sync_error,
            source="database",
        )

    # Fallback to env vars
    from zerg.config import get_settings

    settings = get_settings()
    if settings.jobs_git_repo_url:
        return JobRepoConfigResponse(
            repo_url=settings.jobs_git_repo_url,
            branch=settings.jobs_git_branch,
            has_token=settings.jobs_git_token is not None,
            last_sync_sha=None,
            last_sync_at=None,
            last_sync_error=None,
            source="environment",
        )

    raise HTTPException(status_code=404, detail="No repo config found")


@router.post("/jobs/repo/config", status_code=status.HTTP_200_OK)
def set_repo_config(
    request: JobRepoConfigRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Set or update repo config (token encrypted at rest)."""
    encrypted_token = encrypt(request.token) if request.token else None

    existing = db.query(JobRepoConfig).filter(JobRepoConfig.owner_id == current_user.id).first()

    if existing:
        existing.repo_url = request.repo_url
        existing.branch = request.branch
        existing.encrypted_token = encrypted_token
        # Clear sync state on config change
        existing.last_sync_sha = None
        existing.last_sync_at = None
        existing.last_sync_error = None
        logger.info("Updated job repo config for user %d", current_user.id)
    else:
        config = JobRepoConfig(
            owner_id=current_user.id,
            repo_url=request.repo_url,
            branch=request.branch,
            encrypted_token=encrypted_token,
        )
        db.add(config)
        logger.info("Created job repo config for user %d", current_user.id)

    db.commit()
    return {"success": True}


@router.post("/jobs/repo/verify", response_model=JobRepoVerifyResponse)
async def verify_repo_config(
    request: JobRepoConfigRequest,
    current_user: User = Depends(get_current_user),
) -> JobRepoVerifyResponse:
    """Test-clone the repo to validate URL + token without persisting."""
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            from zerg.jobs.git_sync import GitSyncService

            service = GitSyncService(
                repo_url=request.repo_url,
                local_path=Path(tmpdir) / "test-clone",
                branch=request.branch,
                token=request.token,
            )
            await service.ensure_cloned()
            return JobRepoVerifyResponse(
                success=True,
                commit_sha=service.current_sha,
            )
        except Exception as e:
            return JobRepoVerifyResponse(
                success=False,
                error=str(e),
            )


@router.delete("/jobs/repo/config", status_code=status.HTTP_204_NO_CONTENT)
def delete_repo_config(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    """Remove repo config (reverts to env vars or local-only)."""
    existing = db.query(JobRepoConfig).filter(JobRepoConfig.owner_id == current_user.id).first()
    if not existing:
        raise HTTPException(status_code=404, detail="No repo config found")

    db.delete(existing)
    db.commit()
    logger.info("Deleted job repo config for user %d", current_user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/jobs/repo/sync")
async def trigger_repo_sync(
    current_user: User = Depends(get_current_user),
) -> dict:
    """Trigger an immediate git pull (uses the currently active git sync service)."""
    from zerg.jobs.git_sync import get_git_sync_service

    service = get_git_sync_service()
    if not service:
        raise HTTPException(status_code=404, detail="No git sync service active")

    result = await service.refresh()
    return result
