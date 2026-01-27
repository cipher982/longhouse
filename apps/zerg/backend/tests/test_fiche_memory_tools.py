"""Tests for fiche memory tools."""

import pytest

from zerg.connectors.context import set_credential_resolver
from zerg.connectors.resolver import CredentialResolver
from zerg.models.models import FicheMemoryKV
from zerg.tools.builtin.fiche_memory_tools import fiche_memory_delete
from zerg.tools.builtin.fiche_memory_tools import fiche_memory_export
from zerg.tools.builtin.fiche_memory_tools import fiche_memory_get
from zerg.tools.builtin.fiche_memory_tools import fiche_memory_set


@pytest.fixture
def credential_context(db_session, test_user):
    """Set up credential resolver context for tools."""
    resolver = CredentialResolver(fiche_id=1, db=db_session, owner_id=test_user.id)
    token = set_credential_resolver(resolver)
    yield resolver
    set_credential_resolver(None)


def test_memory_set_basic(credential_context, db_session):
    """Test setting a basic key-value pair."""
    result = fiche_memory_set(
        key="test_key",
        value={"data": "test_value"},
    )

    # Verify result format
    assert result["ok"] is True
    assert "data" in result
    data = result["data"]
    assert data["key"] == "test_key"
    assert data["value"] == {"data": "test_value"}
    assert data["tags"] == []
    assert data["expires_at"] is None
    assert "created_at" in data
    assert "updated_at" in data

    # Verify in database
    entry = (
        db_session.query(FicheMemoryKV)
        .filter(FicheMemoryKV.user_id == credential_context.owner_id, FicheMemoryKV.key == "test_key")
        .first()
    )
    assert entry is not None
    assert entry.value == {"data": "test_value"}
    assert entry.user_id == credential_context.owner_id


def test_memory_set_with_tags(credential_context):
    """Test setting memory with tags."""
    result = fiche_memory_set(
        key="tagged_key",
        value="tagged_value",
        tags=["important", "settings"],
    )

    assert result["ok"] is True
    assert result["data"]["tags"] == ["important", "settings"]


def test_memory_set_with_expiration(credential_context):
    """Test setting memory with expiration date."""
    result = fiche_memory_set(
        key="expiring_key",
        value="temporary_data",
        expires_at="2025-12-31T23:59:59Z",
    )

    assert result["ok"] is True
    assert result["data"]["expires_at"] == "2025-12-31T23:59:59+00:00"


def test_memory_set_update_existing(credential_context):
    """Test updating an existing key."""
    # Create initial entry
    fiche_memory_set(key="update_key", value="original_value")

    # Update the entry
    result = fiche_memory_set(
        key="update_key",
        value="updated_value",
        tags=["modified"],
    )

    assert result["ok"] is True
    assert result["data"]["value"] == "updated_value"
    assert result["data"]["tags"] == ["modified"]


def test_memory_set_various_types(credential_context):
    """Test storing different value types."""
    # Dict
    result = fiche_memory_set(key="dict_key", value={"a": 1, "b": 2})
    assert result["ok"] is True

    # List
    result = fiche_memory_set(key="list_key", value=[1, 2, 3, 4])
    assert result["ok"] is True

    # String
    result = fiche_memory_set(key="string_key", value="simple string")
    assert result["ok"] is True

    # Number
    result = fiche_memory_set(key="number_key", value=42)
    assert result["ok"] is True

    # Boolean
    result = fiche_memory_set(key="bool_key", value=True)
    assert result["ok"] is True


def test_memory_set_empty_key(credential_context):
    """Test that empty key is rejected."""
    result = fiche_memory_set(key="", value="test")

    assert result["ok"] is False
    assert result["error_type"] == "validation_error"
    assert "empty" in result["user_message"].lower()


def test_memory_set_invalid_expiration(credential_context):
    """Test that invalid expiration date is rejected."""
    result = fiche_memory_set(key="test_key", value="test", expires_at="not-a-date")

    assert result["ok"] is False
    assert result["error_type"] == "validation_error"
    assert "Invalid expiration date" in result["user_message"]


def test_memory_set_no_context():
    """Test that set fails without credential context."""
    result = fiche_memory_set(key="test", value="data")

    assert result["ok"] is False
    assert result["error_type"] == "execution_error"
    assert "No user context" in result["user_message"]


def test_memory_get_by_key(credential_context):
    """Test retrieving a specific key."""
    # Create entry
    fiche_memory_set(key="get_test", value={"data": "value"}, tags=["test"])

    # Retrieve it
    result = fiche_memory_get(key="get_test")

    assert result["ok"] is True
    data = result["data"]
    assert data["key"] == "get_test"
    assert data["value"] == {"data": "value"}
    assert data["tags"] == ["test"]
    assert data["found"] is True


def test_memory_get_nonexistent_key(credential_context):
    """Test getting a key that doesn't exist."""
    result = fiche_memory_get(key="nonexistent")

    assert result["ok"] is True
    data = result["data"]
    assert data["key"] == "nonexistent"
    assert data["value"] is None
    assert data["found"] is False


def test_memory_get_by_tags(credential_context):
    """Test retrieving entries by tags."""
    # Create multiple entries with tags
    fiche_memory_set(key="key1", value="val1", tags=["tag1", "tag2"])
    fiche_memory_set(key="key2", value="val2", tags=["tag2", "tag3"])
    fiche_memory_set(key="key3", value="val3", tags=["tag3"])

    # Get entries with tag2
    result = fiche_memory_get(tags=["tag2"])

    assert result["ok"] is True
    data = result["data"]
    assert data["count"] == 2
    keys = [entry["key"] for entry in data["entries"]]
    assert "key1" in keys
    assert "key2" in keys


def test_memory_get_by_multiple_tags(credential_context):
    """Test retrieving entries matching ANY of multiple tags."""
    # Create entries
    fiche_memory_set(key="key1", value="val1", tags=["red"])
    fiche_memory_set(key="key2", value="val2", tags=["blue"])
    fiche_memory_set(key="key3", value="val3", tags=["green"])

    # Get entries with red OR blue
    result = fiche_memory_get(tags=["red", "blue"])

    assert result["ok"] is True
    data = result["data"]
    assert data["count"] == 2
    keys = [entry["key"] for entry in data["entries"]]
    assert "key1" in keys
    assert "key2" in keys
    assert "key3" not in keys


def test_memory_get_all(credential_context):
    """Test retrieving all entries."""
    # Create multiple entries
    fiche_memory_set(key="all1", value="v1")
    fiche_memory_set(key="all2", value="v2")
    fiche_memory_set(key="all3", value="v3")

    # Get all
    result = fiche_memory_get()

    assert result["ok"] is True
    data = result["data"]
    assert data["count"] == 3
    assert data["limit"] == 100


def test_memory_get_with_limit(credential_context):
    """Test limit parameter."""
    # Create 5 entries
    for i in range(5):
        fiche_memory_set(key=f"limit_key{i}", value=f"val{i}")

    # Get with limit of 3
    result = fiche_memory_get(limit=3)

    assert result["ok"] is True
    data = result["data"]
    assert data["count"] == 3
    assert data["limit"] == 3


def test_memory_get_invalid_limit(credential_context):
    """Test that invalid limits are rejected."""
    result = fiche_memory_get(limit=2000)

    assert result["ok"] is False
    assert result["error_type"] == "validation_error"
    assert "Invalid limit" in result["user_message"]


def test_memory_get_no_context():
    """Test that get fails without credential context."""
    result = fiche_memory_get(key="test")

    assert result["ok"] is False
    assert result["error_type"] == "execution_error"


def test_memory_delete_by_key(credential_context, db_session):
    """Test deleting a specific key."""
    # Create entry
    fiche_memory_set(key="delete_me", value="temporary")

    # Delete it
    result = fiche_memory_delete(key="delete_me")

    assert result["ok"] is True
    data = result["data"]
    assert data["deleted_count"] == 1
    assert data["key"] == "delete_me"

    # Verify it's gone
    entry = (
        db_session.query(FicheMemoryKV)
        .filter(FicheMemoryKV.user_id == credential_context.owner_id, FicheMemoryKV.key == "delete_me")
        .first()
    )
    assert entry is None


def test_memory_delete_nonexistent_key(credential_context):
    """Test deleting a key that doesn't exist."""
    result = fiche_memory_delete(key="nonexistent")

    assert result["ok"] is True
    assert result["data"]["deleted_count"] == 0


def test_memory_delete_by_tags(credential_context, db_session):
    """Test deleting entries by tags."""
    # Create entries
    fiche_memory_set(key="temp1", value="v1", tags=["temporary"])
    fiche_memory_set(key="temp2", value="v2", tags=["temporary", "cache"])
    fiche_memory_set(key="keep1", value="v3", tags=["permanent"])

    # Delete temporary entries
    result = fiche_memory_delete(tags=["temporary"])

    assert result["ok"] is True
    data = result["data"]
    assert data["deleted_count"] == 2

    # Verify correct entries were deleted
    remaining = db_session.query(FicheMemoryKV).filter(FicheMemoryKV.user_id == credential_context.owner_id).all()
    assert len(remaining) == 1
    assert remaining[0].key == "keep1"


def test_memory_delete_no_parameters(credential_context):
    """Test that delete requires at least one parameter."""
    result = fiche_memory_delete()

    assert result["ok"] is False
    assert result["error_type"] == "validation_error"
    assert "at least one" in result["user_message"].lower()


def test_memory_delete_no_context():
    """Test that delete fails without credential context."""
    result = fiche_memory_delete(key="test")

    assert result["ok"] is False
    assert result["error_type"] == "execution_error"


def test_memory_export(credential_context):
    """Test exporting all memory entries."""
    # Create multiple entries
    fiche_memory_set(key="export1", value="v1", tags=["tag1"])
    fiche_memory_set(key="export2", value="v2", tags=["tag2"])
    fiche_memory_set(key="export3", value="v3")

    # Export
    result = fiche_memory_export()

    assert result["ok"] is True
    data = result["data"]
    assert data["count"] == 3
    assert data["total_count"] == 3
    assert data["truncated"] is False
    assert len(data["entries"]) == 3

    # Verify all entries are present
    keys = [entry["key"] for entry in data["entries"]]
    assert "export1" in keys
    assert "export2" in keys
    assert "export3" in keys


def test_memory_export_empty(credential_context):
    """Test exporting when no entries exist."""
    result = fiche_memory_export()

    assert result["ok"] is True
    data = result["data"]
    assert data["count"] == 0
    assert data["total_count"] == 0
    assert data["truncated"] is False
    assert data["entries"] == []


def test_memory_export_no_context():
    """Test that export fails without credential context."""
    result = fiche_memory_export()

    assert result["ok"] is False
    assert result["error_type"] == "execution_error"


def test_user_isolation(credential_context, db_session, test_user):
    """Test that users can only access their own memory."""
    from zerg.crud import crud

    # Create memory as User A
    fiche_memory_set(key="user_a_key", value="user_a_data", tags=["private"])

    # Verify User A can see it
    result = fiche_memory_get(key="user_a_key")
    assert result["ok"] is True
    assert result["data"]["found"] is True

    # Create User B
    user_b = crud.create_user(db=db_session, email="userb@test.com")

    # Switch to User B context
    resolver_b = CredentialResolver(fiche_id=2, db=db_session, owner_id=user_b.id)
    set_credential_resolver(resolver_b)

    # Verify User B cannot see User A's memory
    result_b = fiche_memory_get(key="user_a_key")
    assert result_b["ok"] is True
    assert result_b["data"]["found"] is False

    # Verify User B's list is empty
    list_result = fiche_memory_get()
    assert list_result["ok"] is True
    assert list_result["data"]["count"] == 0

    # Create memory as User B
    fiche_memory_set(key="user_b_key", value="user_b_data")

    # Verify User B can see their own memory
    result_b2 = fiche_memory_get(key="user_b_key")
    assert result_b2["ok"] is True
    assert result_b2["data"]["found"] is True

    # Switch back to User A
    set_credential_resolver(credential_context)

    # Verify User A still has their memory
    result_a = fiche_memory_get(key="user_a_key")
    assert result_a["ok"] is True
    assert result_a["data"]["found"] is True

    # Verify User A cannot see User B's memory
    result_a2 = fiche_memory_get(key="user_b_key")
    assert result_a2["ok"] is True
    assert result_a2["data"]["found"] is False

    # Verify User A's delete doesn't affect User B's data
    fiche_memory_delete(key="user_b_key")
    set_credential_resolver(resolver_b)
    result_b3 = fiche_memory_get(key="user_b_key")
    assert result_b3["ok"] is True
    assert result_b3["data"]["found"] is True


def test_tag_filtering_edge_cases(credential_context):
    """Test edge cases in tag filtering."""
    # Create entries with overlapping tags
    fiche_memory_set(key="k1", value="v1", tags=["a", "b"])
    fiche_memory_set(key="k2", value="v2", tags=["b", "c"])
    fiche_memory_set(key="k3", value="v3", tags=["c", "d"])
    fiche_memory_set(key="k4", value="v4", tags=[])  # No tags

    # Get entries with tag "b"
    result = fiche_memory_get(tags=["b"])
    assert result["ok"] is True
    assert result["data"]["count"] == 2

    # Get entries with tag "e" (doesn't exist)
    result = fiche_memory_get(tags=["e"])
    assert result["ok"] is True
    assert result["data"]["count"] == 0

    # Delete entries with tag "c"
    result = fiche_memory_delete(tags=["c"])
    assert result["ok"] is True
    assert result["data"]["deleted_count"] == 2

    # Verify k1 and k4 remain
    all_result = fiche_memory_get()
    assert all_result["data"]["count"] == 2
    keys = [entry["key"] for entry in all_result["data"]["entries"]]
    assert "k1" in keys
    assert "k4" in keys


def test_complete_workflow(credential_context):
    """Test complete memory management workflow."""
    # Store user preferences
    result = fiche_memory_set(
        key="user_prefs",
        value={"theme": "dark", "notifications": True},
        tags=["settings", "ui"],
    )
    assert result["ok"] is True

    # Store some temporary data
    fiche_memory_set(
        key="session_cache",
        value={"data": "temp"},
        tags=["temporary"],
        expires_at="2025-12-31T23:59:59Z",
    )

    # Store computed results
    fiche_memory_set(
        key="computation_result",
        value={"result": 42, "timestamp": "2025-01-01"},
        tags=["cache", "computation"],
    )

    # List all settings
    settings_result = fiche_memory_get(tags=["settings"])
    assert settings_result["ok"] is True
    assert settings_result["data"]["count"] == 1

    # Export everything
    export_result = fiche_memory_export()
    assert export_result["ok"] is True
    assert export_result["data"]["count"] == 3

    # Update user preferences
    update_result = fiche_memory_set(
        key="user_prefs",
        value={"theme": "light", "notifications": False},
        tags=["settings", "ui", "modified"],
    )
    assert update_result["ok"] is True

    # Verify update
    get_result = fiche_memory_get(key="user_prefs")
    assert get_result["ok"] is True
    assert get_result["data"]["value"]["theme"] == "light"

    # Clean up temporary data
    delete_result = fiche_memory_delete(tags=["temporary"])
    assert delete_result["ok"] is True
    assert delete_result["data"]["deleted_count"] == 1

    # Verify final state
    final_export = fiche_memory_export()
    assert final_export["ok"] is True
    assert final_export["data"]["count"] == 2
