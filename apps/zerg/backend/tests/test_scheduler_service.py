import logging

import pytest

from apscheduler.triggers.cron import CronTrigger
from zerg.services.scheduler_service import SchedulerService


@pytest.fixture(autouse=True)
def patch_logging(monkeypatch):
    """
    Patch the scheduler service logger for cleaner test output.
    """
    # Silence logging
    monkeypatch.setattr(
        "zerg.services.scheduler_service.logger",
        logging.getLogger("test_scheduler_service"),
    )
    yield


@pytest.fixture
def service(test_session_factory, monkeypatch):
    """
    Create and return a SchedulerService instance for testing.

    The service is configured to use a test scheduler that does not actually
    run jobs but allows verifying that jobs would be scheduled correctly.
    """
    # Mock the event_bus subscription to avoid errors during tests
    # Using pytest's monkeypatch fixture ensures proper cleanup after test
    monkeypatch.setattr(
        "zerg.services.scheduler_service.event_bus.subscribe",
        lambda event_type, handler: None,
    )
    service = SchedulerService(session_factory=test_session_factory)
    yield service
    # Ensure scheduler is properly shut down
    if service._initialized:
        service.scheduler.shutdown()


@pytest.mark.asyncio
async def test_schedule_fiche(service):
    # Schedule a fiche
    fiche_id = 42
    cron_expression = "*/5 * * * *"
    await service.schedule_fiche(fiche_id, cron_expression)

    # Verify the job was added
    job = service.scheduler.get_job(f"fiche_{fiche_id}")
    assert job is not None
    assert isinstance(job.trigger, CronTrigger)
    assert job.args == (fiche_id,)


@pytest.mark.asyncio
async def test_load_scheduled_fiches(service, db_session):
    # Insert two fiches: one with a cron schedule, one without
    # Reuse the dev user as owner
    from zerg.crud import crud as _crud
    from zerg.models.models import Fiche

    owner = _crud.get_user_by_email(db_session, "dev@local") or _crud.create_user(
        db_session, email="dev@local", provider=None, role="ADMIN"
    )

    a1 = Fiche(
        owner_id=owner.id,
        name="A1",
        system_instructions="si",
        task_instructions="ti",
        model="m1",
        status="idle",
        schedule="*/5 * * * *",
    )
    a2 = Fiche(
        owner_id=owner.id,
        name="A2",
        system_instructions="si",
        task_instructions="ti",
        model="m1",
        status="idle",
        schedule=None,
    )
    db_session.add_all([a1, a2])
    db_session.commit()
    fiche_id = a1.id
    db_session.close()

    # No jobs initially
    assert not service.scheduler.get_jobs()

    # Load scheduled fiches
    await service.load_scheduled_fiches()

    jobs = service.scheduler.get_jobs()
    assert len(jobs) == 1
    job = service.scheduler.get_job(f"fiche_{fiche_id}")
    assert job is not None


@pytest.mark.asyncio
async def test_remove_fiche_job(service):
    # First schedule an fiche
    fiche_id = 42
    await service.schedule_fiche(fiche_id, "*/5 * * * *")

    # Verify it was scheduled
    assert service.scheduler.get_job(f"fiche_{fiche_id}") is not None

    # Now remove the job
    service.remove_fiche_job(fiche_id)

    # Verify it was removed
    assert service.scheduler.get_job(f"fiche_{fiche_id}") is None


@pytest.mark.asyncio
async def test_handle_fiche_created(service):
    """Test that an fiche creation event schedules the fiche if needed."""
    # Create event data
    event_data = {
        "id": 1,
        "name": "Test Fiche",
        "schedule": "*/5 * * * *",
    }

    # Process the event
    await service._handle_fiche_created(event_data)

    # Verify the fiche was scheduled
    job = service.scheduler.get_job(f"fiche_{event_data['id']}")
    assert job is not None
    assert job.args == (event_data["id"],)


@pytest.mark.asyncio
async def test_handle_fiche_updated_enabled(service):
    """Test that an fiche update event updates its schedule when enabled."""
    # First schedule the fiche
    fiche_id = 2
    await service.schedule_fiche(fiche_id, "*/10 * * * *")

    # Update with a new schedule
    event_data = {
        "id": fiche_id,
        "schedule": "*/5 * * * *",
    }

    # Process the update event
    await service._handle_fiche_updated(event_data)

    # Verify the schedule was updated
    job = service.scheduler.get_job(f"fiche_{fiche_id}")
    assert job is not None
    # Check that the job uses the new schedule
    # We can't easily check the cron expression directly but can verify
    # the job still exists with the same ID


@pytest.mark.asyncio
async def test_handle_fiche_updated_disabled(service):
    """Test that an fiche update event removes the schedule when disabled."""
    # First schedule the fiche
    fiche_id = 3
    await service.schedule_fiche(fiche_id, "*/10 * * * *")

    # Verify the fiche is scheduled
    assert service.scheduler.get_job(f"fiche_{fiche_id}") is not None

    # Update to disable scheduling
    event_data = {
        "id": fiche_id,
        # schedule key omitted to unset scheduling
    }

    # Process the update event
    await service._handle_fiche_updated(event_data)

    # Verify the schedule was removed
    assert service.scheduler.get_job(f"fiche_{fiche_id}") is None


@pytest.mark.asyncio
async def test_handle_fiche_deleted(service):
    """Test that an fiche deletion event removes any scheduled jobs."""
    # First schedule the fiche
    fiche_id = 4
    await service.schedule_fiche(fiche_id, "*/10 * * * *")

    # Verify the fiche is scheduled
    assert service.scheduler.get_job(f"fiche_{fiche_id}") is not None

    # Delete the fiche
    event_data = {
        "id": fiche_id,
        "name": "Deleted Fiche",
    }

    # Process the delete event
    await service._handle_fiche_deleted(event_data)

    # Verify the schedule was removed
    assert service.scheduler.get_job(f"fiche_{fiche_id}") is None
