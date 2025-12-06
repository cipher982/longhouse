import pytest

pytest.skip("Legacy AgentManager tests skipped - AgentManager has been removed", allow_module_level=True)
"""
These tests are for the deprecated AgentManager which has been completely removed
from the codebase. The functionality has been replaced by AgentRunner and related
services.
"""

from unittest.mock import ANY
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage
from langchain_core.messages import ToolMessage

from zerg.agents import AgentManager
from zerg.models.models import Agent
from zerg.models.models import Thread
from zerg.models.models import ThreadMessage


@pytest.fixture
def mock_llm():
    """Create a mock LLM for testing"""
    mock = MagicMock()
    # Mock the invoke method to return a response with content
    response = MagicMock()
    response.content = "This is a test response"
    mock.invoke.return_value = response
    return mock


@pytest.fixture
def agent_manager(sample_agent: Agent, mock_llm):
    """Create an AgentManager instance with a mock LLM"""
    with patch("zerg.agents.ChatOpenAI", return_value=mock_llm):
        agent_manager = AgentManager(sample_agent)
        return agent_manager


def test_agent_manager_init(sample_agent: Agent):
    """Test initializing an AgentManager instance"""
    with patch("zerg.legacy_agent_manager.ChatOpenAI") as mock_chat_openai:
        agent_manager = AgentManager(sample_agent)
        assert agent_manager.agent_model == sample_agent
        # Check that the LLM was initialized with the correct model
        mock_chat_openai.assert_called_once()
        args, kwargs = mock_chat_openai.call_args
        assert kwargs["model"] == sample_agent.model


def test_build_graph(agent_manager: AgentManager):
    """Test building a LangGraph state machine with tool handling"""
    with patch("zerg.legacy_agent_manager.StateGraph") as mock_state_graph:
        # Setup the mock graph builder
        mock_builder = mock_state_graph.return_value

        # Call the method
        agent_manager._build_graph()

        # DEBUG: Print the arguments add_edge was called with
        print(f"DEBUG: mock_builder.add_edge called with: {mock_builder.add_edge.call_args_list}")

        # Verify that both nodes were added
        mock_builder.add_node.assert_any_call("chatbot", ANY)
        mock_builder.add_node.assert_any_call("call_tool", ANY)

        # Verify the basic edges were added
        mock_builder.add_edge.assert_any_call("__start__", "chatbot")
        mock_builder.add_edge.assert_any_call("call_tool", "chatbot")

        # Verify conditional edges were added
        mock_builder.add_conditional_edges.assert_called_once_with(
            "chatbot",
            agent_manager._decide_next_step,
            {
                "call_tool": "call_tool",
                "__end__": "__end__",
            },
        )

        # Verify that compile was called
        mock_builder.compile.assert_called_once()


def test_chatbot_node(agent_manager: AgentManager):
    """Test the chatbot node function with tool binding"""
    # Create a test state
    state = {
        "messages": [{"role": "user", "content": "Hello, test assistant"}],
        "metadata": {"agent_id": 1, "thread_id": 1},
    }

    # Mock the llm_with_tools.invoke method
    with patch.object(agent_manager.llm_with_tools, "invoke") as mock_invoke:
        mock_response = MagicMock()
        mock_response.content = "This is a test response"
        mock_invoke.return_value = mock_response

        # Call the node function
        result = agent_manager._chatbot_node(state)

        # Verify the result
        assert "messages" in result
        assert len(result["messages"]) == 1
        assert result["messages"][0].content == "This is a test response"

        # Verify the LLM was called with the expected arguments
        mock_invoke.assert_called_once_with(state["messages"])


def test_get_or_create_thread_new(db_session, agent_manager: AgentManager):
    """Test creating a new thread"""
    with patch("zerg.crud.crud") as mock_crud:
        # Mock crud.get_thread to return None (no existing thread)
        mock_crud.get_thread.return_value = None
        # Mock crud.get_active_thread to return None (no active thread)
        mock_crud.get_active_thread.return_value = None

        # Mock creating a new thread
        mock_thread = MagicMock(spec=Thread)
        mock_thread.id = 1
        mock_thread.agent_id = agent_manager.agent_model.id
        mock_crud.create_thread.return_value = mock_thread

        # Call the method to create a new thread
        thread, created = agent_manager.get_or_create_thread(db_session, title="Test Thread")

        # Verify the right methods were called
        mock_crud.get_active_thread.assert_called_once_with(db_session, agent_manager.agent_model.id)
        mock_crud.create_thread.assert_called_once()
        args, kwargs = mock_crud.create_thread.call_args
        assert kwargs["db"] == db_session
        assert kwargs["agent_id"] == agent_manager.agent_model.id
        assert kwargs["title"] == "Test Thread"
        assert kwargs["active"] is True

        # Verify the results
        assert thread == mock_thread
        assert created is True


def test_get_or_create_thread_existing(db_session, agent_manager: AgentManager):
    """Test getting an existing thread"""
    with patch("zerg.crud.crud") as mock_crud:
        # Create a mock thread
        mock_thread = MagicMock(spec=Thread)
        mock_thread.id = 1
        mock_thread.agent_id = agent_manager.agent_model.id

        # Mock crud.get_thread to return the mock thread
        mock_crud.get_thread.return_value = mock_thread

        # Call the method to get an existing thread
        thread, created = agent_manager.get_or_create_thread(db_session, thread_id=1)

        # Verify the right methods were called
        mock_crud.get_thread.assert_called_once_with(db_session, 1)

        # Verify the results
        assert thread == mock_thread
        assert created is False


def test_add_system_message(db_session, agent_manager: AgentManager):
    """Test adding a system message to a thread"""
    with patch("zerg.crud.crud") as mock_crud:
        # Create a mock thread
        mock_thread = MagicMock(spec=Thread)
        mock_thread.id = 1

        # Mock crud.get_thread_messages to return an empty list (no messages yet)
        mock_crud.get_thread_messages.return_value = []

        # Call the method to add a system message
        agent_manager.add_system_message(db_session, mock_thread)

        # Verify the right methods were called
        mock_crud.get_thread_messages.assert_called_once_with(db_session, mock_thread.id)
        mock_crud.create_thread_message.assert_called_once()
        args, kwargs = mock_crud.create_thread_message.call_args
        assert kwargs["db"] == db_session
        assert kwargs["thread_id"] == mock_thread.id
        assert kwargs["role"] == "system"
        assert kwargs["content"] == agent_manager.agent_model.system_instructions


def test_add_system_message_with_existing_messages(db_session, agent_manager: AgentManager):
    """Test not adding a system message when messages already exist"""
    with patch("zerg.crud.crud") as mock_crud:
        # Create a mock thread
        mock_thread = MagicMock(spec=Thread)
        mock_thread.id = 1

        # Mock crud.get_thread_messages to return a list with a message (messages already exist)
        mock_message = MagicMock(spec=ThreadMessage)
        mock_crud.get_thread_messages.return_value = [mock_message]

        # Call the method to add a system message
        agent_manager.add_system_message(db_session, mock_thread)

        # Verify the right methods were called
        mock_crud.get_thread_messages.assert_called_once_with(db_session, mock_thread.id)
        # Verify that create_thread_message was not called
        mock_crud.create_thread_message.assert_not_called()


def test_process_message(db_session, agent_manager: AgentManager, mock_llm):
    """Test processing a message through the LangGraph agent"""
    with patch("zerg.crud.crud") as mock_crud, patch.object(agent_manager, "_build_graph") as mock_build_graph:
        # Create mock objects
        mock_thread = MagicMock(spec=Thread)
        mock_thread.id = 1

        # Mock crud.create_thread_message with side effect to return different messages
        mock_user_message = MagicMock(spec=ThreadMessage)
        mock_assistant_message = MagicMock(spec=ThreadMessage)
        mock_crud.create_thread_message.side_effect = [mock_user_message, mock_assistant_message]

        # Mock crud.get_thread_messages to return a list of messages
        mock_messages = [
            MagicMock(spec=ThreadMessage, role="system", content="You are a test assistant", tool_calls=None),
            MagicMock(spec=ThreadMessage, role="user", content="Hello", tool_calls=None),
        ]
        mock_crud.get_thread_messages.return_value = mock_messages

        # Set up the mock graph for non-streaming mode
        mock_graph = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "This is a test response"
        mock_graph.invoke.return_value = {"messages": [mock_response]}
        mock_build_graph.return_value = mock_graph

        # Create the user message (new API)
        mock_crud.create_thread_message(
            db=db_session, thread_id=mock_thread.id, role="user", content="Test message", processed=False
        )
        # Call the method to process the thread (non-streaming)
        response_generator = agent_manager.process_thread(db=db_session, thread=mock_thread, stream=False)

        # Consume the generator to get the response
        response = next(response_generator)

        # Verify the LangGraph processing
        assert isinstance(response, dict)
        assert response["content"] == "This is a test response"
        # Accept either tool_output or assistant_message - our mocks don't behave exactly like real messages
        assert response["chunk_type"] in ["tool_output", "assistant_message"]

        # Verify the first create_thread_message call (user message)
        assert mock_crud.create_thread_message.call_count >= 1
        first_call_kwargs = mock_crud.create_thread_message.call_args_list[0][1]
        assert first_call_kwargs["db"] == db_session
        assert first_call_kwargs["thread_id"] == mock_thread.id
        assert first_call_kwargs["role"] == "user"
        assert first_call_kwargs["content"] == "Test message"

        # Run the generator to completion to ensure all side effects happen
        try:
            while True:
                next(response_generator)
        except StopIteration:
            pass

        # Now verify the assistant message was created
        assert mock_crud.create_thread_message.call_count >= 2
        second_call_kwargs = mock_crud.create_thread_message.call_args_list[1][1]
        assert second_call_kwargs["db"] == db_session
        assert second_call_kwargs["thread_id"] == mock_thread.id
        assert second_call_kwargs["role"] == "assistant"
        assert second_call_kwargs["content"] == "This is a test response"

        # Verify the thread was updated
        mock_crud.update_thread.assert_called_once_with(db_session, mock_thread.id)


def test_process_message_with_tool_calls(db_session, agent_manager: AgentManager):
    """Test processing a message with tool calls"""
    with patch("zerg.crud.crud") as mock_crud, patch.object(agent_manager, "_build_graph") as mock_build_graph:
        # Create mock objects
        mock_thread = MagicMock(spec=Thread)
        mock_thread.id = 1

        # Mock crud.create_thread_message to return different messages on each call
        mock_user_message = MagicMock(spec=ThreadMessage)
        mock_assistant_message = MagicMock(spec=ThreadMessage)
        mock_crud.create_thread_message.side_effect = [mock_user_message, mock_assistant_message]

        # Mock crud.get_thread_messages to return messages with tool calls
        mock_system_message = MagicMock(spec=ThreadMessage)
        mock_system_message.role = "system"
        mock_system_message.content = "You are a test assistant"
        mock_system_message.tool_calls = None

        mock_user_message = MagicMock(spec=ThreadMessage)
        mock_user_message.role = "user"
        mock_user_message.content = "What's the weather?"
        mock_user_message.tool_calls = None

        mock_assistant_message = MagicMock(spec=ThreadMessage)
        mock_assistant_message.role = "assistant"
        mock_assistant_message.content = ""
        mock_assistant_message.tool_calls = [{"type": "function", "function": {"name": "get_weather"}}]
        mock_assistant_message.name = None
        mock_assistant_message.tool_call_id = None

        mock_crud.get_thread_messages.return_value = [mock_system_message, mock_user_message, mock_assistant_message]

        # Set up the mock graph stream
        mock_graph = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "Weather response"
        # In streaming mode, return a message that can be processed
        mock_graph.stream.return_value = [{"chatbot": {"messages": [mock_response]}}]
        mock_build_graph.return_value = mock_graph

        # Create the user message (new API)
        mock_crud.create_thread_message(
            db=db_session, thread_id=mock_thread.id, role="user", content="What's the temperature?", processed=False
        )
        # Call the method to process the thread
        response_generator = agent_manager.process_thread(db=db_session, thread=mock_thread, stream=True)

        # Consume the generator to get the response
        response = next(response_generator)

        # Verify the right methods were called - initially called for user message
        assert mock_crud.create_thread_message.call_count >= 1
        first_call_kwargs = mock_crud.create_thread_message.call_args_list[0][1]
        assert first_call_kwargs["db"] == db_session
        assert first_call_kwargs["thread_id"] == mock_thread.id
        assert first_call_kwargs["role"] == "user"
        assert first_call_kwargs["content"] == "What's the temperature?"

        # Verify the response is now a dictionary with metadata
        assert isinstance(response, dict)
        assert response["content"] == "Weather response"
        # Accept either tool_output or assistant_message - our mocks don't behave exactly like real messages
        assert response["chunk_type"] in ["tool_output", "assistant_message"]

        # Run the generator to completion to ensure all side effects happen
        try:
            while True:
                next(response_generator)
        except StopIteration:
            pass

        # Now verify the assistant message was created
        assert mock_crud.create_thread_message.call_count >= 2
        second_call_kwargs = mock_crud.create_thread_message.call_args_list[1][1]
        assert second_call_kwargs["db"] == db_session
        assert second_call_kwargs["thread_id"] == mock_thread.id
        assert second_call_kwargs["role"] == "assistant"
        assert second_call_kwargs["content"] == "Weather response"


def test_call_tool_node(agent_manager: AgentManager):
    """Test the tool execution node function"""
    # Create a mock AIMessage with a tool call
    tool_call = {"id": "call_123", "name": "get_current_time", "args": {}}
    last_message = MagicMock(spec=AIMessage)
    last_message.tool_calls = [tool_call]

    # Create a test state with the tool call
    state = {
        "messages": [last_message],
        "metadata": {"agent_id": 1, "thread_id": 1},
    }

    # Call the node function
    result = agent_manager._call_tool_node(state)

    # Verify the result
    assert "messages" in result
    assert len(result["messages"]) == 1
    tool_message = result["messages"][0]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.tool_call_id == "call_123"
    assert tool_message.name == "get_current_time"
    # The content should be an ISO format datetime string
    assert "T" in tool_message.content  # Simple check for ISO format


def test_decide_next_step(agent_manager: AgentManager):
    """Test the decision function for routing messages"""
    # Test case 1: Message with tool calls
    tool_call = {"id": "call_123", "name": "get_current_time", "args": {}}
    message_with_tool = MagicMock(spec=AIMessage)
    message_with_tool.tool_calls = [tool_call]
    state_with_tool = {
        "messages": [message_with_tool],
    }
    assert agent_manager._decide_next_step(state_with_tool) == "call_tool"

    # Test case 2: Regular message without tool calls
    message_without_tool = MagicMock(spec=AIMessage)
    message_without_tool.tool_calls = None
    state_without_tool = {
        "messages": [message_without_tool],
    }
    assert agent_manager._decide_next_step(state_without_tool) == "__end__"
