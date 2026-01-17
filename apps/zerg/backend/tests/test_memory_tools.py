"""Tests for memory file tools."""

import pytest

from zerg.connectors.context import set_credential_resolver
from zerg.connectors.resolver import CredentialResolver
from zerg.tools.builtin.memory_tools import memory_delete
from zerg.tools.builtin.memory_tools import memory_ls
from zerg.tools.builtin.memory_tools import memory_read
from zerg.tools.builtin.memory_tools import memory_search
from zerg.tools.builtin.memory_tools import memory_write


@pytest.fixture
def credential_context(db_session, test_user):
    """Set up credential resolver context for memory tools."""
    resolver = CredentialResolver(agent_id=1, db=db_session, owner_id=test_user.id)
    token = set_credential_resolver(resolver)
    yield resolver
    set_credential_resolver(None)


def test_memory_write_and_read(credential_context):
    """memory_write should persist content; memory_read should retrieve it."""
    write_result = memory_write(
        path="episodes/2026-01-17/test.md",
        content="Hello memory",
        tags=["test"],
        metadata={"run_id": 42},
    )

    assert write_result["ok"] is True
    assert write_result["data"]["path"] == "episodes/2026-01-17/test.md"

    read_result = memory_read(path="episodes/2026-01-17/test.md")
    assert read_result["ok"] is True
    assert read_result["data"]["content"] == "Hello memory"
    assert read_result["data"]["tags"] == ["test"]
    assert read_result["data"]["metadata"]["run_id"] == 42


def test_memory_write_overwrites_existing(credential_context):
    """memory_write should overwrite existing file content."""
    memory_write(path="episodes/2026-01-17/overwrite.md", content="v1")
    updated = memory_write(path="episodes/2026-01-17/overwrite.md", content="v2", tags=["updated"])

    assert updated["ok"] is True
    assert updated["data"]["content"] == "v2"
    assert updated["data"]["tags"] == ["updated"]


def test_memory_ls_prefix(credential_context):
    """memory_ls should list only matching prefixes."""
    memory_write(path="episodes/2026-01-01/a.md", content="a")
    memory_write(path="episodes/2026-01-02/b.md", content="b")
    memory_write(path="projects/hdrpop/status.md", content="c")

    result = memory_ls(prefix="episodes/")
    assert result["ok"] is True
    paths = {row["path"] for row in result["data"]["files"]}
    assert paths == {"episodes/2026-01-01/a.md", "episodes/2026-01-02/b.md"}


def test_memory_search_basic(credential_context):
    """memory_search should return snippets containing the query."""
    memory_write(path="episodes/2026-01-17/search.md", content="Runner exec failed on cube")

    result = memory_search(query="runner exec", limit=5, use_embeddings=False)
    assert result["ok"] is True
    assert len(result["data"]["results"]) >= 1
    assert "runner" in " ".join(result["data"]["results"][0]["snippets"]).lower()


def test_memory_delete(credential_context):
    """memory_delete should remove a file."""
    memory_write(path="episodes/2026-01-17/delete.md", content="remove me")
    deleted = memory_delete(path="episodes/2026-01-17/delete.md")
    assert deleted["ok"] is True
    assert deleted["data"]["deleted"] is True

    read_after = memory_read(path="episodes/2026-01-17/delete.md")
    assert read_after["ok"] is False


def test_memory_write_requires_context():
    """memory_write should fail without user context."""
    result = memory_write(path="episodes/2026-01-17/noctx.md", content="nope")
    assert result["ok"] is False
    assert result["error_type"] == "execution_error"
