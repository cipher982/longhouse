"""Conversation sync API router.

Provides endpoints for Jarvis clients to push and pull conversation sync
operations. Implements idempotent push and cursor-based pull for offline-first
conversation synchronization.
"""

import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from zerg.database import get_db
from zerg.models.models import User
from zerg.models.sync import SyncOperation

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/jarvis/sync", tags=["sync"])


# ---------------------------------------------------------------------------
# Authentication - Import from jarvis router
# ---------------------------------------------------------------------------


# Import the actual dependency function from jarvis router
from zerg.routers.jarvis import get_current_jarvis_user


# ---------------------------------------------------------------------------
# Request/Response Models
# ---------------------------------------------------------------------------


class SyncOp(BaseModel):
    """A single sync operation."""

    opId: str = Field(..., description="Client-generated unique operation ID")
    type: str = Field(..., description="Operation type (e.g., message, conversation)")
    body: dict = Field(..., description="Operation payload")
    lamport: int = Field(..., description="Lamport timestamp for ordering")
    ts: str = Field(..., description="Client timestamp (ISO format)")


class PushRequest(BaseModel):
    """Push sync operations to server."""

    deviceId: str = Field(..., description="Device identifier")
    cursor: int = Field(..., description="Client's current cursor position")
    ops: List[SyncOp] = Field(..., description="Operations to push")


class PushResponse(BaseModel):
    """Response from push operation."""

    acked: List[str] = Field(..., description="List of acknowledged operation IDs")
    nextCursor: int = Field(..., description="Updated cursor position")


class PullResponse(BaseModel):
    """Response from pull operation."""

    ops: List[dict] = Field(..., description="Operations since cursor")
    nextCursor: int = Field(..., description="Updated cursor position")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/push", response_model=PushResponse)
def push_sync_operations(
    request: PushRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_jarvis_user),
) -> PushResponse:
    """Push sync operations from client.

    Implements idempotent push semantics - operations with duplicate opId
    values are acknowledged without error. This allows clients to safely
    retry without creating duplicates.

    Args:
        request: Push request with operations
        db: Database session
        current_user: Authenticated user

    Returns:
        PushResponse with acknowledged operation IDs and next cursor
    """
    acked = []

    for op in request.ops:
        try:
            # Parse timestamp from ISO string
            try:
                ts = datetime.fromisoformat(op.ts.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                # Fallback to current time if timestamp invalid
                ts = datetime.utcnow()

            # Create sync operation
            sync_op = SyncOperation(
                op_id=op.opId,
                user_id=current_user.id,
                type=op.type,
                body=op.body,
                lamport=op.lamport,
                ts=ts,
            )
            db.add(sync_op)
            db.flush()  # Flush to catch integrity errors

            acked.append(op.opId)

        except IntegrityError:
            # Duplicate op_id - this is expected for retries, just acknowledge it
            db.rollback()
            acked.append(op.opId)
            logger.debug(f"Duplicate op_id {op.opId} from user {current_user.id}, acknowledging")

        except Exception as e:
            # Unexpected error - log and continue with next operation
            db.rollback()
            logger.error(f"Failed to store sync operation {op.opId}: {e}")
            # Don't add to acked list - client will retry

    # Commit all successful operations
    try:
        db.commit()
    except Exception as e:
        logger.error(f"Failed to commit sync operations: {e}")
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to persist sync operations",
        )

    # Calculate next cursor (total operations for this user)
    next_cursor = db.query(SyncOperation).filter(SyncOperation.user_id == current_user.id).count()

    return PushResponse(acked=acked, nextCursor=next_cursor)


@router.get("/pull", response_model=PullResponse)
def pull_sync_operations(
    cursor: int = Query(0, description="Cursor position to pull from"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_jarvis_user),
) -> PullResponse:
    """Pull sync operations from server.

    Returns all operations for the current user created after the given
    cursor position. Cursor is a simple offset - clients should store the
    nextCursor value and use it for the next pull request.

    Args:
        cursor: Starting cursor position (offset)
        db: Database session
        current_user: Authenticated user

    Returns:
        PullResponse with operations and next cursor
    """
    if cursor < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cursor must be non-negative",
        )

    # Query operations for this user after cursor
    operations = (
        db.query(SyncOperation)
        .filter(SyncOperation.user_id == current_user.id)
        .order_by(SyncOperation.id)
        .offset(cursor)
        .all()
    )

    # Format operations for response
    ops = []
    for op in operations:
        ops.append(
            {
                "opId": op.op_id,
                "type": op.type,
                "body": op.body,
                "lamport": op.lamport,
                "ts": op.ts.isoformat(),
            }
        )

    # Calculate next cursor
    next_cursor = (
        db.query(SyncOperation)
        .filter(SyncOperation.user_id == current_user.id)
        .count()
    )

    return PullResponse(ops=ops, nextCursor=next_cursor)
