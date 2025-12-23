"""CRUD operations for WorkerJob."""

from sqlalchemy.orm import Session

from zerg.models import WorkerJob


def get_by_supervisor_run(
    db: Session,
    supervisor_run_id: int,
    owner_id: int | None = None,
) -> list[WorkerJob]:
    """Get all worker jobs for a supervisor run.

    Parameters
    ----------
    db
        Database session
    supervisor_run_id
        Supervisor run ID to query
    owner_id
        Optional owner ID for security filtering

    Returns
    -------
    list[WorkerJob]
        List of worker jobs for this supervisor run
    """
    query = db.query(WorkerJob).filter(WorkerJob.supervisor_run_id == supervisor_run_id)

    if owner_id is not None:
        query = query.filter(WorkerJob.owner_id == owner_id)

    return query.all()


__all__ = ["get_by_supervisor_run"]
