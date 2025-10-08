"""Graph layout & canvas persistence endpoints.

This is a **placeholder** router that will be expanded in a later phase.
For now it merely accepts the batched layout payload sent by the frontend
every few hundred milliseconds once the user stops dragging/zooming the
canvas and responds with HTTP 204 (no-content).

Keeping the stub in place unblocks the frontend work while we design the
proper persistence model (likely a dedicated `canvas_layouts` table).
"""

from __future__ import annotations

from typing import Dict
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Response
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator
from sqlalchemy.orm import Session

from zerg.crud import crud
from zerg.database import get_db
from zerg.dependencies.auth import get_current_user

router = APIRouter(prefix="/graph", tags=["canvas"])


class NodePos(BaseModel):
    x: float = Field(..., ge=-10_000.0, le=10_000.0)
    y: float = Field(..., ge=-10_000.0, le=10_000.0)


class Viewport(BaseModel):
    x: float
    y: float
    zoom: float = Field(..., ge=0.1, le=10.0)


class LayoutUpdate(BaseModel):
    nodes: Dict[str, NodePos]
    viewport: Optional[Viewport] = None

    @field_validator("nodes")
    def check_node_count(cls, v):  # noqa: N805 – Pydantic naming rule
        if len(v) > 5_000:
            raise ValueError("payload too large – max 5000 nodes")
        return v


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.patch("/layout", status_code=status.HTTP_204_NO_CONTENT)
async def patch_layout(
    payload: LayoutUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    workflow_id: Optional[int] = None,
) -> Response:
    """Upsert the authenticated user's canvas layout."""

    # Convert Pydantic models → plain dicts for JSON storage.
    nodes_dict = {k: v.model_dump() for k, v in payload.nodes.items()}
    viewport_dict = payload.viewport.model_dump() if payload.viewport is not None else None

    crud.upsert_canvas_layout(
        db,
        getattr(current_user, "id", None),
        nodes_dict,
        viewport_dict,
        workflow_id,
    )

    # Consider broadcasting update over WebSocket in a future enhancement.
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/layout")
async def get_layout(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    workflow_id: Optional[int] = None,
):
    """Return the stored layout for the authenticated user (if any)."""

    layout = crud.get_canvas_layout(db, getattr(current_user, "id", None), workflow_id)
    if layout is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return {"nodes": layout.nodes_json, "viewport": layout.viewport}
