from datetime import datetime

from sqlalchemy.orm import Session

from tests.conftest import TEST_MODEL
from zerg.crud import crud as _crud
from zerg.models.models import Fiche
from zerg.models.models import FicheMessage
from zerg.schemas.schemas import FicheCreate
from zerg.schemas.schemas import FicheUpdate
from zerg.schemas.schemas import MessageCreate


def test_fiche_model(db_session: Session):
    """Test creating an Fiche model instance"""
    owner = _crud.get_user_by_email(db_session, "dev@local") or _crud.create_user(
        db_session, email="dev@local", provider=None, role="ADMIN"
    )

    fiche = Fiche(
        owner_id=owner.id,
        name="Test Fiche",
        system_instructions="This is a test system instruction",
        task_instructions="This is a test task instruction",
        model=TEST_MODEL,
        status="idle",
        schedule=None,
        config={"key": "value"},
    )

    db_session.add(fiche)
    db_session.commit()
    db_session.refresh(fiche)

    assert fiche.id is not None
    assert fiche.name == "Test Fiche"
    assert fiche.system_instructions == "This is a test system instruction"
    assert fiche.task_instructions == "This is a test task instruction"
    assert fiche.model == TEST_MODEL
    assert fiche.status == "idle"
    assert fiche.schedule is None
    assert fiche.config == {"key": "value"}
    assert fiche.created_at is not None
    assert fiche.updated_at is not None
    assert isinstance(fiche.created_at, datetime)
    assert isinstance(fiche.updated_at, datetime)
    assert len(fiche.messages) == 0


def test_fiche_message_model(db_session: Session, sample_fiche: Fiche):
    """Test creating an FicheMessage model instance"""
    message = FicheMessage(fiche_id=sample_fiche.id, role="user", content="Test message content")

    db_session.add(message)
    db_session.commit()
    db_session.refresh(message)

    assert message.id is not None
    assert message.fiche_id == sample_fiche.id
    assert message.role == "user"
    assert message.content == "Test message content"
    assert message.timestamp is not None
    assert isinstance(message.timestamp, datetime)

    # Test the relationship back to the fiche
    assert message.fiche.id == sample_fiche.id
    assert message.fiche.name == sample_fiche.name


def test_fiche_message_relationship(db_session: Session, sample_fiche: Fiche):
    """Test the relationship between Fiche and FicheMessage"""
    # Create a few messages for the fiche
    messages = [
        FicheMessage(fiche_id=sample_fiche.id, role="system", content="System instructions"),
        FicheMessage(fiche_id=sample_fiche.id, role="user", content="User message 1"),
        FicheMessage(fiche_id=sample_fiche.id, role="assistant", content="Assistant reply 1"),
        FicheMessage(fiche_id=sample_fiche.id, role="user", content="User message 2"),
    ]

    for message in messages:
        db_session.add(message)

    db_session.commit()
    db_session.refresh(sample_fiche)

    # Test that the fiche has the right number of messages
    assert len(sample_fiche.messages) == 4

    # Test cascade delete
    db_session.delete(sample_fiche)
    db_session.commit()

    # Check that all messages were deleted
    remaining_messages = db_session.query(FicheMessage).filter(FicheMessage.fiche_id == sample_fiche.id).count()
    assert remaining_messages == 0


def test_fiche_schema_validation():
    """Test the Pydantic schemas for request validation"""
    # Test FicheCreate
    fiche_data = {
        "name": "Schema Test Fiche",
        "system_instructions": "Test system instructions",
        "task_instructions": "Test task instructions",
        "model": TEST_MODEL,
        "schedule": "0 0 * * *",  # Daily at midnight
        "config": {"test_key": "test_value"},
    }

    fiche_create = FicheCreate(**fiche_data)
    # FicheCreate does not have a 'name' field (it's auto-generated)
    # assert fiche_create.name == fiche_data["name"]
    assert fiche_create.system_instructions == fiche_data["system_instructions"]
    assert fiche_create.task_instructions == fiche_data["task_instructions"]
    assert fiche_create.model == fiche_data["model"]
    assert fiche_create.schedule == fiche_data["schedule"]
    assert fiche_create.config == fiche_data["config"]

    # Test FicheUpdate with partial data
    update_data = {"name": "Updated Name", "status": "processing"}

    fiche_update = FicheUpdate(**update_data)
    assert fiche_update.name == update_data["name"]
    assert fiche_update.status == update_data["status"]
    assert fiche_update.system_instructions is None  # Not provided
    assert fiche_update.task_instructions is None  # Not provided
    assert fiche_update.model is None  # Not provided

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
