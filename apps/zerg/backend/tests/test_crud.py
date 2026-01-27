from sqlalchemy.orm import Session

from tests.conftest import TEST_MODEL
from tests.conftest import TEST_COMMIS_MODEL
from zerg.crud.crud import create_fiche
from zerg.crud.crud import create_fiche_message
from zerg.crud.crud import create_user
from zerg.crud.crud import delete_fiche
from zerg.crud.crud import get_fiche
from zerg.crud.crud import get_fiche_messages
from zerg.crud.crud import get_fiches
from zerg.crud.crud import update_fiche
from zerg.models.models import Fiche

# Ensure we have at least one user – the sample_fiche already uses _dev_user.


def test_get_fiches(db_session: Session, sample_fiche: Fiche):
    """Test getting all fiches"""
    # Reuse the owner of sample_fiche for additional fiches
    owner_id: int = sample_fiche.owner_id  # type: ignore[attr-defined]

    # Create a few more fiches
    for i in range(3):
        fiche = Fiche(
            owner_id=owner_id,
            name=f"Test Fiche {i}",
            system_instructions=f"System instructions for fiche {i}",
            task_instructions=f"Instructions for fiche {i}",
            model=TEST_MODEL,
            status="idle",
        )
        db_session.add(fiche)
    db_session.commit()

    # Get all fiches
    fiches = get_fiches(db_session)
    assert len(fiches) == 4  # 3 new fiches + 1 sample fiche

    # Test pagination
    fiches_page1 = get_fiches(db_session, skip=0, limit=2)
    fiches_page2 = get_fiches(db_session, skip=2, limit=2)

    assert len(fiches_page1) == 2
    assert len(fiches_page2) == 2
    assert fiches_page1[0].id != fiches_page2[0].id  # Should be different fiches


def test_get_fiche(db_session: Session, sample_fiche: Fiche):
    """Test getting a single fiche by ID"""
    fiche = get_fiche(db_session, sample_fiche.id)
    assert fiche is not None
    assert fiche.id == sample_fiche.id
    assert fiche.name == sample_fiche.name

    # Test getting a non-existent fiche
    non_existent_fiche = get_fiche(db_session, 999)  # Assuming this ID doesn't exist
    assert non_existent_fiche is None


# We need a user row because ``owner_id`` is mandatory.


def _ensure_user(db_session: Session):
    user = create_user(db_session, email="crud@test", provider=None, role="USER")  # type: ignore[arg-type]
    return user


def test_create_fiche(db_session: Session):
    """Test creating a new fiche"""
    owner = _ensure_user(db_session)

    fiche = create_fiche(
        db=db_session,
        owner_id=owner.id,
        system_instructions="System instructions for testing",
        task_instructions="Testing CRUD operations",
        model=TEST_COMMIS_MODEL,
        schedule="0 12 * * *",  # Noon every day
        config={"test": True},
    )

    assert fiche.id is not None
    assert fiche.name == "New Fiche"  # Auto-generated placeholder name
    assert fiche.system_instructions == "System instructions for testing"
    assert fiche.task_instructions == "Testing CRUD operations"
    assert fiche.model == TEST_COMMIS_MODEL
    assert fiche.status == "idle"  # Default value
    assert fiche.schedule == "0 12 * * *"
    assert fiche.config == {"test": True}

    # Verify the fiche was added to the database
    db_fiche = get_fiche(db_session, fiche.id)
    assert db_fiche is not None
    assert db_fiche.id == fiche.id
    assert db_fiche.name == "New Fiche"


def test_update_fiche(db_session: Session, sample_fiche: Fiche):
    """Test updating an existing fiche"""
    # Update some fields
    updated_fiche = update_fiche(
        db_session, sample_fiche.id, name="Updated CRUD Fiche", status="processing", model=TEST_COMMIS_MODEL
    )

    assert updated_fiche is not None
    assert updated_fiche.id == sample_fiche.id
    assert updated_fiche.name == "Updated CRUD Fiche"
    assert updated_fiche.status == "processing"
    assert updated_fiche.model == TEST_COMMIS_MODEL
    assert updated_fiche.system_instructions == sample_fiche.system_instructions  # Should be unchanged
    assert updated_fiche.task_instructions == sample_fiche.task_instructions  # Should be unchanged

    # Verify the changes were saved to the database
    db_fiche = get_fiche(db_session, sample_fiche.id)
    assert db_fiche.name == "Updated CRUD Fiche"
    assert db_fiche.status == "processing"

    # Test updating a non-existent fiche
    non_existent_update = update_fiche(db_session, 999, name="This doesn't exist")
    assert non_existent_update is None


def test_delete_fiche(db_session: Session, sample_fiche: Fiche):
    """Test deleting an fiche"""
    # First verify the fiche exists
    fiche = get_fiche(db_session, sample_fiche.id)
    assert fiche is not None

    # Delete the fiche
    success = delete_fiche(db_session, sample_fiche.id)
    assert success is True

    # Verify the fiche is gone
    deleted_fiche = get_fiche(db_session, sample_fiche.id)
    assert deleted_fiche is None

    # Test deleting a non-existent fiche
    success = delete_fiche(db_session, 999)  # Assuming this ID doesn't exist
    assert success is False


def test_get_fiche_messages(db_session: Session, sample_fiche: Fiche, sample_messages):
    """Test getting messages for a fiche"""
    messages = get_fiche_messages(db_session, sample_fiche.id)
    assert len(messages) == 3  # From the sample_messages fixture

    # Test pagination
    messages_page = get_fiche_messages(db_session, sample_fiche.id, skip=1, limit=1)
    assert len(messages_page) == 1

    # Test getting messages for a non-existent fiche
    non_existent_messages = get_fiche_messages(db_session, 999)
    assert len(non_existent_messages) == 0


def test_create_fiche_message(db_session: Session, sample_fiche: Fiche):
    """Test creating a message for a fiche"""
    message = create_fiche_message(
        db_session, fiche_id=sample_fiche.id, role="user", content="Testing CRUD message creation"
    )

    assert message.id is not None
    assert message.fiche_id == sample_fiche.id
    assert message.role == "user"
    assert message.content == "Testing CRUD message creation"
    assert message.timestamp is not None

    # Verify the message is in the database
    messages = get_fiche_messages(db_session, sample_fiche.id)
    message_ids = [m.id for m in messages]
    assert message.id in message_ids
