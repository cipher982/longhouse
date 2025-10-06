import contextlib

import pytest

from zerg.crud import crud
from zerg.main import app


class _UsageStub:
    def __init__(self, *args, **kwargs):
        pass

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        from langchain_core.messages import AIMessage

        # Simulate OpenAI usage metadata via LangChain AIMessage.response_metadata
        meta = {
            "token_usage": {
                "prompt_tokens": 8,
                "completion_tokens": 9,
                "total_tokens": 17,
            }
        }
        return AIMessage(content="ok", response_metadata=meta)


@pytest.mark.asyncio
async def test_usage_totals_persist_with_metadata(client, db_session, monkeypatch):
    # Ensure allowed model
    monkeypatch.setenv("ALLOWED_MODELS_NON_ADMIN", "gpt-4o-mini")
    # Clear AgentRunner runnable cache and patch ChatOpenAI used by agent definition to our usage stub
    import zerg.agents_def.zerg_react_agent as zr
    import zerg.managers.agent_runner as ar

    ar._RUNNABLE_CACHE.clear()
    monkeypatch.setattr(zr, "ChatOpenAI", _UsageStub)

    # Create a user and agent/thread
    user = crud.get_user_by_email(db_session, "u@local") or crud.create_user(
        db_session, email="u@local", provider=None, role="USER"
    )
    agent = crud.create_agent(
        db_session,
        owner_id=user.id,
        name="a",
        system_instructions="sys",
        task_instructions="task",
        model="gpt-4o-mini",
        schedule=None,
        config={},
    )
    thread = crud.create_thread(
        db=db_session, agent_id=agent.id, title="t", active=True, agent_state={}, memory_strategy="buffer"
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
    runs = crud.list_runs(db_session, agent.id, limit=1)
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


@pytest.mark.asyncio
async def test_usage_missing_leaves_totals_null(client, db_session, monkeypatch):
    import zerg.agents_def.zerg_react_agent as zr
    import zerg.managers.agent_runner as ar

    ar._RUNNABLE_CACHE.clear()
    monkeypatch.setattr(zr, "ChatOpenAI", _NoUsageStub)

    user = crud.get_user_by_email(db_session, "u2@local") or crud.create_user(
        db_session, email="u2@local", provider=None, role="USER"
    )
    agent = crud.create_agent(
        db_session,
        owner_id=user.id,
        name="a2",
        system_instructions="sys",
        task_instructions="task",
        model="gpt-4o-mini",
        schedule=None,
        config={},
    )
    thread = crud.create_thread(
        db=db_session, agent_id=agent.id, title="t2", active=True, agent_state={}, memory_strategy="buffer"
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
    runs = crud.list_runs(db_session, agent.id, limit=1)
    assert runs and runs[0].total_tokens is None and runs[0].total_cost_usd is None
