"""File reservations API for multi-agent workflows.

Provides endpoints for:
- POST /api/reservations — reserve a file (clean expired first, 409 on conflict)
- GET /api/reservations/check — check if a file is reserved
- DELETE /api/reservations/{id} — release a reservation

Authentication uses the same agents token pattern as the agents router.
"""

import logging
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.models.work import FileReservation
from zerg.routers.agents import require_single_tenant
from zerg.routers.agents import verify_agents_read_access
from zerg.routers.agents import verify_agents_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reservations", tags=["reservations"])


# ---------------------------------------------------------------------------
# Request / Response Models
# ---------------------------------------------------------------------------


class ReservationCreateRequest(BaseModel):
    """Request body for creating a file reservation."""

    file_path: str = Field(..., description="Path to the file to reserve")
    project: Optional[str] = Field(None, description="Project context")
    agent: str = Field("claude", description="Agent name")
    reason: Optional[str] = Field(None, description="Why the file is being reserved")
    duration_minutes: int = Field(60, ge=1, le=1440, description="Reservation duration in minutes")


class ReservationResponse(BaseModel):
    """Response for a single reservation."""

    id: str = Field(..., description="Reservation UUID")
    file_path: str
    project: str
    agent: str
    reason: Optional[str] = None
    expires_at: datetime
    released_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class ReservationCheckResponse(BaseModel):
    """Response for reservation check."""

    reserved: bool
    reservation: Optional[ReservationResponse] = None


class ReservationReleaseResponse(BaseModel):
    """Response for reservation release."""

    released: bool
    id: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", response_model=ReservationResponse, status_code=status.HTTP_201_CREATED)
async def create_reservation(
    body: ReservationCreateRequest,
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> ReservationResponse:
    """Reserve a file to prevent edit conflicts.

    Cleans expired reservations first, then checks for existing active
    reservations on the same file+project. Returns 409 if already reserved.
    """
    try:
        now = datetime.now(timezone.utc)
        project = body.project or ""

        # Opportunistic cleanup: expire all overdue reservations
        expired = (
            db.query(FileReservation)
            .filter(
                FileReservation.released_at.is_(None),
                FileReservation.expires_at < now,
            )
            .all()
        )
        for res in expired:
            res.released_at = now
        if expired:
            db.flush()

        # Check for existing active reservation (query-then-insert for SQLite compatibility)
        existing = (
            db.query(FileReservation)
            .filter(
                FileReservation.file_path == body.file_path,
                FileReservation.project == project,
                FileReservation.released_at.is_(None),
            )
            .first()
        )

        if existing:
            db.commit()  # Commit the cleanup
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": f"File already reserved by {existing.agent}",
                    "reservation": _reservation_to_dict(existing),
                },
            )

        # Create new reservation
        expires_at = now + timedelta(minutes=body.duration_minutes)
        reservation = FileReservation(
            file_path=body.file_path,
            project=project,
            agent=body.agent,
            reason=body.reason,
            expires_at=expires_at,
        )
        db.add(reservation)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"File already reserved (concurrent request): {body.file_path}",
            )
        db.refresh(reservation)

        return _reservation_to_response(reservation)

    except HTTPException:
        raise
    except Exception:
        db.rollback()
        logger.exception("Failed to create reservation")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create reservation",
        )


@router.get("/check", response_model=ReservationCheckResponse)
async def check_reservation(
    file_path: str = Query(..., description="File path to check"),
    project: Optional[str] = Query(None, description="Project context"),
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_read_access),
    _single: None = Depends(require_single_tenant),
) -> ReservationCheckResponse:
    """Check if a file is currently reserved."""
    try:
        now = datetime.now(timezone.utc)
        proj = project or ""

        # Find active, non-expired reservation
        reservation = (
            db.query(FileReservation)
            .filter(
                FileReservation.file_path == file_path,
                FileReservation.project == proj,
                FileReservation.released_at.is_(None),
                FileReservation.expires_at >= now,
            )
            .first()
        )

        if reservation:
            return ReservationCheckResponse(
                reserved=True,
                reservation=_reservation_to_response(reservation),
            )

        return ReservationCheckResponse(reserved=False)

    except Exception:
        logger.exception("Failed to check reservation")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to check reservation",
        )


@router.delete("/{reservation_id}", response_model=ReservationReleaseResponse)
async def release_reservation(
    reservation_id: UUID,
    db: Session = Depends(get_db),
    _auth: None = Depends(verify_agents_token),
    _single: None = Depends(require_single_tenant),
) -> ReservationReleaseResponse:
    """Release a file reservation."""
    try:
        reservation = db.query(FileReservation).filter(FileReservation.id == reservation_id).first()

        if not reservation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Reservation {reservation_id} not found",
            )

        if reservation.released_at is not None:
            return ReservationReleaseResponse(released=True, id=str(reservation_id))

        reservation.released_at = datetime.now(timezone.utc)
        db.commit()

        return ReservationReleaseResponse(released=True, id=str(reservation_id))

    except HTTPException:
        raise
    except Exception:
        db.rollback()
        logger.exception("Failed to release reservation")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to release reservation",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reservation_to_response(reservation: FileReservation) -> ReservationResponse:
    """Convert a FileReservation ORM object to a response model."""
    return ReservationResponse(
        id=str(reservation.id),
        file_path=reservation.file_path,
        project=reservation.project,
        agent=reservation.agent,
        reason=reservation.reason,
        expires_at=reservation.expires_at,
        released_at=reservation.released_at,
        created_at=reservation.created_at,
    )


def _reservation_to_dict(reservation: FileReservation) -> dict:
    """Convert a FileReservation to a dict for error detail."""
    return {
        "id": str(reservation.id),
        "file_path": reservation.file_path,
        "project": reservation.project,
        "agent": reservation.agent,
        "reason": reservation.reason,
        "expires_at": reservation.expires_at.isoformat() if reservation.expires_at else None,
    }
