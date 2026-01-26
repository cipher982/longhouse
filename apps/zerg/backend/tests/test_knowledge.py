"""Tests for Knowledge Base (Phase 0)."""

from unittest.mock import AsyncMock
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy.orm import Session

from zerg.crud import knowledge_crud
from zerg.models.models import User
from zerg.services import knowledge_sync_service

# ---------------------------------------------------------------------------
# CRUD Tests
# ---------------------------------------------------------------------------


class TestKnowledgeSourceCRUD:
    """Tests for KnowledgeSource CRUD operations."""

    def test_create_knowledge_source(self, db_session: Session, _dev_user: User):
        """Test creating a new knowledge source."""
        source = knowledge_crud.create_knowledge_source(
            db_session,
            owner_id=_dev_user.id,
            name="Test Docs",
            source_type="url",
            config={"url": "https://example.com/docs.md"},
            sync_schedule="0 * * * *",
        )

        assert source.id is not None
        assert source.owner_id == _dev_user.id
        assert source.name == "Test Docs"
        assert source.source_type == "url"
        assert source.config["url"] == "https://example.com/docs.md"
        assert source.sync_schedule == "0 * * * *"
        assert source.sync_status == "pending"

    def test_get_knowledge_source(self, db_session: Session, _dev_user: User):
        """Test retrieving a knowledge source by ID."""
        source = knowledge_crud.create_knowledge_source(
            db_session,
            owner_id=_dev_user.id,
            name="Test Docs",
            source_type="url",
            config={"url": "https://example.com/docs.md"},
        )

        retrieved = knowledge_crud.get_knowledge_source(db_session, source.id)
        assert retrieved is not None
        assert retrieved.id == source.id
        assert retrieved.name == "Test Docs"

    def test_get_knowledge_source_not_found(self, db_session: Session):
        """Test retrieving a non-existent knowledge source."""
        result = knowledge_crud.get_knowledge_source(db_session, 99999)
        assert result is None

    def test_get_knowledge_sources(self, db_session: Session, _dev_user: User):
        """Test listing knowledge sources for a user."""
        # Create multiple sources
        for i in range(3):
            knowledge_crud.create_knowledge_source(
                db_session,
                owner_id=_dev_user.id,
                name=f"Test Docs {i}",
                source_type="url",
                config={"url": f"https://example.com/docs{i}.md"},
            )

        sources = knowledge_crud.get_knowledge_sources(db_session, owner_id=_dev_user.id)
        assert len(sources) == 3

        # Test pagination
        page1 = knowledge_crud.get_knowledge_sources(db_session, owner_id=_dev_user.id, skip=0, limit=2)
        page2 = knowledge_crud.get_knowledge_sources(db_session, owner_id=_dev_user.id, skip=2, limit=2)
        assert len(page1) == 2
        assert len(page2) == 1

    def test_update_knowledge_source(self, db_session: Session, _dev_user: User):
        """Test updating a knowledge source."""
        source = knowledge_crud.create_knowledge_source(
            db_session,
            owner_id=_dev_user.id,
            name="Original Name",
            source_type="url",
            config={"url": "https://example.com/docs.md"},
        )

        updated = knowledge_crud.update_knowledge_source(
            db_session,
            source.id,
            name="Updated Name",
            config={"url": "https://example.com/new-docs.md"},
        )

        assert updated is not None
        assert updated.name == "Updated Name"
        assert updated.config["url"] == "https://example.com/new-docs.md"

    def test_delete_knowledge_source(self, db_session: Session, _dev_user: User):
        """Test deleting a knowledge source."""
        source = knowledge_crud.create_knowledge_source(
            db_session,
            owner_id=_dev_user.id,
            name="To Delete",
            source_type="url",
            config={"url": "https://example.com/docs.md"},
        )

        result = knowledge_crud.delete_knowledge_source(db_session, source.id)
        assert result is True

        # Verify it's deleted
        retrieved = knowledge_crud.get_knowledge_source(db_session, source.id)
        assert retrieved is None

    def test_delete_knowledge_source_not_found(self, db_session: Session):
        """Test deleting a non-existent knowledge source."""
        result = knowledge_crud.delete_knowledge_source(db_session, 99999)
        assert result is False

    def test_update_source_sync_status(self, db_session: Session, _dev_user: User):
        """Test updating sync status for a source."""
        source = knowledge_crud.create_knowledge_source(
            db_session,
            owner_id=_dev_user.id,
            name="Test Source",
            source_type="url",
            config={"url": "https://example.com/docs.md"},
        )

        # Update to success
        updated = knowledge_crud.update_source_sync_status(
            db_session,
            source.id,
            status="success",
        )
        assert updated.sync_status == "success"
        assert updated.last_synced_at is not None
        assert updated.sync_error is None

        # Update to failed
        updated = knowledge_crud.update_source_sync_status(
            db_session,
            source.id,
            status="failed",
            error="Connection timeout",
        )
        assert updated.sync_status == "failed"
        assert updated.sync_error == "Connection timeout"


class TestKnowledgeDocumentCRUD:
    """Tests for KnowledgeDocument CRUD operations."""

    def test_upsert_knowledge_document_create(self, db_session: Session, _dev_user: User):
        """Test creating a new document via upsert."""
        source = knowledge_crud.create_knowledge_source(
            db_session,
            owner_id=_dev_user.id,
            name="Test Source",
            source_type="url",
            config={"url": "https://example.com/docs.md"},
        )

        doc = knowledge_crud.upsert_knowledge_document(
            db_session,
            source_id=source.id,
            owner_id=_dev_user.id,
            path="https://example.com/docs.md",
            content_text="# Test Document\n\nThis is test content.",
            title="Test Document",
            doc_metadata={"content_type": "text/markdown"},
        )

        assert doc.id is not None
        assert doc.source_id == source.id
        assert doc.owner_id == _dev_user.id
        assert doc.path == "https://example.com/docs.md"
        assert doc.title == "Test Document"
        assert "# Test Document" in doc.content_text
        assert doc.content_hash is not None
        assert len(doc.content_hash) == 64  # SHA-256 hex

    def test_upsert_knowledge_document_update(self, db_session: Session, _dev_user: User):
        """Test updating an existing document via upsert."""
        source = knowledge_crud.create_knowledge_source(
            db_session,
            owner_id=_dev_user.id,
            name="Test Source",
            source_type="url",
            config={"url": "https://example.com/docs.md"},
        )

        # Create initial document
        doc1 = knowledge_crud.upsert_knowledge_document(
            db_session,
            source_id=source.id,
            owner_id=_dev_user.id,
            path="https://example.com/docs.md",
            content_text="Original content",
            title="Original Title",
        )
        original_hash = doc1.content_hash

        # Update via upsert
        doc2 = knowledge_crud.upsert_knowledge_document(
            db_session,
            source_id=source.id,
            owner_id=_dev_user.id,
            path="https://example.com/docs.md",
            content_text="Updated content",
            title="Updated Title",
        )

        # Should be the same row
        assert doc2.id == doc1.id
        assert doc2.content_text == "Updated content"
        assert doc2.title == "Updated Title"
        assert doc2.content_hash != original_hash

    def test_get_knowledge_documents(self, db_session: Session, _dev_user: User):
        """Test listing documents for a user."""
        source = knowledge_crud.create_knowledge_source(
            db_session,
            owner_id=_dev_user.id,
            name="Test Source",
            source_type="url",
            config={"url": "https://example.com/"},
        )

        # Create multiple documents
        for i in range(3):
            knowledge_crud.upsert_knowledge_document(
                db_session,
                source_id=source.id,
                owner_id=_dev_user.id,
                path=f"https://example.com/doc{i}.md",
                content_text=f"Content {i}",
            )

        docs = knowledge_crud.get_knowledge_documents(db_session, owner_id=_dev_user.id)
        assert len(docs) == 3

    def test_search_knowledge_documents(self, db_session: Session, _dev_user: User):
        """Test searching documents by keyword."""
        source = knowledge_crud.create_knowledge_source(
            db_session,
            owner_id=_dev_user.id,
            name="Infrastructure Docs",
            source_type="url",
            config={"url": "https://example.com/"},
        )

        # Create documents with different content
        knowledge_crud.upsert_knowledge_document(
            db_session,
            source_id=source.id,
            owner_id=_dev_user.id,
            path="https://example.com/servers.md",
            content_text="cube (100.70.237.79) - Home GPU server for AI workloads",
            title="Server Overview",
        )

        knowledge_crud.upsert_knowledge_document(
            db_session,
            source_id=source.id,
            owner_id=_dev_user.id,
            path="https://example.com/backups.md",
            content_text="Backups run nightly using Kopia to Bremen NAS",
            title="Backup Guide",
        )

        # Search for "cube"
        results = knowledge_crud.search_knowledge_documents(
            db_session,
            owner_id=_dev_user.id,
            query="cube",
        )
        assert len(results) == 1
        doc, source = results[0]
        assert "cube" in doc.content_text.lower()

        # Search for "GPU"
        results = knowledge_crud.search_knowledge_documents(
            db_session,
            owner_id=_dev_user.id,
            query="GPU",
        )
        assert len(results) == 1

        # Search for something that doesn't exist
        results = knowledge_crud.search_knowledge_documents(
            db_session,
            owner_id=_dev_user.id,
            query="nonexistent",
        )
        assert len(results) == 0


# ---------------------------------------------------------------------------
# Sync Service Tests
# ---------------------------------------------------------------------------


class TestKnowledgeSyncService:
    """Tests for KnowledgeSyncService."""

    @pytest.mark.asyncio
    async def test_sync_url_source_success(self, db_session: Session, _dev_user: User):
        """Test successful URL sync."""
        source = knowledge_crud.create_knowledge_source(
            db_session,
            owner_id=_dev_user.id,
            name="Test URL",
            source_type="url",
            config={"url": "https://example.com/docs.md"},
        )

        mock_response = AsyncMock()
        mock_response.text = "# Test Document\n\nThis is test content."
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/markdown"}
        mock_response.raise_for_status = lambda: None

        with patch.object(httpx.AsyncClient, "get", return_value=mock_response):
            await knowledge_sync_service.sync_url_source(db_session, source)

        # Verify source status updated
        db_session.refresh(source)
        assert source.sync_status == "success"
        assert source.last_synced_at is not None

        # Verify document created
        docs = knowledge_crud.get_knowledge_documents(
            db_session,
            owner_id=_dev_user.id,
            source_id=source.id,
        )
        assert len(docs) == 1
        assert "# Test Document" in docs[0].content_text

    @pytest.mark.asyncio
    async def test_sync_url_source_with_auth(self, db_session: Session, _dev_user: User):
        """Test URL sync with auth header."""
        source = knowledge_crud.create_knowledge_source(
            db_session,
            owner_id=_dev_user.id,
            name="Private Docs",
            source_type="url",
            config={
                "url": "https://example.com/private/docs.md",
                "auth_header": "Bearer secret-token",
            },
        )

        mock_response = AsyncMock()
        mock_response.text = "Private content"
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/plain"}
        mock_response.raise_for_status = lambda: None

        with patch.object(httpx.AsyncClient, "get", return_value=mock_response) as mock_get:
            await knowledge_sync_service.sync_url_source(db_session, source)

            # Verify Authorization header was sent
            call_args = mock_get.call_args
            assert call_args.kwargs["headers"]["Authorization"] == "Bearer secret-token"

    @pytest.mark.asyncio
    async def test_sync_url_source_http_error(self, db_session: Session, _dev_user: User):
        """Test URL sync with HTTP error."""
        source = knowledge_crud.create_knowledge_source(
            db_session,
            owner_id=_dev_user.id,
            name="Bad URL",
            source_type="url",
            config={"url": "https://example.com/notfound.md"},
        )

        with patch.object(
            httpx.AsyncClient,
            "get",
            side_effect=httpx.HTTPStatusError("404 Not Found", request=None, response=None),
        ):
            with pytest.raises(httpx.HTTPError):
                await knowledge_sync_service.sync_url_source(db_session, source)

        # Verify source status updated to failed
        db_session.refresh(source)
        assert source.sync_status == "failed"
        assert "404" in source.sync_error

    @pytest.mark.asyncio
    async def test_sync_url_source_wrong_type(self, db_session: Session, _dev_user: User):
        """Test that sync_url_source rejects non-URL sources."""
        source = knowledge_crud.create_knowledge_source(
            db_session,
            owner_id=_dev_user.id,
            name="Git Repo",
            source_type="git_repo",  # Not URL
            config={"repo_url": "https://github.com/test/repo.git"},
        )

        with pytest.raises(ValueError, match="Expected source_type='url'"):
            await knowledge_sync_service.sync_url_source(db_session, source)

    @pytest.mark.asyncio
    async def test_sync_knowledge_source(self, db_session: Session, _dev_user: User):
        """Test sync dispatcher."""
        source = knowledge_crud.create_knowledge_source(
            db_session,
            owner_id=_dev_user.id,
            name="Test URL",
            source_type="url",
            config={"url": "https://example.com/docs.md"},
        )

        mock_response = AsyncMock()
        mock_response.text = "Content"
        mock_response.status_code = 200
        mock_response.headers = {}
        mock_response.raise_for_status = lambda: None

        with patch.object(httpx.AsyncClient, "get", return_value=mock_response):
            await knowledge_sync_service.sync_knowledge_source(db_session, source.id)

        db_session.refresh(source)
        assert source.sync_status == "success"

    @pytest.mark.asyncio
    async def test_sync_user_text_source(self, db_session: Session, _dev_user: User):
        """Test syncing a user_text source."""
        source = knowledge_crud.create_knowledge_source(
            db_session,
            owner_id=_dev_user.id,
            name="User Notes",
            source_type="user_text",
            config={"content": "Remember to rotate keys."},
        )

        await knowledge_sync_service.sync_knowledge_source(db_session, source.id)
        db_session.refresh(source)
        assert source.sync_status == "success"

        docs = knowledge_crud.get_knowledge_documents(db_session, owner_id=_dev_user.id, source_id=source.id)
        assert len(docs) == 1
        assert docs[0].content_text == "Remember to rotate keys."

    @pytest.mark.asyncio
    async def test_sync_knowledge_source_not_found(self, db_session: Session):
        """Test sync with non-existent source."""
        with pytest.raises(ValueError, match="not found"):
            await knowledge_sync_service.sync_knowledge_source(db_session, 99999)


# ---------------------------------------------------------------------------
# Tool Tests
# ---------------------------------------------------------------------------


class TestKnowledgeTools:
    """Tests for knowledge_search tool."""

    def test_extract_snippets_exact_match(self):
        """Test snippet extraction with exact match."""
        from zerg.tools.builtin.knowledge_tools import extract_snippets

        text = "The cube server (100.70.237.79) is used for AI workloads."
        snippets = extract_snippets(text, "cube", max_snippets=3)

        assert len(snippets) == 1
        assert "cube" in snippets[0].lower()

    def test_extract_snippets_multiple_matches(self):
        """Test snippet extraction with multiple matches."""
        from zerg.tools.builtin.knowledge_tools import extract_snippets

        text = "cube is great. cube is fast. cube is powerful."
        snippets = extract_snippets(text, "cube", max_snippets=2)

        assert len(snippets) == 2

    def test_extract_snippets_word_fallback(self):
        """Test snippet extraction falls back to word matching."""
        from zerg.tools.builtin.knowledge_tools import extract_snippets

        text = "The server is fast.\nIt runs GPU workloads.\nVery efficient."
        snippets = extract_snippets(text, "GPU server", max_snippets=3)

        # Should find lines containing either "GPU" or "server"
        assert len(snippets) >= 1


# ---------------------------------------------------------------------------
# API Tests
# ---------------------------------------------------------------------------


class TestKnowledgeAPI:
    """Tests for knowledge API endpoints."""

    def test_create_source(self, client, _dev_user: User):
        """Test POST /api/knowledge/sources."""
        response = client.post(
            "/api/knowledge/sources",
            json={
                "name": "Test Docs",
                "source_type": "url",
                "config": {"url": "https://example.com/docs.md"},
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Test Docs"
        assert data["source_type"] == "url"
        assert data["sync_status"] == "pending"

    def test_create_source_user_text(self, client, db_session: Session, _dev_user: User):
        """Test creating a user_text knowledge source."""
        response = client.post(
            "/api/knowledge/sources",
            json={
                "name": "My Notes",
                "source_type": "user_text",
                "config": {"content": "Important context"},
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "My Notes"
        assert data["source_type"] == "user_text"
        assert data["sync_status"] == "success"

        docs = knowledge_crud.get_knowledge_documents(db_session, owner_id=_dev_user.id, source_id=data["id"])
        assert len(docs) == 1
        assert docs[0].content_text == "Important context"

    def test_create_source_invalid_type(self, client, _dev_user: User):
        """Test creating source with unsupported type."""
        response = client.post(
            "/api/knowledge/sources",
            json={
                "name": "Git Repo",
                "source_type": "gitlab_repo",  # Not supported
                "config": {"repo_url": "https://gitlab.com/test/repo.git"},
            },
        )
        assert response.status_code == 400
        assert "Unsupported source_type" in response.json()["detail"]

    def test_create_source_user_text_missing_content(self, client, _dev_user: User):
        """Test creating user_text source without content."""
        response = client.post(
            "/api/knowledge/sources",
            json={
                "name": "Empty Notes",
                "source_type": "user_text",
                "config": {},
            },
        )
        assert response.status_code == 400
        assert "content" in response.json()["detail"].lower()

    def test_create_source_missing_url(self, client, _dev_user: User):
        """Test creating URL source without URL in config."""
        response = client.post(
            "/api/knowledge/sources",
            json={
                "name": "Bad Config",
                "source_type": "url",
                "config": {},  # Missing "url"
            },
        )
        assert response.status_code == 400
        assert "url" in response.json()["detail"].lower()

    def test_create_source_invalid_url_scheme(self, client, _dev_user: User):
        """Test creating URL source with dangerous URL scheme (javascript:, data:, etc.)."""
        # Test javascript: scheme
        response = client.post(
            "/api/knowledge/sources",
            json={
                "name": "Malicious Source",
                "source_type": "url",
                "config": {"url": "javascript:alert('xss')"},
            },
        )
        assert response.status_code == 400
        assert "scheme" in response.json()["detail"].lower()
        assert "http" in response.json()["detail"].lower()

        # Test data: scheme
        response = client.post(
            "/api/knowledge/sources",
            json={
                "name": "Data URI Source",
                "source_type": "url",
                "config": {"url": "data:text/html,<script>alert('xss')</script>"},
            },
        )
        assert response.status_code == 400
        assert "scheme" in response.json()["detail"].lower()

        # Test valid https: scheme (should succeed)
        response = client.post(
            "/api/knowledge/sources",
            json={
                "name": "Valid HTTPS Source",
                "source_type": "url",
                "config": {"url": "https://example.com/docs.md"},
            },
        )
        assert response.status_code == 201

    def test_list_sources(self, client, db_session: Session, _dev_user: User):
        """Test GET /api/knowledge/sources."""
        # Create some sources
        for i in range(3):
            knowledge_crud.create_knowledge_source(
                db_session,
                owner_id=_dev_user.id,
                name=f"Source {i}",
                source_type="url",
                config={"url": f"https://example.com/doc{i}.md"},
            )

        response = client.get("/api/knowledge/sources")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 3

    def test_get_source(self, client, db_session: Session, _dev_user: User):
        """Test GET /api/knowledge/sources/{id}."""
        source = knowledge_crud.create_knowledge_source(
            db_session,
            owner_id=_dev_user.id,
            name="Test Source",
            source_type="url",
            config={"url": "https://example.com/docs.md"},
        )

        response = client.get(f"/api/knowledge/sources/{source.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Test Source"

    def test_get_source_not_found(self, client):
        """Test GET with non-existent source."""
        response = client.get("/api/knowledge/sources/99999")
        assert response.status_code == 404

    def test_update_source(self, client, db_session: Session, _dev_user: User):
        """Test PUT /api/knowledge/sources/{id}."""
        source = knowledge_crud.create_knowledge_source(
            db_session,
            owner_id=_dev_user.id,
            name="Original Name",
            source_type="url",
            config={"url": "https://example.com/docs.md"},
        )

        response = client.put(
            f"/api/knowledge/sources/{source.id}",
            json={"name": "Updated Name"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Updated Name"

    def test_delete_source(self, client, db_session: Session, _dev_user: User):
        """Test DELETE /api/knowledge/sources/{id}."""
        source = knowledge_crud.create_knowledge_source(
            db_session,
            owner_id=_dev_user.id,
            name="To Delete",
            source_type="url",
            config={"url": "https://example.com/docs.md"},
        )

        response = client.delete(f"/api/knowledge/sources/{source.id}")
        assert response.status_code == 204

        # Verify deleted
        response = client.get(f"/api/knowledge/sources/{source.id}")
        assert response.status_code == 404

    def test_search(self, client, db_session: Session, _dev_user: User):
        """Test GET /api/knowledge/search."""
        source = knowledge_crud.create_knowledge_source(
            db_session,
            owner_id=_dev_user.id,
            name="Infra Docs",
            source_type="url",
            config={"url": "https://example.com/"},
        )

        knowledge_crud.upsert_knowledge_document(
            db_session,
            source_id=source.id,
            owner_id=_dev_user.id,
            path="https://example.com/servers.md",
            content_text="cube (100.70.237.79) - Home GPU server",
            title="Servers",
        )

        response = client.get("/api/knowledge/search", params={"q": "cube"})
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["source_name"] == "Infra Docs"
        assert len(data[0]["snippets"]) > 0

    @pytest.mark.asyncio
    async def test_sync_source_success(self, client, db_session: Session, _dev_user: User):
        """Test POST /api/knowledge/sources/{id}/sync - success case."""
        source = knowledge_crud.create_knowledge_source(
            db_session,
            owner_id=_dev_user.id,
            name="Test URL",
            source_type="url",
            config={"url": "https://example.com/docs.md"},
        )

        # Mock the sync service to simulate success
        with patch("zerg.routers.knowledge.knowledge_sync_service.sync_knowledge_source") as mock_sync:
            mock_sync.return_value = None  # Sync succeeds (no exception)
            # Manually update status as the service would
            knowledge_crud.update_source_sync_status(db_session, source.id, status="success")

            response = client.post(f"/api/knowledge/sources/{source.id}/sync")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == source.id
        assert data["sync_status"] == "success"

    @pytest.mark.asyncio
    async def test_sync_source_failure(self, client, db_session: Session, _dev_user: User):
        """Test POST /api/knowledge/sources/{id}/sync - failure case.

        When sync fails, the endpoint should still return 200 with the updated source
        showing sync_status='failed' and sync_error populated.
        """
        source = knowledge_crud.create_knowledge_source(
            db_session,
            owner_id=_dev_user.id,
            name="Bad URL",
            source_type="url",
            config={"url": "https://example.com/nonexistent.md"},
        )

        # Mock sync to raise an exception (simulate sync failure)
        with patch("zerg.routers.knowledge.knowledge_sync_service.sync_knowledge_source") as mock_sync:
            mock_sync.side_effect = Exception("Connection refused")
            # Manually update status as the service would on failure
            knowledge_crud.update_source_sync_status(db_session, source.id, status="failed", error="Connection refused")

            response = client.post(f"/api/knowledge/sources/{source.id}/sync")

        # Should still return 200, but with failed status
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == source.id
        assert data["sync_status"] == "failed"
        assert data["sync_error"] == "Connection refused"

    def test_sync_source_not_found(self, client):
        """Test POST /api/knowledge/sources/{id}/sync with non-existent source."""
        response = client.post("/api/knowledge/sources/99999/sync")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Knowledge Search Tool Context Tests (V1.1)
# ---------------------------------------------------------------------------


class TestKnowledgeSearchToolContext:
    """Tests for knowledge_search tool context resolution (V1.1)."""

    def test_knowledge_search_with_commis_context(self, db_session: Session, _dev_user: User):
        """Test knowledge_search resolves owner_id from CommisContext."""
        from zerg.context import CommisContext
        from zerg.context import reset_commis_context
        from zerg.context import set_commis_context
        from zerg.tools.builtin.knowledge_tools import knowledge_search

        # Create a source with documents
        source = knowledge_crud.create_knowledge_source(
            db_session,
            owner_id=_dev_user.id,
            name="Test Infra Docs",
            source_type="url",
            config={"url": "https://example.com/"},
        )

        knowledge_crud.upsert_knowledge_document(
            db_session,
            source_id=source.id,
            owner_id=_dev_user.id,
            path="https://example.com/servers.md",
            content_text="cube (100.70.237.79) - Home GPU server for AI workloads",
            title="Server Overview",
        )

        # Set up commis context with the user's owner_id
        ctx = CommisContext(
            commis_id="test-commis-123",
            owner_id=_dev_user.id,
            course_id="test-run-123",
        )
        token = set_commis_context(ctx)

        try:
            # Call knowledge_search - should resolve owner_id from context
            results = knowledge_search("cube", limit=5)

            # Should find the document
            assert isinstance(results, list)
            assert len(results) == 1
            assert results[0]["source"] == "Test Infra Docs"
            assert "cube" in results[0]["snippets"][0].lower()
        finally:
            reset_commis_context(token)

    def test_knowledge_search_without_context_returns_error(self, db_session: Session, _dev_user: User):
        """Test knowledge_search returns structured error without context."""
        from zerg.tools.builtin.knowledge_tools import knowledge_search

        # No commis context set - should return error
        results = knowledge_search("anything", limit=5)

        assert isinstance(results, list)
        assert len(results) == 1
        assert "error" in results[0]
        assert "context" in results[0]["error"].lower()

    def test_knowledge_search_no_results(self, db_session: Session, _dev_user: User):
        """Test knowledge_search with no matching documents."""
        from zerg.context import CommisContext
        from zerg.context import reset_commis_context
        from zerg.context import set_commis_context
        from zerg.tools.builtin.knowledge_tools import knowledge_search

        # Create a source but no matching documents
        source = knowledge_crud.create_knowledge_source(
            db_session,
            owner_id=_dev_user.id,
            name="Empty Source",
            source_type="url",
            config={"url": "https://example.com/"},
        )

        knowledge_crud.upsert_knowledge_document(
            db_session,
            source_id=source.id,
            owner_id=_dev_user.id,
            path="https://example.com/unrelated.md",
            content_text="This document has no matching keywords",
            title="Unrelated",
        )

        ctx = CommisContext(
            commis_id="test-commis-123",
            owner_id=_dev_user.id,
            course_id="test-run-123",
        )
        token = set_commis_context(ctx)

        try:
            results = knowledge_search("nonexistent_query_xyz", limit=5)

            # Should return "no results" message, not empty list
            assert isinstance(results, list)
            assert len(results) == 1
            assert "message" in results[0]
            assert "no results" in results[0]["message"].lower()
        finally:
            reset_commis_context(token)

    def test_knowledge_search_with_credential_resolver_context(self, db_session: Session, _dev_user: User):
        """Test knowledge_search resolves owner_id from CredentialResolver (Concierge path).

        V1.1: This tests the fallback to CredentialResolver when CommisContext is not set,
        which is the case for Concierge runs (FicheRunner sets CredentialResolver, not CommisContext).
        """
        from zerg.connectors.context import reset_credential_resolver
        from zerg.connectors.context import set_credential_resolver
        from zerg.connectors.resolver import CredentialResolver
        from zerg.tools.builtin.knowledge_tools import knowledge_search

        # Create a source with documents
        source = knowledge_crud.create_knowledge_source(
            db_session,
            owner_id=_dev_user.id,
            name="Concierge Test Docs",
            source_type="url",
            config={"url": "https://example.com/"},
        )

        knowledge_crud.upsert_knowledge_document(
            db_session,
            source_id=source.id,
            owner_id=_dev_user.id,
            path="https://example.com/concierge-doc.md",
            content_text="clifford (100.120.197.80) - Primary VPS for production apps",
            title="Server Overview",
        )

        # Set up CredentialResolver context (as FicheRunner does for Concierge runs)
        # Note: We use a mock fiche_id since we just need owner_id
        resolver = CredentialResolver(
            fiche_id=999,  # Doesn't need to exist
            db=db_session,
            owner_id=_dev_user.id,
        )
        token = set_credential_resolver(resolver)

        try:
            # Call knowledge_search - should resolve owner_id from CredentialResolver
            results = knowledge_search("clifford", limit=5)

            # Should find the document
            assert isinstance(results, list)
            assert len(results) == 1
            assert results[0]["source"] == "Concierge Test Docs"
            assert "clifford" in results[0]["snippets"][0].lower()
        finally:
            reset_credential_resolver(token)

    def test_knowledge_search_prefers_commis_context_over_resolver(self, db_session: Session, _dev_user: User):
        """Test knowledge_search prefers CommisContext when both contexts are set.

        V1.1: When both contexts are available (edge case), CommisContext should take precedence.
        Uses different owner_ids to prove CommisContext is actually used.
        """
        from zerg.connectors.context import reset_credential_resolver
        from zerg.connectors.context import set_credential_resolver
        from zerg.connectors.resolver import CredentialResolver
        from zerg.context import CommisContext
        from zerg.context import reset_commis_context
        from zerg.context import set_commis_context
        from zerg.models.models import User as UserModel
        from zerg.tools.builtin.knowledge_tools import knowledge_search

        # Create a second user to prove CommisContext takes precedence
        other_user = UserModel(
            email="other-user@test.com",
            is_active=True,
        )
        db_session.add(other_user)
        db_session.flush()

        # Create source for _dev_user (CommisContext user)
        commis_source = knowledge_crud.create_knowledge_source(
            db_session,
            owner_id=_dev_user.id,
            name="Commis User Source",
            source_type="url",
            config={"url": "https://example.com/"},
        )
        knowledge_crud.upsert_knowledge_document(
            db_session,
            source_id=commis_source.id,
            owner_id=_dev_user.id,
            path="https://example.com/commis-doc.md",
            content_text="bremen NAS - Synology storage server - COMMIS DOC",
            title="Commis Storage Overview",
        )

        # Create source for other_user (CredentialResolver user)
        resolver_source = knowledge_crud.create_knowledge_source(
            db_session,
            owner_id=other_user.id,
            name="Resolver User Source",
            source_type="url",
            config={"url": "https://example.com/"},
        )
        knowledge_crud.upsert_knowledge_document(
            db_session,
            source_id=resolver_source.id,
            owner_id=other_user.id,
            path="https://example.com/resolver-doc.md",
            content_text="bremen NAS - Synology storage server - RESOLVER DOC",
            title="Resolver Storage Overview",
        )

        # Set CommisContext with _dev_user, CredentialResolver with other_user
        ctx = CommisContext(
            commis_id="test-commis-456",
            owner_id=_dev_user.id,  # Different from resolver
            course_id="test-run-456",
        )
        resolver = CredentialResolver(
            fiche_id=999,
            db=db_session,
            owner_id=other_user.id,  # Different from commis context
        )

        commis_token = set_commis_context(ctx)
        resolver_token = set_credential_resolver(resolver)

        try:
            # Call knowledge_search - should resolve owner_id from CommisContext (_dev_user)
            results = knowledge_search("bremen", limit=5)

            # Should find ONLY the CommisContext user's document, not the resolver user's
            assert isinstance(results, list)
            assert len(results) == 1
            assert results[0]["source"] == "Commis User Source"
            assert "COMMIS DOC" in results[0]["snippets"][0]
        finally:
            reset_commis_context(commis_token)
            reset_credential_resolver(resolver_token)
