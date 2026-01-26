"""Skills management API endpoints.

Provides REST API for:
- Listing available skills
- Getting skill details
- Managing workspace skills
- Skill eligibility information
"""

import logging
import os
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from pydantic import BaseModel
from pydantic import Field

from zerg.dependencies.auth import get_current_user
from zerg.skills.loader import SkillLoader
from zerg.skills.models import SkillEntry
from zerg.skills.models import SkillSource
from zerg.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/skills", tags=["skills"])

# Default workspace base path (same as workspace_manager.py)
DEFAULT_WORKSPACE_PATH = "/var/jarvis/workspaces"


# ---------------------------------------------------------------------------
# Path Validation (Security)
# ---------------------------------------------------------------------------


def get_workspace_base_path() -> Path:
    """Get the allowed base path for workspaces."""
    return Path(os.getenv("JARVIS_WORKSPACE_PATH", DEFAULT_WORKSPACE_PATH))


def validate_workspace_path(workspace_path: Optional[str]) -> Optional[Path]:
    """Validate workspace path to prevent path traversal attacks.

    Args:
        workspace_path: User-provided workspace path

    Returns:
        Validated Path or None if no path provided

    Raises:
        HTTPException: If path is invalid or outside allowed directory
    """
    if not workspace_path:
        return None

    # Resolve to absolute path (handles .. and symlinks)
    try:
        resolved = Path(workspace_path).resolve()
    except (OSError, ValueError) as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid workspace path: {e}",
        )

    # Check for path traversal attempts
    workspace_base = get_workspace_base_path().resolve()

    # Allow the exact base path or subdirectories
    try:
        resolved.relative_to(workspace_base)
    except ValueError:
        # Path is not relative to workspace base
        logger.warning(f"Workspace path traversal attempt blocked: {workspace_path} " f"(resolved: {resolved}, base: {workspace_base})")
        raise HTTPException(
            status_code=400,
            detail=f"Workspace path must be within {workspace_base}",
        )

    return resolved


# ---------------------------------------------------------------------------
# Pydantic models for API
# ---------------------------------------------------------------------------


class SkillRequirementsResponse(BaseModel):
    """Skill requirements in API response."""

    bins: List[str] = Field(default_factory=list, description="Required binaries")
    env: List[str] = Field(default_factory=list, description="Required env vars")
    config: List[str] = Field(default_factory=list, description="Required config keys")


class MissingRequirementsResponse(BaseModel):
    """Missing requirements in API response."""

    bins: List[str] = Field(default_factory=list)
    env: List[str] = Field(default_factory=list)
    config: List[str] = Field(default_factory=list)


class SkillResponse(BaseModel):
    """Skill information in API response."""

    name: str
    description: str
    emoji: str = ""
    source: str
    eligible: bool
    user_invocable: bool
    model_invocable: bool
    missing_requirements: MissingRequirementsResponse
    homepage: str = ""


class SkillDetailResponse(BaseModel):
    """Detailed skill information."""

    name: str
    description: str
    emoji: str = ""
    source: str
    eligible: bool
    user_invocable: bool
    model_invocable: bool
    missing_requirements: MissingRequirementsResponse
    homepage: str = ""
    content: str = ""
    requirements: SkillRequirementsResponse
    base_dir: str = ""


class SkillListResponse(BaseModel):
    """Response for listing skills."""

    skills: List[SkillResponse]
    total: int
    eligible_count: int


class SkillPromptResponse(BaseModel):
    """Response for skill prompt generation."""

    prompt: str
    skill_count: int
    version: int


class SkillCommandResponse(BaseModel):
    """User-invocable skill command."""

    name: str
    description: str
    emoji: str = ""


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def entry_to_response(entry: SkillEntry) -> SkillResponse:
    """Convert SkillEntry to API response."""
    return SkillResponse(
        name=entry.skill.name,
        description=entry.skill.description,
        emoji=entry.skill.manifest.emoji,
        source=entry.skill.source.value,
        eligible=entry.eligible,
        user_invocable=entry.skill.manifest.user_invocable,
        model_invocable=entry.skill.manifest.model_invocable,
        missing_requirements=MissingRequirementsResponse(
            bins=entry.missing_bins,
            env=entry.missing_env,
            config=entry.missing_config,
        ),
        homepage=entry.skill.manifest.homepage,
    )


def entry_to_detail_response(entry: SkillEntry) -> SkillDetailResponse:
    """Convert SkillEntry to detailed API response."""
    reqs = entry.skill.manifest.requires
    return SkillDetailResponse(
        name=entry.skill.name,
        description=entry.skill.description,
        emoji=entry.skill.manifest.emoji,
        source=entry.skill.source.value,
        eligible=entry.eligible,
        user_invocable=entry.skill.manifest.user_invocable,
        model_invocable=entry.skill.manifest.model_invocable,
        missing_requirements=MissingRequirementsResponse(
            bins=entry.missing_bins,
            env=entry.missing_env,
            config=entry.missing_config,
        ),
        homepage=entry.skill.manifest.homepage,
        content=entry.skill.content,
        requirements=SkillRequirementsResponse(
            bins=list(reqs.bins),
            env=list(reqs.env),
            config=list(reqs.config),
        ),
        base_dir=str(entry.skill.base_dir),
    )


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=SkillListResponse)
async def list_skills(
    workspace_path: Optional[str] = Query(None, description="Workspace path to load skills from"),
    source: Optional[str] = Query(None, description="Filter by source (bundled, user, workspace)"),
    eligible_only: bool = Query(False, description="Only return eligible skills"),
    current_user=Depends(get_current_user),
) -> SkillListResponse:
    """List all available skills.

    Returns skills from all sources (bundled, user, workspace) with
    eligibility information based on current environment.
    """
    # Per-request loader (thread-safe)
    loader = SkillLoader()

    # Validate workspace path (security)
    workspace = validate_workspace_path(workspace_path)
    entries = loader.load_skill_entries(workspace_path=workspace)

    # Filter by source if specified
    if source:
        try:
            source_enum = SkillSource(source)
            entries = [e for e in entries if e.skill.source == source_enum]
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid source: {source}. Must be one of: bundled, user, workspace, package",
            )

    # Filter by eligibility
    if eligible_only:
        entries = [e for e in entries if e.eligible]

    eligible_count = sum(1 for e in entries if e.eligible)

    return SkillListResponse(
        skills=[entry_to_response(e) for e in entries],
        total=len(entries),
        eligible_count=eligible_count,
    )


@router.get("/commands", response_model=List[SkillCommandResponse])
async def list_skill_commands(
    workspace_path: Optional[str] = Query(None),
    current_user=Depends(get_current_user),
) -> List[SkillCommandResponse]:
    """List user-invocable skill commands.

    Returns skills that can be invoked via slash commands,
    formatted for UI command palettes.
    """
    # Per-request loader (thread-safe)
    loader = SkillLoader()
    workspace = validate_workspace_path(workspace_path)
    entries = loader.load_skill_entries(workspace_path=workspace, filter_eligible=True)

    commands = []
    for entry in entries:
        if entry.skill.manifest.user_invocable:
            commands.append(
                SkillCommandResponse(
                    name=entry.skill.name,
                    description=entry.skill.description[:100],
                    emoji=entry.skill.manifest.emoji,
                )
            )

    return sorted(commands, key=lambda c: c.name)


@router.get("/prompt", response_model=SkillPromptResponse)
async def get_skills_prompt(
    workspace_path: Optional[str] = Query(None),
    allowed: Optional[str] = Query(None, description="Comma-separated skill names/patterns to include"),
    current_user=Depends(get_current_user),
) -> SkillPromptResponse:
    """Generate skills prompt for system prompt injection.

    Returns a formatted markdown prompt containing eligible skills,
    suitable for including in an fiche's system prompt.
    """
    # Per-request registry (thread-safe)
    registry = SkillRegistry()
    workspace = validate_workspace_path(workspace_path)
    registry.load_for_workspace(workspace)

    allowed_list = allowed.split(",") if allowed else None
    snapshot = registry.get_snapshot(allowed=allowed_list)

    return SkillPromptResponse(
        prompt=snapshot.prompt,
        skill_count=len(snapshot.skills),
        version=snapshot.version,
    )


@router.get("/{skill_name}", response_model=SkillDetailResponse)
async def get_skill(
    skill_name: str,
    workspace_path: Optional[str] = Query(None),
    current_user=Depends(get_current_user),
) -> SkillDetailResponse:
    """Get detailed information about a specific skill.

    Returns the skill's content, requirements, and eligibility status.
    """
    # Per-request loader (thread-safe)
    loader = SkillLoader()
    workspace = validate_workspace_path(workspace_path)
    entries = loader.load_skill_entries(workspace_path=workspace)

    for entry in entries:
        if entry.skill.name == skill_name:
            return entry_to_detail_response(entry)

    raise HTTPException(status_code=404, detail=f"Skill not found: {skill_name}")


@router.post("/reload")
async def reload_skills(
    workspace_path: Optional[str] = Query(None),
    current_user=Depends(get_current_user),
) -> Dict[str, Any]:
    """Reload skills from filesystem.

    Forces a reload of all skills, useful after adding/modifying skills.
    """
    # Per-request registry (thread-safe)
    registry = SkillRegistry()
    workspace = validate_workspace_path(workspace_path)
    registry.load_for_workspace(workspace)

    entries = registry.get_all_entries()
    eligible_count = sum(1 for e in entries if e.eligible)

    return {
        "message": "Skills reloaded",
        "total": len(entries),
        "eligible": eligible_count,
        "version": registry._version,
    }
