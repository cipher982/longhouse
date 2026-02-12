"""Pydantic schemas for job secrets and repo config API.

Secrets values are never returned in responses.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel
from pydantic import Field

# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


class JobSecretListItem(BaseModel):
    """Single secret entry in list response (value never exposed)."""

    key: str = Field(..., description="Secret key identifier")
    description: Optional[str] = Field(None, description="Optional hint for UI")
    created_at: datetime
    updated_at: datetime


class JobSecretUpsertRequest(BaseModel):
    """Request body for creating or updating a secret."""

    value: str = Field(..., description="Plaintext value (encrypted server-side)")
    description: Optional[str] = Field(None, description="Optional description/hint")


# ---------------------------------------------------------------------------
# Repo Config
# ---------------------------------------------------------------------------


class JobRepoConfigResponse(BaseModel):
    """Repo config response (token never exposed)."""

    repo_url: str
    branch: str
    has_token: bool = Field(..., description="Whether a PAT is stored (never exposed)")
    last_sync_sha: Optional[str] = None
    last_sync_at: Optional[datetime] = None
    last_sync_error: Optional[str] = None
    source: str = Field(..., description="'database' or 'environment'")


class JobRepoConfigRequest(BaseModel):
    """Request body for setting/updating repo config."""

    repo_url: str = Field(..., description="Git repo HTTPS URL")
    branch: str = Field("main", description="Branch to clone")
    token: Optional[str] = Field(None, description="PAT for private repos (encrypted server-side)")


class JobRepoVerifyResponse(BaseModel):
    """Result of a test-clone verification."""

    success: bool
    error: Optional[str] = None
    commit_sha: Optional[str] = None
