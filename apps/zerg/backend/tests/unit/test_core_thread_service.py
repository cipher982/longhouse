import pytest

from tests.conftest import TEST_WORKER_MODEL
from zerg.core.implementations import SQLAlchemyDatabase
from zerg.core.services import ThreadService
from zerg.core.test_implementations import TestAuthProvider as AuthProviderStub
from zerg.crud import crud


def test_core_thread_service_scopes_threads(db_session):
    owner = crud.create_user(db_session, email="owner-core@local", provider=None, role="USER")
    other = crud.create_user(db_session, email="other-core@local", provider=None, role="USER")

    owner_agent = crud.create_agent(
        db_session,
        owner_id=owner.id,
        name="owner-agent",
        system_instructions="sys",
        task_instructions="task",
        model=TEST_WORKER_MODEL,
        schedule=None,
        config={},
    )
    other_agent = crud.create_agent(
        db_session,
        owner_id=other.id,
        name="other-agent",
        system_instructions="sys",
        task_instructions="task",
        model=TEST_WORKER_MODEL,
        schedule=None,
        config={},
    )

    owner_thread = crud.create_thread(
        db=db_session,
        agent_id=owner_agent.id,
        title="owner-thread",
        active=True,
        agent_state={},
        memory_strategy="buffer",
    )
    other_thread = crud.create_thread(
        db=db_session,
        agent_id=other_agent.id,
        title="other-thread",
        active=True,
        agent_state={},
        memory_strategy="buffer",
    )

    service = ThreadService(SQLAlchemyDatabase(), AuthProviderStub(owner))
    threads = service.get_threads(owner)
    ids = {t.id for t in threads}
    assert owner_thread.id in ids
    assert other_thread.id not in ids


def test_core_thread_service_create_requires_owner(db_session):
    owner = crud.create_user(db_session, email="owner-core-create@local", provider=None, role="USER")
    other = crud.create_user(db_session, email="other-core-create@local", provider=None, role="USER")

    agent = crud.create_agent(
        db_session,
        owner_id=owner.id,
        name="owner-agent",
        system_instructions="sys",
        task_instructions="task",
        model=TEST_WORKER_MODEL,
        schedule=None,
        config={},
    )

    service = ThreadService(SQLAlchemyDatabase(), AuthProviderStub(other))

    with pytest.raises(PermissionError):
        service.create_thread(other, agent.id, "nope")
