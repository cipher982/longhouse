import contextlib

import pytest

from tests.conftest import TEST_COMMIS_MODEL
from zerg.crud import crud
from zerg.main import app


class _UsageStub:
    def __init__(self, *args, **kwargs):
        pass

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        from langchain_core.messages import AIMessage

        # Simulate OpenAI usage metadata via LangChain AIMessage.usage_metadata
        # In langchain-core 1.x, usage is in usage_metadata, not response_metadata
        usage = {
            "input_tokens": 8,
            "output_tokens": 9,
            "total_tokens": 17,
        }
        return AIMessage(content="ok", usage_metadata=usage)

    async def ainvoke(self, messages, **kwargs):
        from langchain_core.messages import AIMessage

        # Simulate OpenAI usage metadata via LangChain AIMessage.usage_metadata
        # In langchain-core 1.x, usage is in usage_metadata, not response_metadata
        usage = {
            "input_tokens": 8,
            "output_tokens": 9,
            "total_tokens": 17,
        }
        return AIMessage(content="ok", usage_metadata=usage)


@pytest.mark.asyncio
async def test_usage_totals_persist_with_metadata(client, db_session, monkeypatch):
    # Ensure allowed model
    monkeypatch.setenv("ALLOWED_MODELS_NON_ADMIN", TEST_COMMIS_MODEL)
    # Patch ChatOpenAI used by concierge_react_engine to our usage stub
    import zerg.services.concierge_react_engine as sre

    monkeypatch.setattr(sre, "ChatOpenAI", _UsageStub)

    # Create a user and fiche/thread
    user = crud.get_user_by_email(db_session, "u@local") or crud.create_user(
        db_session, email="u@local", provider=None, role="USER"
    )
    fiche = crud.create_fiche(
        db_session,
        owner_id=user.id,
        name="a",
        system_instructions="sys",
        task_instructions="task",
        model=TEST_COMMIS_MODEL,
        schedule=None,
        config={},
    )
    thread = crud.create_thread(
        db=db_session, fiche_id=fiche.id, title="t", active=True, fiche_state={}, memory_strategy="buffer"
    )
    crud.create_thread_message(db=db_session, thread_id=thread.id, role="user", content="hi")

    from zerg.dependencies.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: user
    try:
        resp = client.post(f"/api/threads/{thread.id}/run")
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]

    assert resp.status_code == 202, resp.text

    # Verify latest run has total_tokens set (cost may be None if pricing map empty)
    runs = crud.list_courses(db_session, fiche.id, limit=1)
    assert runs and runs[0].total_tokens == 17
    # Cost left None unless pricing added
    assert runs[0].total_cost_usd is None


class _NoUsageStub:
    def __init__(self, *args, **kwargs):
        pass

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        from langchain_core.messages import AIMessage

        return AIMessage(content="ok")

    async def ainvoke(self, messages, **kwargs):
        from langchain_core.messages import AIMessage

        return AIMessage(content="ok")


@pytest.mark.asyncio
async def test_usage_missing_leaves_totals_null(client, db_session, monkeypatch):
    # Patch ChatOpenAI used by concierge_react_engine to our no-usage stub
    import zerg.services.concierge_react_engine as sre

    monkeypatch.setattr(sre, "ChatOpenAI", _NoUsageStub)

    user = crud.get_user_by_email(db_session, "u2@local") or crud.create_user(
        db_session, email="u2@local", provider=None, role="USER"
    )
    fiche = crud.create_fiche(
        db_session,
        owner_id=user.id,
        name="a2",
        system_instructions="sys",
        task_instructions="task",
        model=TEST_COMMIS_MODEL,
        schedule=None,
        config={},
    )
    thread = crud.create_thread(
        db=db_session, fiche_id=fiche.id, title="t2", active=True, fiche_state={}, memory_strategy="buffer"
    )
    crud.create_thread_message(db=db_session, thread_id=thread.id, role="user", content="hi")

    from zerg.dependencies.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: user
    try:
        resp = client.post(f"/api/threads/{thread.id}/run")
    finally:
        with contextlib.suppress(Exception):
            del app.dependency_overrides[get_current_user]

    assert resp.status_code == 202, resp.text
    runs = crud.list_courses(db_session, fiche.id, limit=1)
    assert runs and runs[0].total_tokens is None and runs[0].total_cost_usd is None
