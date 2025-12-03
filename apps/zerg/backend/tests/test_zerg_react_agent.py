"""Unit-tests for the *pure* Functional-API agent definition."""

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest


@pytest.fixture()
def agent_row(db_session):
    """Insert a minimal Agent row in the test DB and return it."""

    # Ensure we have a user row for ownership
    from zerg.crud import crud as _crud  # noqa: WPS433
    from zerg.models.models import Agent as AgentModel

    owner = _crud.get_user_by_email(db_session, "dev@local") or _crud.create_user(
        db_session, email="dev@local", provider=None, role="ADMIN"
    )

    row = AgentModel(
        owner_id=owner.id,
        name="unit-bot",
        system_instructions="sys",
        task_instructions="task",
        model="gpt-4o-mini",
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def test_get_runnable_returns_graph(agent_row):
    from zerg.agents_def import zerg_react_agent as mod

    runnable = mod.get_runnable(agent_row)

    # The compiled graph exposes .invoke() – that is enough for the runner.
    assert hasattr(runnable, "invoke")


def test_invoke_calls_llm(agent_row):
    from zerg.agents_def import zerg_react_agent as mod

    # Patch ChatOpenAI so we don't hit real LLM
    with (
        patch("zerg.agents_def.zerg_react_agent.ChatOpenAI") as mock_llm_cls,
        patch("zerg.agents_def.zerg_react_agent.MemorySaver", new=lambda *a, **kw: None),
    ):
        mock_instance = MagicMock()
        # When llm_with_tools.invoke is called, return AIMessage("hi")
        from unittest.mock import AsyncMock

        from langchain_core.messages import AIMessage

        mock_instance.bind_tools.return_value.invoke.return_value = AIMessage(content="hi")
        # Fix: The runtime now awaits ainvoke() inside _call_model_async
        mock_instance.bind_tools.return_value.ainvoke = AsyncMock(return_value=AIMessage(content="hi"))
        mock_llm_cls.return_value = mock_instance

        runnable = mod.get_runnable(agent_row)

        # We don't patch the whole graph – the real runnable should work with
        # our stubbed LLM. It should append an AIMessage("hi").

        res = runnable.invoke([])

        # The runnable returns list of messages; last one should be AIMessage("hi")
        assert res[-1].content == "hi"
