"""Runs router – read-only access to Run rows."""

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
from zerg.models.models import Fiche as FicheModel
from zerg.models.models import Run as RunModel

# Schemas
from zerg.schemas.schemas import RunOut

router = APIRouter(
    tags=["runs"],
    dependencies=[Depends(get_current_user)],
)


@router.get("/fiches/{fiche_id}/runs", response_model=List[RunOut])
def list_runs(
    fiche_id: int,
    limit: int = 20,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Return latest *limit* runs for the given fiche (descending)."""

    fiche = crud.get_fiche(db, fiche_id)
    if fiche is None:
        raise HTTPException(status_code=404, detail="Fiche not found")

    # Authorization: only owner or admin may view a fiche's runs
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and fiche.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Forbidden: not fiche owner")

    return crud.list_runs(db, fiche_id, limit=limit)


@router.get("/runs/{run_id}", response_model=RunOut)
def get_run(run_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    row = db.query(RunModel).join(FicheModel, FicheModel.id == RunModel.fiche_id).filter(RunModel.id == run_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found")

    # Authorization: only owner or admin may view a run
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and row.fiche.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Forbidden: not fiche owner")
    return row
