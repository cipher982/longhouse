"""Insights API for agent infrastructure — track learnings across sessions.

Provides endpoints for:
- POST /api/insights — create or deduplicate insight (same title+project within 7 days → update)
- GET /api/insights — browser-authenticated insight query
- GET /api/agents/insights — machine-authenticated insight query
- POST /api/insights/{id}/archive — browser-authenticated archive
- POST /api/insights/{id}/unarchive — browser-authenticated restore
"""

import logging
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import List
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.dependencies.agents_auth import require_single_tenant
from zerg.dependencies.agents_auth import verify_agents_token
from zerg.dependencies.browser_auth import get_current_browser_user
from zerg.models.work import INSIGHT_DEDUP_WINDOW_DAYS
from zerg.models.work import INSIGHT_ORIGIN_MANUAL
from zerg.models.work import Insight
from zerg.models.work import insight_visibility_clause
from zerg.models.work import user_visible_insight_clause
from zerg.utils.time import UTCBaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/insights", tags=["insights"])
machine_router = APIRouter(prefix="/agents/insights", tags=["insights"])


# ---------------------------------------------------------------------------
# Request / Response Models
# ---------------------------------------------------------------------------


class InsightCreateRequest(BaseModel):
    """Request body for creating an insight."""

    insight_type: str = Field(..., description="Type: pattern, failure, improvement, learning")
    title: str = Field(..., description="Short summary of the insight")
    description: Optional[str] = Field(None, description="Detailed explanation")
    project: Optional[str] = Field(None, description="Project name")
    severity: str = Field("info", description="Severity: info, warning, critical")
    confidence: Optional[float] = Field(None, description="Confidence score 0.0-1.0")
    tags: Optional[List[str]] = Field(None, description="Tags for categorization")
    session_id: Optional[str] = Field(None, description="Source session UUID")
    origin: Optional[str] = Field(None, description="Origin label for the insight, e.g. manual, reflection, hindsight")


class InsightResponse(UTCBaseModel):
    """Response for a single insight."""

    id: str = Field(..., description="Insight UUID")
    insight_type: str
    title: str
    description: Optional[str] = None
    project: Optional[str] = None
    origin: Optional[str] = None
    severity: str = "info"
    confidence: Optional[float] = None
    tags: Optional[List[str]] = None
    observations: Optional[List[str]] = None
    session_id: Optional[str] = None
    archived_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class InsightListResponse(BaseModel):
    """Response for insight list."""

    insights: List[InsightResponse]
    total: int


def _get_insight_or_404(db: Session, insight_id: str) -> Insight:
    insight = db.query(Insight).filter(Insight.id == insight_id).first()
    if insight is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Insight not found")
    return insight


def _normalize_origin(origin: Optional[str]) -> Optional[str]:
    if origin is None:
        return None
    normalized = origin.strip()
    return normalized or None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", response_model=InsightResponse)
async def create_insight(
    body: InsightCreateRequest,
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> InsightResponse:
    """Create or deduplicate an insight.

    If an insight with the same title AND project exists within the last 7 days,
    updates its confidence and appends to observations instead of creating a new one.
    """
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=INSIGHT_DEDUP_WINDOW_DAYS)
        normalized_origin = _normalize_origin(body.origin) or INSIGHT_ORIGIN_MANUAL

        # Dedup: look for existing insight with same title + project
        query = db.query(Insight).filter(
            user_visible_insight_clause(Insight),
            Insight.title == body.title,
            Insight.created_at >= cutoff,
        )
        if body.project is not None:
            query = query.filter(Insight.project == body.project)
        else:
            query = query.filter(Insight.project.is_(None))

        existing = query.first()

        if existing:
            # Update existing insight
            if body.confidence is not None:
                existing.confidence = body.confidence

            # Append observation
            observations = existing.observations or []
            observation_entry = f"{datetime.now(timezone.utc).isoformat()}: {body.description or body.title}"
            observations.append(observation_entry)
            existing.observations = observations

            # Force SQLAlchemy to detect the JSON mutation
            from sqlalchemy.orm.attributes import flag_modified

            flag_modified(existing, "observations")

            db.commit()
            db.refresh(existing)

            return _insight_to_response(existing)

        # Cross-project dedup: check for same title in ANY project within 7 days
        cross_match = (
            db.query(Insight)
            .filter(
                user_visible_insight_clause(Insight),
                Insight.title == body.title,
                Insight.created_at >= cutoff,
            )
            .first()
        )

        if cross_match:
            # Merge into the cross-project match
            if body.confidence is not None:
                cross_match.confidence = body.confidence

            observations = cross_match.observations or []
            prefix = f"[{body.project}] " if body.project else ""
            observation_entry = f"{datetime.now(timezone.utc).isoformat()}: {prefix}{body.description or body.title}"
            observations.append(observation_entry)
            cross_match.observations = observations

            # Add source project as tag if not already present
            existing_tags = cross_match.tags or []
            if body.project and body.project not in existing_tags:
                cross_match.tags = existing_tags + [body.project]
                from sqlalchemy.orm.attributes import flag_modified as _flag_modified

                _flag_modified(cross_match, "tags")

            from sqlalchemy.orm.attributes import flag_modified

            flag_modified(cross_match, "observations")

            db.commit()
            db.refresh(cross_match)

            return _insight_to_response(cross_match)

        # Create new insight
        insight = Insight(
            insight_type=body.insight_type,
            title=body.title,
            description=body.description,
            project=body.project,
            origin=normalized_origin,
            severity=body.severity,
            confidence=body.confidence,
            tags=body.tags,
            observations=[],
            session_id=body.session_id,
        )
        db.add(insight)
        db.commit()
        db.refresh(insight)

        return _insight_to_response(insight)

    except HTTPException:
        raise
    except Exception:
        db.rollback()
        logger.exception("Failed to create insight")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create insight",
        )


def _list_insights_response(
    db: Session,
    project: Optional[str] = None,
    insight_type: Optional[str] = None,
    since_hours: int = 168,
    limit: int = 20,
    include_system: bool = False,
    include_archived: bool = False,
) -> InsightListResponse:
    """Query insights with shared filtering for browser and machine reads."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)

    query = db.query(Insight).filter(Insight.created_at >= cutoff)
    query = query.filter(
        insight_visibility_clause(
            Insight,
            include_system=include_system,
            include_archived=include_archived,
        )
    )

    if project is not None:
        query = query.filter(Insight.project == project)
    if insight_type is not None:
        query = query.filter(Insight.insight_type == insight_type)

    total = query.count()
    insights = query.order_by(Insight.created_at.desc()).limit(limit).all()

    return InsightListResponse(
        insights=[_insight_to_response(i) for i in insights],
        total=total,
    )


@router.get("", response_model=InsightListResponse)
async def list_insights(
    project: Optional[str] = Query(None, description="Filter by project"),
    insight_type: Optional[str] = Query(None, description="Filter by type"),
    since_hours: int = Query(168, ge=1, le=8760, description="Hours to look back (default 168 = 7 days)"),
    limit: int = Query(20, ge=1, le=100, description="Max results"),
    include_system: bool = Query(False, description="Include system-generated alert rows"),
    include_archived: bool = Query(False, description="Include archived insights"),
    db: Session = Depends(get_db),
    _browser_user=Depends(get_current_browser_user),
    _single: None = Depends(require_single_tenant),
) -> InsightListResponse:
    """Query insights for browser-owned UI reads."""
    try:
        return _list_insights_response(
            db=db,
            project=project,
            insight_type=insight_type,
            since_hours=since_hours,
            limit=limit,
            include_system=include_system,
            include_archived=include_archived,
        )
    except Exception:
        logger.exception("Failed to list insights")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list insights",
        )


@machine_router.get("", response_model=InsightListResponse)
async def list_machine_insights(
    project: Optional[str] = Query(None, description="Filter by project"),
    insight_type: Optional[str] = Query(None, description="Filter by type"),
    since_hours: int = Query(168, ge=1, le=8760, description="Hours to look back (default 168 = 7 days)"),
    limit: int = Query(20, ge=1, le=100, description="Max results"),
    include_system: bool = Query(False, description="Include system-generated alert rows"),
    include_archived: bool = Query(False, description="Include archived insights"),
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> InsightListResponse:
    """Query insights for machine-owned continuity reads."""
    try:
        return _list_insights_response(
            db=db,
            project=project,
            insight_type=insight_type,
            since_hours=since_hours,
            limit=limit,
            include_system=include_system,
            include_archived=include_archived,
        )
    except Exception:
        logger.exception("Failed to list machine insights")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list insights",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insight_to_response(insight: Insight) -> InsightResponse:
    """Convert an Insight ORM object to a response model."""
    return InsightResponse(
        id=str(insight.id),
        insight_type=insight.insight_type,
        title=insight.title,
        description=insight.description,
        project=insight.project,
        origin=insight.origin,
        severity=insight.severity,
        confidence=insight.confidence,
        tags=insight.tags,
        observations=insight.observations,
        session_id=str(insight.session_id) if insight.session_id else None,
        created_at=insight.created_at,
        updated_at=insight.updated_at,
        archived_at=insight.archived_at,
    )


@router.post("/{insight_id}/archive", response_model=InsightResponse)
async def archive_insight(
    insight_id: str,
    db: Session = Depends(get_db),
    _browser_user=Depends(get_current_browser_user),
    _single: None = Depends(require_single_tenant),
) -> InsightResponse:
    """Archive an insight so it stops participating in default continuity reads."""
    try:
        insight = _get_insight_or_404(db, insight_id)
        if insight.archived_at is None:
            insight.archived_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(insight)
        return _insight_to_response(insight)
    except HTTPException:
        raise
    except Exception:
        db.rollback()
        logger.exception("Failed to archive insight %s", insight_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to archive insight",
        )


@router.post("/{insight_id}/unarchive", response_model=InsightResponse)
async def unarchive_insight(
    insight_id: str,
    db: Session = Depends(get_db),
    _browser_user=Depends(get_current_browser_user),
    _single: None = Depends(require_single_tenant),
) -> InsightResponse:
    """Restore an archived insight to the default continuity corpus."""
    try:
        insight = _get_insight_or_404(db, insight_id)
        if insight.archived_at is not None:
            insight.archived_at = None
            db.commit()
            db.refresh(insight)
        return _insight_to_response(insight)
    except HTTPException:
        raise
    except Exception:
        db.rollback()
        logger.exception("Failed to unarchive insight %s", insight_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to unarchive insight",
        )
