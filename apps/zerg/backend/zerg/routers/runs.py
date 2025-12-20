"""Runs router – read-only access to AgentRun rows."""

from __future__ import annotations

from typing import List

# FastAPI helpers
from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from sqlalchemy.orm import Session

from zerg.crud import crud
from zerg.database import get_db

# Auth dependency
from zerg.dependencies.auth import get_current_user
from zerg.models.models import Agent as AgentModel
from zerg.models.models import AgentRun as AgentRunModel

# Schemas
from zerg.schemas.schemas import AgentRunOut

router = APIRouter(
    tags=["runs"],
    dependencies=[Depends(get_current_user)],
)


@router.get("/agents/{agent_id}/runs", response_model=List[AgentRunOut])
def list_agent_runs(
    agent_id: int,
    limit: int = 20,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Return latest *limit* runs for the given agent (descending)."""

    agent = crud.get_agent(db, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Authorization: only owner or admin may view an agent's runs
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and agent.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Forbidden: not agent owner")

    return crud.list_runs(db, agent_id, limit=limit)


@router.get("/runs/{run_id}", response_model=AgentRunOut)
def get_run(run_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    row = db.query(AgentRunModel).join(AgentModel, AgentModel.id == AgentRunModel.agent_id).filter(AgentRunModel.id == run_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found")

    # Authorization: only owner or admin may view a run
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and row.agent.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Forbidden: not agent owner")
    return row
