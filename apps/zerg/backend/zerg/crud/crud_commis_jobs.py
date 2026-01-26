"""CRUD operations for CommisJob."""

from sqlalchemy.orm import Session

from zerg.models import CommisJob


def get_by_oikos_run(
    db: Session,
    oikos_run_id: int,
    owner_id: int | None = None,
) -> list[CommisJob]:
    """Get all commis jobs for a oikos run.

    Parameters
    ----------
    db
        Database session
    oikos_run_id
        Oikos run ID to query
    owner_id
        Optional owner ID for security filtering

    Returns
    -------
    list[CommisJob]
        List of commis jobs for this oikos run
    """
    query = db.query(CommisJob).filter(CommisJob.oikos_run_id == oikos_run_id)

    if owner_id is not None:
        query = query.filter(CommisJob.owner_id == owner_id)

    return query.all()


__all__ = ["get_by_oikos_run"]
