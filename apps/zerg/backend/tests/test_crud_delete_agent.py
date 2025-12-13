from zerg.crud import crud
from zerg.models.enums import RunStatus, RunTrigger
from zerg.models.models import Agent, AgentRun, Thread, ThreadMessage, WorkerJob


def test_delete_agent_deletes_dependents_and_nulls_worker_jobs(db_session):
    # Create agent
    agent = crud.create_agent(
        db_session,
        owner_id=1,
        name="Temp Agent",
        system_instructions="system",
        task_instructions="task",
        model="gpt-5-mini",
    )

    # Create a thread + message referencing the agent
    thread = Thread(agent_id=agent.id, title="t", active=False, thread_type="manual")
    db_session.add(thread)
    db_session.commit()
    db_session.refresh(thread)

    msg = ThreadMessage(thread_id=thread.id, role="user", content="hi", processed=False)
    db_session.add(msg)
    db_session.commit()

    # Create a run referencing the agent + thread
    run = AgentRun(
        agent_id=agent.id,
        thread_id=thread.id,
        status=RunStatus.SUCCESS.value,
        trigger=RunTrigger.MANUAL.value,
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    # Create a worker job that references the run (should be preserved, correlation nulled)
    job = WorkerJob(
        owner_id=1,
        supervisor_run_id=run.id,
        task="noop",
        model="gpt-5-mini",
        status="queued",
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)

    ok = crud.delete_agent(db_session, agent.id)
    assert ok is True

    assert db_session.query(Agent).filter(Agent.id == agent.id).count() == 0
    assert db_session.query(AgentRun).filter(AgentRun.id == run.id).count() == 0
    assert db_session.query(Thread).filter(Thread.id == thread.id).count() == 0
    assert db_session.query(ThreadMessage).filter(ThreadMessage.thread_id == thread.id).count() == 0

    refreshed_job = db_session.query(WorkerJob).filter(WorkerJob.id == job.id).one()
    assert refreshed_job.supervisor_run_id is None
