from datetime import datetime

from sqlalchemy.orm import Session

from zerg.crud import crud as _crud
from zerg.models.models import Agent
from zerg.models.models import AgentMessage
from zerg.schemas.schemas import AgentCreate
from zerg.schemas.schemas import AgentUpdate
from zerg.schemas.schemas import MessageCreate


def test_agent_model(db_session: Session):
    """Test creating an Agent model instance"""
    owner = _crud.get_user_by_email(db_session, "dev@local") or _crud.create_user(
        db_session, email="dev@local", provider=None, role="ADMIN"
    )

    agent = Agent(
        owner_id=owner.id,
        name="Test Agent",
        system_instructions="This is a test system instruction",
        task_instructions="This is a test task instruction",
        model="gpt-4o",
        status="idle",
        schedule=None,
        config={"key": "value"},
    )

    db_session.add(agent)
    db_session.commit()
    db_session.refresh(agent)

    assert agent.id is not None
    assert agent.name == "Test Agent"
    assert agent.system_instructions == "This is a test system instruction"
    assert agent.task_instructions == "This is a test task instruction"
    assert agent.model == "gpt-4o"
    assert agent.status == "idle"
    assert agent.schedule is None
    assert agent.config == {"key": "value"}
    assert agent.created_at is not None
    assert agent.updated_at is not None
    assert isinstance(agent.created_at, datetime)
    assert isinstance(agent.updated_at, datetime)
    assert len(agent.messages) == 0


def test_agent_message_model(db_session: Session, sample_agent: Agent):
    """Test creating an AgentMessage model instance"""
    message = AgentMessage(agent_id=sample_agent.id, role="user", content="Test message content")

    db_session.add(message)
    db_session.commit()
    db_session.refresh(message)

    assert message.id is not None
    assert message.agent_id == sample_agent.id
    assert message.role == "user"
    assert message.content == "Test message content"
    assert message.timestamp is not None
    assert isinstance(message.timestamp, datetime)

    # Test the relationship back to the agent
    assert message.agent.id == sample_agent.id
    assert message.agent.name == sample_agent.name


def test_agent_message_relationship(db_session: Session, sample_agent: Agent):
    """Test the relationship between Agent and AgentMessage"""
    # Create a few messages for the agent
    messages = [
        AgentMessage(agent_id=sample_agent.id, role="system", content="System instructions"),
        AgentMessage(agent_id=sample_agent.id, role="user", content="User message 1"),
        AgentMessage(agent_id=sample_agent.id, role="assistant", content="Assistant reply 1"),
        AgentMessage(agent_id=sample_agent.id, role="user", content="User message 2"),
    ]

    for message in messages:
        db_session.add(message)

    db_session.commit()
    db_session.refresh(sample_agent)

    # Test that the agent has the right number of messages
    assert len(sample_agent.messages) == 4

    # Test cascade delete
    db_session.delete(sample_agent)
    db_session.commit()

    # Check that all messages were deleted
    remaining_messages = db_session.query(AgentMessage).filter(AgentMessage.agent_id == sample_agent.id).count()
    assert remaining_messages == 0


def test_agent_schema_validation():
    """Test the Pydantic schemas for request validation"""
    # Test AgentCreate
    agent_data = {
        "name": "Schema Test Agent",
        "system_instructions": "Test system instructions",
        "task_instructions": "Test task instructions",
        "model": "gpt-4o",
        "schedule": "0 0 * * *",  # Daily at midnight
        "config": {"test_key": "test_value"},
    }

    agent_create = AgentCreate(**agent_data)
    assert agent_create.name == agent_data["name"]
    assert agent_create.system_instructions == agent_data["system_instructions"]
    assert agent_create.task_instructions == agent_data["task_instructions"]
    assert agent_create.model == agent_data["model"]
    assert agent_create.schedule == agent_data["schedule"]
    assert agent_create.config == agent_data["config"]

    # Test AgentUpdate with partial data
    update_data = {"name": "Updated Name", "status": "processing"}

    agent_update = AgentUpdate(**update_data)
    assert agent_update.name == update_data["name"]
    assert agent_update.status == update_data["status"]
    assert agent_update.system_instructions is None  # Not provided
    assert agent_update.task_instructions is None  # Not provided
    assert agent_update.model is None  # Not provided

    # Test MessageCreate
    message_data = {"role": "user", "content": "Test message"}

    message_create = MessageCreate(**message_data)
    assert message_create.role == message_data["role"]
    assert message_create.content == message_data["content"]


def test_execution_state_machine_validation(db_session: Session):
    """Test ExecutionStateMachine validate_state method"""
    from zerg.crud.crud import create_workflow
    from zerg.models.models import WorkflowExecution
    from zerg.services.execution_state import ExecutionStateMachine

    # Create a test workflow
    workflow = create_workflow(db_session, owner_id=1, name="Test", description="Test", canvas={})

    # Test valid states
    execution = WorkflowExecution(workflow_id=workflow.id, phase="waiting", result=None)
    assert ExecutionStateMachine.validate_state(execution) is True

    execution.phase = "running"
    assert ExecutionStateMachine.validate_state(execution) is True

    execution.phase = "finished"
    execution.result = "success"
    assert ExecutionStateMachine.validate_state(execution) is True

    # Test invalid states
    execution.phase = "finished"
    execution.result = None  # Invalid: finished without result
    assert ExecutionStateMachine.validate_state(execution) is False

    execution.phase = "running"
    execution.result = "success"  # Invalid: running with result
    assert ExecutionStateMachine.validate_state(execution) is False

    execution.phase = "finished"
    execution.result = "failure"
    execution.failure_kind = "system"  # Valid: failure with failure_kind
    assert ExecutionStateMachine.validate_state(execution) is True

    execution.result = "success"
    execution.failure_kind = "system"  # Invalid: success with failure_kind
    assert ExecutionStateMachine.validate_state(execution) is False
