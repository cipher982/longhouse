"""Insights API for agent infrastructure — track learnings across sessions.

Provides endpoints for:
- POST /api/insights — create or deduplicate insight (same title+project within 7 days → update)
- GET /api/insights — query insights with filters

Authentication uses the same agents token pattern as the agents router.
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
from zerg.models.work import INSIGHT_DEDUP_WINDOW_DAYS
from zerg.models.work import Insight
from zerg.routers.agents import require_single_tenant
from zerg.routers.agents import verify_agents_read_access
from zerg.routers.agents import verify_agents_token
from zerg.utils.time import UTCBaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/insights", tags=["insights"])


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


class InsightResponse(UTCBaseModel):
    """Response for a single insight."""

    id: str = Field(..., description="Insight UUID")
    insight_type: str
    title: str
    description: Optional[str] = None
    project: Optional[str] = None
    severity: str = "info"
    confidence: Optional[float] = None
    tags: Optional[List[str]] = None
    observations: Optional[List[str]] = None
    session_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class InsightListResponse(BaseModel):
    """Response for insight list."""

    insights: List[InsightResponse]
    total: int


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

        # Dedup: look for existing insight with same title + project
        query = db.query(Insight).filter(
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


@router.get("", response_model=InsightListResponse)
async def list_insights(
    project: Optional[str] = Query(None, description="Filter by project"),
    insight_type: Optional[str] = Query(None, description="Filter by type"),
    since_hours: int = Query(168, ge=1, le=8760, description="Hours to look back (default 168 = 7 days)"),
    limit: int = Query(20, ge=1, le=100, description="Max results"),
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_read_access),
    _single: None = Depends(require_single_tenant),
) -> InsightListResponse:
    """Query insights with filters."""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)

        query = db.query(Insight).filter(Insight.created_at >= cutoff)

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

    except Exception:
        logger.exception("Failed to list insights")
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
        severity=insight.severity,
        confidence=insight.confidence,
        tags=insight.tags,
        observations=insight.observations,
        session_id=str(insight.session_id) if insight.session_id else None,
        created_at=insight.created_at,
        updated_at=insight.updated_at,
    )
