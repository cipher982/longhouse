"""Action Proposals API — human-in-the-loop review queue for reflection insights.

Provides endpoints for:
- GET  /api/proposals        — list proposals with status/project filters
- POST /api/proposals/{id}/approve — approve a proposal (creates task description)
- POST /api/proposals/{id}/decline — decline a proposal
"""

import logging
from datetime import datetime
from datetime import timezone
from typing import List
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.models.work import ActionProposal
from zerg.models.work import Insight
from zerg.routers.agents import require_single_tenant
from zerg.routers.agents import verify_agents_read_access
from zerg.routers.agents import verify_agents_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/proposals", tags=["proposals"])


# ---------------------------------------------------------------------------
# Response Models
# ---------------------------------------------------------------------------


class ProposalResponse(BaseModel):
    """Response for a single action proposal."""

    id: str
    insight_id: str
    reflection_run_id: Optional[str] = None
    project: Optional[str] = None
    title: str
    action_blurb: str
    status: str = "pending"
    decided_at: Optional[datetime] = None
    task_description: Optional[str] = None
    created_at: Optional[datetime] = None
    # Denormalized from the parent insight for display
    insight_type: Optional[str] = None
    severity: Optional[str] = None


class ProposalListResponse(BaseModel):
    """Response for proposal list."""

    proposals: List[ProposalResponse]
    total: int


class ProposalActionResponse(BaseModel):
    """Response after approving or declining a proposal."""

    proposal: ProposalResponse
    task_created: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _proposal_to_response(
    proposal: ActionProposal,
    insight: Insight | None = None,
) -> ProposalResponse:
    """Convert an ActionProposal ORM object to a response model."""
    return ProposalResponse(
        id=str(proposal.id),
        insight_id=str(proposal.insight_id),
        reflection_run_id=str(proposal.reflection_run_id) if proposal.reflection_run_id else None,
        project=proposal.project,
        title=proposal.title,
        action_blurb=proposal.action_blurb,
        status=proposal.status,
        decided_at=proposal.decided_at,
        task_description=proposal.task_description,
        created_at=proposal.created_at,
        insight_type=insight.insight_type if insight else None,
        severity=insight.severity if insight else None,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=ProposalListResponse)
async def list_proposals(
    status_filter: Optional[str] = Query(
        "pending",
        alias="status",
        description="Filter by status: pending, approved, declined",
    ),
    project: Optional[str] = Query(None, description="Filter by project"),
    limit: int = Query(20, ge=1, le=100, description="Max results"),
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_read_access),
    _single: None = Depends(require_single_tenant),
) -> ProposalListResponse:
    """List action proposals with filters."""
    try:
        query = db.query(ActionProposal)

        if status_filter:
            query = query.filter(ActionProposal.status == status_filter)
        if project:
            query = query.filter(ActionProposal.project == project)

        total = query.count()
        proposals = query.order_by(ActionProposal.created_at.desc()).limit(limit).all()

        # Batch-fetch related insights for display fields
        insight_ids = [p.insight_id for p in proposals]
        insights_map: dict = {}
        if insight_ids:
            insights = db.query(Insight).filter(Insight.id.in_(insight_ids)).all()
            insights_map = {str(i.id): i for i in insights}

        return ProposalListResponse(
            proposals=[_proposal_to_response(p, insights_map.get(str(p.insight_id))) for p in proposals],
            total=total,
        )

    except Exception:
        logger.exception("Failed to list proposals")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list proposals",
        )


@router.post("/{proposal_id}/approve", response_model=ProposalActionResponse)
async def approve_proposal(
    proposal_id: str,
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> ProposalActionResponse:
    """Approve a proposal — sets status to approved and generates a task description."""
    try:
        proposal = db.query(ActionProposal).filter(ActionProposal.id == proposal_id).first()
        if not proposal:
            raise HTTPException(status_code=404, detail="Proposal not found")

        # Fetch parent insight for richer task context
        insight = db.query(Insight).filter(Insight.id == proposal.insight_id).first()

        proposal.status = "approved"
        proposal.decided_at = datetime.now(timezone.utc)

        # Build task description from insight context + action blurb
        parts = [proposal.action_blurb]
        if insight and insight.description:
            parts.append(f"\nContext: {insight.description}")
        if proposal.project:
            parts.append(f"\nProject: {proposal.project}")
        proposal.task_description = "\n".join(parts)

        db.commit()
        db.refresh(proposal)

        return ProposalActionResponse(
            proposal=_proposal_to_response(proposal, insight),
            task_created=True,
        )

    except HTTPException:
        raise
    except Exception:
        db.rollback()
        logger.exception("Failed to approve proposal %s", proposal_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to approve proposal",
        )


@router.post("/{proposal_id}/decline", response_model=ProposalActionResponse)
async def decline_proposal(
    proposal_id: str,
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> ProposalActionResponse:
    """Decline a proposal — sets status to declined."""
    try:
        proposal = db.query(ActionProposal).filter(ActionProposal.id == proposal_id).first()
        if not proposal:
            raise HTTPException(status_code=404, detail="Proposal not found")

        insight = db.query(Insight).filter(Insight.id == proposal.insight_id).first()

        proposal.status = "declined"
        proposal.decided_at = datetime.now(timezone.utc)

        db.commit()
        db.refresh(proposal)

        return ProposalActionResponse(
            proposal=_proposal_to_response(proposal, insight),
            task_created=False,
        )

    except HTTPException:
        raise
    except Exception:
        db.rollback()
        logger.exception("Failed to decline proposal %s", proposal_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to decline proposal",
        )
