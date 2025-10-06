"""Tests for ThreadService – ensuring DB helpers work correctly."""

from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage
from langchain_core.messages import SystemMessage
from langchain_core.messages import ToolMessage

from zerg.crud import crud as _crud
from zerg.models.models import Agent
from zerg.services.thread_service import ThreadService


def _create_test_agent(db_session):
    owner = _crud.get_user_by_email(db_session, "dev@local") or _crud.create_user(
        db_session, email="dev@local", provider=None, role="ADMIN"
    )

    return Agent(
        owner_id=owner.id,
        name="TestAgent",
        system_instructions="You are helpful.",
        task_instructions="",
        model="gpt-4o-mini",
    )


def test_create_thread_with_system_message(db_session):
    # Arrange: store agent in DB
    agent = _create_test_agent(db_session)
    db_session.add(agent)
    db_session.commit()
    db_session.refresh(agent)

    # Act
    thread = ThreadService.create_thread_with_system_message(db_session, agent, title="Hello")

    # Assert – thread exists and first message is system prompt
    assert thread.id is not None
    messages = ThreadService.get_thread_messages_as_langchain(db_session, thread.id)
    assert len(messages) == 1
    assert isinstance(messages[0], SystemMessage)
    assert messages[0].content == "You are helpful."


def test_save_and_retrieve_messages(db_session):
    # Prepare agent + thread
    agent = _create_test_agent(db_session)
    db_session.add(agent)
    db_session.commit()
    db_session.refresh(agent)

    thread = ThreadService.create_thread_with_system_message(db_session, agent, title="Conversation")

    # Save additional messages
    new_msgs = [
        HumanMessage(content="Hi"),
        AIMessage(content="Hello!"),
        ToolMessage(content="The time is 12:00", tool_call_id="abc123", name="clock"),
    ]

    ThreadService.save_new_messages(db_session, thread_id=thread.id, messages=new_msgs, processed=True)

    history = ThreadService.get_thread_messages_as_langchain(db_session, thread.id)

    # There should be 1 (system) + 3 = 4 messages
    assert len(history) == 4
    assert isinstance(history[1], HumanMessage)
    assert history[1].content == "Hi"
    assert isinstance(history[3], ToolMessage)
    assert history[3].name == "clock"
