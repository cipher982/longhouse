"""Tests for ThreadService – ensuring DB helpers work correctly."""

import re

from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage
from langchain_core.messages import ToolMessage

from tests.conftest import TEST_WORKER_MODEL
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
        model=TEST_WORKER_MODEL,
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
    # System prompts are injected at runtime (not stored in DB)
    assert len(messages) == 0


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

    # System prompts are injected at runtime (not stored in DB)
    assert len(history) == 3

    # Verify user message has timestamp prefix
    assert isinstance(history[0], HumanMessage)
    assert re.match(TIMESTAMP_PATTERN, history[0].content), "User message should have timestamp prefix"
    assert history[0].content.endswith("] Hi"), f"Expected content to end with '] Hi', got: {history[0].content}"

    # Verify assistant message does NOT have timestamp prefix
    assert isinstance(history[1], AIMessage)
    assert not re.match(TIMESTAMP_PATTERN, history[1].content), "Assistant message should not have timestamp prefix"
    assert history[1].content == "Hello!"

    # Verify tool message does NOT have timestamp prefix
    assert isinstance(history[2], ToolMessage)
    assert history[2].name == "clock"
    assert history[2].content == "The time is 12:00"


def test_timestamp_format_in_messages(db_session):
    """Verify that user messages have ISO 8601 timestamp prefix."""
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
    user_msg = history[0]
    assert isinstance(user_msg, HumanMessage)

    # Verify timestamp format matches ISO 8601: [YYYY-MM-DDTHH:MM:SSZ]
    match = re.match(r"^\[(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z\] (.+)$", user_msg.content)
    assert match is not None, f"User message should have ISO 8601 timestamp prefix, got: {user_msg.content}"

    year, month, day, hour, minute, second, content = match.groups()
    assert content == "Test message"

    # Assistant message should remain unprefixed
    assistant_msg = history[1]
    assert isinstance(assistant_msg, AIMessage)
    assert assistant_msg.content == "Response message"
