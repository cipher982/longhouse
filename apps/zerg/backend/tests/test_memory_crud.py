"""Tests for Memory Files CRUD + Embedding search."""

import numpy as np

from zerg.crud import memory_crud
from zerg.services import memory_embeddings
from zerg.services import memory_search


def test_memory_upsert_and_get(db_session, test_user):
    """Upsert should create a new memory file and allow retrieval by path."""
    path = "episodes/2026-01-17/test.md"
    created = memory_crud.upsert_memory_file(
        db_session,
        owner_id=test_user.id,
        path=path,
        title="Test Episode",
        content="We debugged runner_exec auth.",
        tags=["zerg", "infra"],
        metadata={"course_id": 123},
    )

    assert created.id is not None
    assert created.owner_id == test_user.id
    assert created.path == path
    assert created.title == "Test Episode"
    assert created.content == "We debugged runner_exec auth."
    assert created.tags == ["zerg", "infra"]
    assert created.file_metadata["course_id"] == 123

    fetched = memory_crud.get_memory_file_by_path(db_session, owner_id=test_user.id, path=path)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.content == "We debugged runner_exec auth."


def test_memory_upsert_updates_existing(db_session, test_user):
    """Upsert should overwrite content/tags when path already exists."""
    path = "episodes/2026-01-17/update.md"
    original = memory_crud.upsert_memory_file(
        db_session,
        owner_id=test_user.id,
        path=path,
        title="Original",
        content="v1 content",
        tags=["alpha"],
    )

    updated = memory_crud.upsert_memory_file(
        db_session,
        owner_id=test_user.id,
        path=path,
        title="Updated",
        content="v2 content",
        tags=["beta"],
    )

    assert updated.id == original.id
    assert updated.title == "Updated"
    assert updated.content == "v2 content"
    assert updated.tags == ["beta"]


def test_memory_list_prefix(db_session, test_user):
    """List should filter by prefix and return only matching paths."""
    memory_crud.upsert_memory_file(
        db_session,
        owner_id=test_user.id,
        path="episodes/2026-01-01/a.md",
        content="episode a",
    )
    memory_crud.upsert_memory_file(
        db_session,
        owner_id=test_user.id,
        path="episodes/2026-01-02/b.md",
        content="episode b",
    )
    memory_crud.upsert_memory_file(
        db_session,
        owner_id=test_user.id,
        path="projects/hdrpop/status.md",
        content="project status",
    )

    results = memory_crud.list_memory_files(db_session, owner_id=test_user.id, prefix="episodes/")
    paths = {row.path for row in results}
    assert paths == {"episodes/2026-01-01/a.md", "episodes/2026-01-02/b.md"}


def test_memory_delete(db_session, test_user):
    """Delete should remove the memory file by path."""
    path = "episodes/2026-01-17/delete.md"
    memory_crud.upsert_memory_file(
        db_session,
        owner_id=test_user.id,
        path=path,
        content="delete me",
    )

    deleted = memory_crud.delete_memory_file(db_session, owner_id=test_user.id, path=path)
    assert deleted is True

    fetched = memory_crud.get_memory_file_by_path(db_session, owner_id=test_user.id, path=path)
    assert fetched is None


def test_memory_embeddings_search_orders_results(db_session, test_user):
    """Embedding search should order results by similarity."""
    file_a = memory_crud.upsert_memory_file(
        db_session,
        owner_id=test_user.id,
        path="episodes/2026-01-17/a.md",
        content="runner_exec auth issue",
    )
    file_b = memory_crud.upsert_memory_file(
        db_session,
        owner_id=test_user.id,
        path="episodes/2026-01-17/b.md",
        content="email triage notes",
    )

    vec_a = np.array([1.0, 0.0], dtype=np.float32)
    vec_b = np.array([0.0, 1.0], dtype=np.float32)

    memory_embeddings.upsert_memory_embedding(
        db_session,
        owner_id=test_user.id,
        memory_file_id=file_a.id,
        model="test-embed",
        embedding=vec_a,
    )
    memory_embeddings.upsert_memory_embedding(
        db_session,
        owner_id=test_user.id,
        memory_file_id=file_b.id,
        model="test-embed",
        embedding=vec_b,
    )

    query_vec = np.array([1.0, 0.0], dtype=np.float32)
    results = memory_embeddings.search_memory_embeddings(
        db_session,
        owner_id=test_user.id,
        query_embedding=query_vec,
        limit=2,
    )

    assert results[0][0] == file_a.id
    assert results[0][1] > results[1][1]


def test_memory_search_keyword_fallback(db_session, test_user):
    """Memory search should fall back to keyword search when embeddings are disabled."""
    file_a = memory_crud.upsert_memory_file(
        db_session,
        owner_id=test_user.id,
        path="episodes/2026-01-17/keyword.md",
        content="runner_exec auth issue",
        tags=["infra"],
    )

    results = memory_search.search_memory_files(
        db_session,
        owner_id=test_user.id,
        query="runner_exec",
        limit=3,
        use_embeddings=False,
    )

    assert len(results) >= 1
    assert results[0]["path"] == file_a.path
    assert "runner_exec" in " ".join(results[0]["snippets"])
