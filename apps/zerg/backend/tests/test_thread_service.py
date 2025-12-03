"""Tests for ThreadService – ensuring DB helpers work correctly."""

import re

from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage
from langchain_core.messages import SystemMessage
from langchain_core.messages import ToolMessage

from zerg.crud import crud as _crud
from zerg.models.models import Agent
from zerg.services.thread_service import ThreadService

# Regex pattern for ISO 8601 timestamp prefix: [YYYY-MM-DDTHH:MM:SSZ]
TIMESTAMP_PATTERN = r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\] "


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

    # Verify user message has timestamp prefix
    assert isinstance(history[1], HumanMessage)
    assert re.match(TIMESTAMP_PATTERN, history[1].content), "User message should have timestamp prefix"
    assert history[1].content.endswith("] Hi"), f"Expected content to end with '] Hi', got: {history[1].content}"

    # Verify assistant message has timestamp prefix
    assert isinstance(history[2], AIMessage)
    assert re.match(TIMESTAMP_PATTERN, history[2].content), "Assistant message should have timestamp prefix"
    assert history[2].content.endswith("] Hello!"), f"Expected content to end with '] Hello!', got: {history[2].content}"

    # Verify tool message does NOT have timestamp prefix
    assert isinstance(history[3], ToolMessage)
    assert history[3].name == "clock"
    assert history[3].content == "The time is 12:00"


def test_timestamp_format_in_messages(db_session):
    """Verify that user and assistant messages have ISO 8601 timestamp prefix."""
    # Prepare agent + thread
    agent = _create_test_agent(db_session)
    db_session.add(agent)
    db_session.commit()
    db_session.refresh(agent)

    thread = ThreadService.create_thread_with_system_message(db_session, agent, title="Timestamp Test")

    # Save messages
    new_msgs = [
        HumanMessage(content="Test message"),
        AIMessage(content="Response message"),
    ]

    ThreadService.save_new_messages(db_session, thread_id=thread.id, messages=new_msgs, processed=True)
    history = ThreadService.get_thread_messages_as_langchain(db_session, thread.id)

    # Extract timestamp from user message
    user_msg = history[1]
    assert isinstance(user_msg, HumanMessage)

    # Verify timestamp format matches ISO 8601: [YYYY-MM-DDTHH:MM:SSZ]
    match = re.match(r"^\[(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z\] (.+)$", user_msg.content)
    assert match is not None, f"User message should have ISO 8601 timestamp prefix, got: {user_msg.content}"

    year, month, day, hour, minute, second, content = match.groups()
    assert content == "Test message"

    # Verify assistant message also has timestamp
    assistant_msg = history[2]
    assert isinstance(assistant_msg, AIMessage)
    match = re.match(r"^\[(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z\] (.+)$", assistant_msg.content)
    assert match is not None, f"Assistant message should have ISO 8601 timestamp prefix, got: {assistant_msg.content}"

    _, _, _, _, _, _, content = match.groups()
    assert content == "Response message"
