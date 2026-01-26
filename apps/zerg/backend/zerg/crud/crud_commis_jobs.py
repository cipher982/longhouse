"""CRUD operations for CommisJob."""

from sqlalchemy.orm import Session

from zerg.models import CommisJob


def get_by_concierge_course(
    db: Session,
    concierge_course_id: int,
    owner_id: int | None = None,
) -> list[CommisJob]:
    """Get all commis jobs for a concierge course.

    Parameters
    ----------
    db
        Database session
    concierge_course_id
        Concierge course ID to query
    owner_id
        Optional owner ID for security filtering

    Returns
    -------
    list[CommisJob]
        List of commis jobs for this concierge course
    """
    query = db.query(CommisJob).filter(CommisJob.concierge_course_id == concierge_course_id)

    if owner_id is not None:
        query = query.filter(CommisJob.owner_id == owner_id)

    return query.all()


__all__ = ["get_by_concierge_course"]
