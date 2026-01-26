"""Courses router – read-only access to Course rows."""

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
from zerg.models.models import Course as CourseModel
from zerg.models.models import Fiche as FicheModel

# Schemas
from zerg.schemas.schemas import CourseOut

router = APIRouter(
    tags=["courses"],
    dependencies=[Depends(get_current_user)],
)


@router.get("/fiches/{fiche_id}/courses", response_model=List[CourseOut])
def list_courses(
    fiche_id: int,
    limit: int = 20,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Return latest *limit* courses for the given fiche (descending)."""

    fiche = crud.get_fiche(db, fiche_id)
    if fiche is None:
        raise HTTPException(status_code=404, detail="Fiche not found")

    # Authorization: only owner or admin may view a fiche's courses
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and fiche.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Forbidden: not fiche owner")

    return crud.list_courses(db, fiche_id, limit=limit)


@router.get("/courses/{course_id}", response_model=CourseOut)
def get_course(course_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    row = db.query(CourseModel).join(FicheModel, FicheModel.id == CourseModel.fiche_id).filter(CourseModel.id == course_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Course not found")

    # Authorization: only owner or admin may view a course
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and row.fiche.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Forbidden: not fiche owner")
    return row
