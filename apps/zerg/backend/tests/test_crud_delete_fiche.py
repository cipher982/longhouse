from zerg.crud import crud
from zerg.models.enums import CourseStatus
from zerg.models.enums import CourseTrigger
from zerg.models.models import Fiche
from zerg.models.models import Course
from zerg.models.models import Thread
from zerg.models.models import ThreadMessage
from zerg.models.models import CommisJob


def test_delete_fiche_deletes_dependents_and_nulls_commis_jobs(db_session):
    # Create fiche
    fiche = crud.create_fiche(
        db_session,
        owner_id=1,
        name="Temp Fiche",
        system_instructions="system",
        task_instructions="task",
        model="gpt-5-mini",
    )

    # Create a thread + message referencing the fiche
    thread = Thread(fiche_id=fiche.id, title="t", active=False, thread_type="manual")
    db_session.add(thread)
    db_session.commit()
    db_session.refresh(thread)

    msg = ThreadMessage(thread_id=thread.id, role="user", content="hi", processed=False)
    db_session.add(msg)
    db_session.commit()

    # Create a run referencing the fiche + thread
    run = Course(
        fiche_id=fiche.id,
        thread_id=thread.id,
        status=CourseStatus.SUCCESS.value,
        trigger=CourseTrigger.MANUAL.value,
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    # Create a commis job that references the run (should be preserved, correlation nulled)
    job = CommisJob(
        owner_id=1,
        concierge_course_id=run.id,
        task="noop",
        model="gpt-5-mini",
        status="queued",
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    ok = crud.delete_fiche(db_session, fiche.id)
    assert ok is True

    assert db_session.query(Fiche).filter(Fiche.id == fiche.id).count() == 0
    assert db_session.query(Course).filter(Course.id == run.id).count() == 0
    assert db_session.query(Thread).filter(Thread.id == thread.id).count() == 0
    assert db_session.query(ThreadMessage).filter(ThreadMessage.thread_id == thread.id).count() == 0

    refreshed_job = db_session.query(CommisJob).filter(CommisJob.id == job.id).one()
    assert refreshed_job.concierge_course_id is None
