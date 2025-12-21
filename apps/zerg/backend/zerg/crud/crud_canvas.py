"""CRUD operations for Canvas layouts."""

from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from zerg.models import CanvasLayout


def upsert_canvas_layout(
    db: Session,
    user_id: Optional[int],
    nodes: dict,
    viewport: Optional[dict],
    workflow_id: Optional[int] = None,
):
    """Insert **or** update the *canvas layout* for *(user_id, workflow_id)*.

    Uses database-agnostic upsert logic that works with both SQLite and PostgreSQL.
    Relies on the UNIQUE(user_id, workflow_id) constraint declared on the CanvasLayout model.
    """

    if user_id is None:
        raise ValueError("upsert_canvas_layout: `user_id` must not be None, auth dependency failed?")

    # First, try to find an existing record
    existing = db.query(CanvasLayout).filter(CanvasLayout.user_id == user_id, CanvasLayout.workflow_id == workflow_id).first()

    if existing:
        # Update existing record
        existing.nodes_json = nodes
        existing.viewport = viewport
        existing.updated_at = func.now()
    else:
        # Create new record
        new_layout = CanvasLayout(
            user_id=user_id,
            workflow_id=workflow_id,
            nodes_json=nodes,
            viewport=viewport,
        )
        db.add(new_layout)

    db.commit()

    # Return the *current* row so callers can inspect the stored payload.
    return db.query(CanvasLayout).filter_by(user_id=user_id, workflow_id=workflow_id).first()


def get_canvas_layout(db: Session, user_id: Optional[int], workflow_id: Optional[int] = None):
    """Return the persisted canvas layout for *(user_id, workflow_id)*."""

    if user_id is None:
        return None

    return db.query(CanvasLayout).filter_by(user_id=user_id, workflow_id=workflow_id).first()
