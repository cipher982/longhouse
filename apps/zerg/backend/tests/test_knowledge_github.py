"""Tests for GitHub Knowledge Sync."""

import base64
import json
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy.orm import Session

from zerg.crud import knowledge_crud
from zerg.models.models import AccountConnectorCredential
from zerg.models.models import KnowledgeDocument
from zerg.models.models import User
from zerg.services import knowledge_sync_service
from zerg.utils.crypto import encrypt


def _create_github_credential(db: Session, user: User, token: str = "test-token"):
    """Helper to create a GitHub credential for a user."""
    encrypted = encrypt(json.dumps({"token": token}))
    cred = AccountConnectorCredential(
        owner_id=user.id,
        connector_type="github",
        encrypted_value=encrypted,
    )
    db.add(cred)
    db.commit()
    return cred


class TestGitHubRepoSyncService:
    """Tests for sync_github_repo_source()."""

    @pytest.mark.asyncio
    async def test_sync_wrong_source_type(self, db_session: Session, _dev_user: User):
        """Test that sync_github_repo_source rejects non-github_repo sources."""
        source = knowledge_crud.create_knowledge_source(
            db_session,
            owner_id=_dev_user.id,
            name="URL Source",
            source_type="url",
            config={"url": "https://example.com/docs.md"},
        )

        with pytest.raises(ValueError, match="Expected source_type='github_repo'"):
            await knowledge_sync_service.sync_github_repo_source(db_session, source)

    @pytest.mark.asyncio
    async def test_sync_github_not_connected(self, db_session: Session, _dev_user: User):
        """Test error when GitHub not connected."""
        source = knowledge_crud.create_knowledge_source(
            db_session,
            owner_id=_dev_user.id,
            name="My Repo",
            source_type="github_repo",
            config={"owner": "testuser", "repo": "testrepo"},
        )

        with pytest.raises(ValueError, match="GitHub not connected"):
            await knowledge_sync_service.sync_github_repo_source(db_session, source)

    @pytest.mark.asyncio
    async def test_sync_github_repo_success(self, db_session: Session, _dev_user: User):
        """Test successful repo sync with mocked GitHub API."""
        # Create GitHub credential
        _create_github_credential(db_session, _dev_user)

        # Create source
        source = knowledge_crud.create_knowledge_source(
            db_session,
            owner_id=_dev_user.id,
            name="Test Repo",
            source_type="github_repo",
            config={
                "owner": "testuser",
                "repo": "testrepo",
                "branch": "main",
                "include_paths": ["**/*.md"],
            },
        )

        # Mock GitHub API responses
        mock_responses = {
            "/repos/testuser/testrepo/git/ref/heads/main": {
                "object": {"sha": "commit123"}
            },
            "/repos/testuser/testrepo/git/commits/commit123": {
                "tree": {"sha": "tree123"}
            },
            "/repos/testuser/testrepo/git/trees/tree123": {
                "truncated": False,
                "tree": [
                    {"type": "blob", "path": "README.md", "sha": "blob1", "size": 100},
                    {"type": "blob", "path": "docs/guide.md", "sha": "blob2", "size": 200},
                    {"type": "tree", "path": "src"},  # Should be skipped (not blob)
                    {"type": "blob", "path": "src/main.py", "sha": "blob3", "size": 50},  # Should be skipped (not .md)
                ],
            },
            "/repos/testuser/testrepo/git/blobs/blob1": {
                "encoding": "base64",
                "content": base64.b64encode(b"# README\n\nTest content").decode(),
            },
            "/repos/testuser/testrepo/git/blobs/blob2": {
                "encoding": "base64",
                "content": base64.b64encode(b"# Guide\n\nGuide content").decode(),
            },
        }

        async def mock_get(endpoint, **kwargs):
            response = MagicMock()
            # Handle params in endpoint
            if "?" in endpoint:
                endpoint = endpoint.split("?")[0]
            data = mock_responses.get(endpoint, {})
            response.json.return_value = data
            response.status_code = 200
            response.raise_for_status = MagicMock()
            return response

        with patch("zerg.services.knowledge_sync_service.github_async_client") as mock_client:
            mock_cm = AsyncMock()
            mock_cm.__aenter__.return_value.get = mock_get
            mock_client.return_value = mock_cm

            await knowledge_sync_service.sync_github_repo_source(db_session, source)

        # Verify source status
        db_session.refresh(source)
        assert source.sync_status == "success"

        # Verify documents created
        docs = knowledge_crud.get_knowledge_documents(
            db_session,
            owner_id=_dev_user.id,
            source_id=source.id,
        )
        assert len(docs) == 2

        # Check document paths
        doc_paths = {doc.path for doc in docs}
        assert "https://github.com/testuser/testrepo/blob/main/README.md" in doc_paths
        assert "https://github.com/testuser/testrepo/blob/main/docs/guide.md" in doc_paths

        # Check metadata
        readme_doc = next(d for d in docs if "README.md" in d.path)
        assert readme_doc.doc_metadata["github_sha"] == "blob1"
        assert readme_doc.doc_metadata["branch"] == "main"
        assert readme_doc.title == "README.md"

    @pytest.mark.asyncio
    async def test_sync_incremental_skip_unchanged(self, db_session: Session, _dev_user: User):
        """Test that unchanged files (same SHA) are skipped."""
        # Create GitHub credential
        _create_github_credential(db_session, _dev_user)

        # Create source
        source = knowledge_crud.create_knowledge_source(
            db_session,
            owner_id=_dev_user.id,
            name="Test Repo",
            source_type="github_repo",
            config={
                "owner": "testuser",
                "repo": "testrepo",
                "branch": "main",
                "include_paths": ["**/*.md"],
            },
        )

        # Pre-create document with same SHA
        knowledge_crud.upsert_knowledge_document(
            db_session,
            source_id=source.id,
            owner_id=_dev_user.id,
            path="https://github.com/testuser/testrepo/blob/main/README.md",
            content_text="Old content",
            title="README.md",
            doc_metadata={"github_sha": "blob1"},  # Same SHA as mock response
        )

        # Mock GitHub API responses
        mock_responses = {
            "/repos/testuser/testrepo/git/ref/heads/main": {
                "object": {"sha": "commit123"}
            },
            "/repos/testuser/testrepo/git/commits/commit123": {
                "tree": {"sha": "tree123"}
            },
            "/repos/testuser/testrepo/git/trees/tree123": {
                "truncated": False,
                "tree": [
                    {"type": "blob", "path": "README.md", "sha": "blob1", "size": 100},
                ],
            },
        }

        blob_fetch_count = 0

        async def mock_get(endpoint, **kwargs):
            nonlocal blob_fetch_count
            response = MagicMock()
            if "?" in endpoint:
                endpoint = endpoint.split("?")[0]

            if endpoint.startswith("/repos/testuser/testrepo/git/blobs/"):
                blob_fetch_count += 1
                response.json.return_value = {
                    "encoding": "base64",
                    "content": base64.b64encode(b"New content").decode(),
                }
            else:
                response.json.return_value = mock_responses.get(endpoint, {})

            response.status_code = 200
            response.raise_for_status = MagicMock()
            return response

        with patch("zerg.services.knowledge_sync_service.github_async_client") as mock_client:
            mock_cm = AsyncMock()
            mock_cm.__aenter__.return_value.get = mock_get
            mock_client.return_value = mock_cm

            await knowledge_sync_service.sync_github_repo_source(db_session, source)

        # Verify blob was NOT fetched (incremental skip)
        assert blob_fetch_count == 0

        # Verify document still has old content (not updated)
        docs = knowledge_crud.get_knowledge_documents(
            db_session,
            owner_id=_dev_user.id,
            source_id=source.id,
        )
        assert len(docs) == 1
        assert docs[0].content_text == "Old content"

    @pytest.mark.asyncio
    async def test_sync_removes_deleted_files(self, db_session: Session, _dev_user: User):
        """Test that files deleted from repo are removed from docs."""
        # Create GitHub credential
        _create_github_credential(db_session, _dev_user)

        # Create source
        source = knowledge_crud.create_knowledge_source(
            db_session,
            owner_id=_dev_user.id,
            name="Test Repo",
            source_type="github_repo",
            config={
                "owner": "testuser",
                "repo": "testrepo",
                "branch": "main",
                "include_paths": ["**/*.md"],
            },
        )

        # Pre-create document that will be "deleted" from repo
        knowledge_crud.upsert_knowledge_document(
            db_session,
            source_id=source.id,
            owner_id=_dev_user.id,
            path="https://github.com/testuser/testrepo/blob/main/DELETED.md",
            content_text="This file was deleted",
            title="DELETED.md",
            doc_metadata={"github_sha": "deleted_blob"},
        )

        # Mock GitHub API - tree does NOT include DELETED.md
        mock_responses = {
            "/repos/testuser/testrepo/git/ref/heads/main": {
                "object": {"sha": "commit123"}
            },
            "/repos/testuser/testrepo/git/commits/commit123": {
                "tree": {"sha": "tree123"}
            },
            "/repos/testuser/testrepo/git/trees/tree123": {
                "truncated": False,
                "tree": [
                    {"type": "blob", "path": "README.md", "sha": "blob1", "size": 100},
                ],
            },
            "/repos/testuser/testrepo/git/blobs/blob1": {
                "encoding": "base64",
                "content": base64.b64encode(b"# README").decode(),
            },
        }

        async def mock_get(endpoint, **kwargs):
            response = MagicMock()
            if "?" in endpoint:
                endpoint = endpoint.split("?")[0]
            data = mock_responses.get(endpoint, {})
            response.json.return_value = data
            response.status_code = 200
            response.raise_for_status = MagicMock()
            return response

        with patch("zerg.services.knowledge_sync_service.github_async_client") as mock_client:
            mock_cm = AsyncMock()
            mock_cm.__aenter__.return_value.get = mock_get
            mock_client.return_value = mock_cm

            await knowledge_sync_service.sync_github_repo_source(db_session, source)

        # Verify only README.md remains
        docs = knowledge_crud.get_knowledge_documents(
            db_session,
            owner_id=_dev_user.id,
            source_id=source.id,
        )
        assert len(docs) == 1
        assert "README.md" in docs[0].path

    @pytest.mark.asyncio
    async def test_sync_skips_cleanup_on_truncated_tree(self, db_session: Session, _dev_user: User):
        """Test that cleanup is skipped when tree is truncated."""
        # Create GitHub credential
        _create_github_credential(db_session, _dev_user)

        # Create source
        source = knowledge_crud.create_knowledge_source(
            db_session,
            owner_id=_dev_user.id,
            name="Test Repo",
            source_type="github_repo",
            config={
                "owner": "testuser",
                "repo": "testrepo",
                "branch": "main",
                "include_paths": ["**/*.md"],
            },
        )

        # Pre-create document that should NOT be deleted when tree is truncated
        knowledge_crud.upsert_knowledge_document(
            db_session,
            source_id=source.id,
            owner_id=_dev_user.id,
            path="https://github.com/testuser/testrepo/blob/main/EXISTING.md",
            content_text="Existing content",
            title="EXISTING.md",
            doc_metadata={"github_sha": "existing_blob"},
        )

        # Mock GitHub API with truncated tree
        mock_responses = {
            "/repos/testuser/testrepo/git/ref/heads/main": {
                "object": {"sha": "commit123"}
            },
            "/repos/testuser/testrepo/git/commits/commit123": {
                "tree": {"sha": "tree123"}
            },
            "/repos/testuser/testrepo/git/trees/tree123": {
                "truncated": True,  # Tree is truncated
                "tree": [
                    {"type": "blob", "path": "README.md", "sha": "blob1", "size": 100},
                ],
            },
            "/repos/testuser/testrepo/git/blobs/blob1": {
                "encoding": "base64",
                "content": base64.b64encode(b"# README").decode(),
            },
        }

        async def mock_get(endpoint, **kwargs):
            response = MagicMock()
            if "?" in endpoint:
                endpoint = endpoint.split("?")[0]
            data = mock_responses.get(endpoint, {})
            response.json.return_value = data
            response.status_code = 200
            response.raise_for_status = MagicMock()
            return response

        with patch("zerg.services.knowledge_sync_service.github_async_client") as mock_client:
            mock_cm = AsyncMock()
            mock_cm.__aenter__.return_value.get = mock_get
            mock_client.return_value = mock_cm

            await knowledge_sync_service.sync_github_repo_source(db_session, source)

        # Verify EXISTING.md was NOT deleted (cleanup skipped due to truncated)
        docs = knowledge_crud.get_knowledge_documents(
            db_session,
            owner_id=_dev_user.id,
            source_id=source.id,
        )
        assert len(docs) == 2
        doc_titles = {d.title for d in docs}
        assert "EXISTING.md" in doc_titles
        assert "README.md" in doc_titles


class TestGitHubPatternMatching:
    """Tests for file pattern filtering."""

    def test_default_include_patterns(self):
        """Test that default patterns include docs only."""
        import pathspec

        include_spec = pathspec.PathSpec.from_lines(
            "gitwildmatch", knowledge_sync_service.DEFAULT_INCLUDE_PATHS
        )

        # Should match
        assert include_spec.match_file("README.md")
        assert include_spec.match_file("docs/guide.md")
        assert include_spec.match_file("AGENTS.md")
        assert include_spec.match_file("docs/tutorial.mdx")
        assert include_spec.match_file("notes.txt")
        assert include_spec.match_file("docs/api.rst")

        # Should not match
        assert not include_spec.match_file("src/main.py")
        assert not include_spec.match_file("package.json")
        assert not include_spec.match_file("src/index.js")

    def test_default_exclude_patterns(self):
        """Test that default patterns exclude secrets."""
        import pathspec

        exclude_spec = pathspec.PathSpec.from_lines(
            "gitwildmatch", knowledge_sync_service.DEFAULT_EXCLUDE_PATHS
        )

        # Should match (be excluded)
        assert exclude_spec.match_file(".env")
        assert exclude_spec.match_file(".env.local")
        assert exclude_spec.match_file("config/.env.prod")
        assert exclude_spec.match_file("secrets/key.pem")
        assert exclude_spec.match_file("certs/server.key")
        assert exclude_spec.match_file("ssh/id_rsa")
        assert exclude_spec.match_file("config/credentials.json")
        assert exclude_spec.match_file("node_modules/lodash/README.md")
        assert exclude_spec.match_file(".git/config")

        # Should not match (not excluded)
        assert not exclude_spec.match_file("docs/guide.md")
        assert not exclude_spec.match_file("src/main.py")


class TestGitHubKnowledgeAPI:
    """Tests for GitHub knowledge API endpoints."""

    def test_create_github_source_validates_config(self, client, _dev_user: User):
        """Test validation of required fields."""
        response = client.post(
            "/api/knowledge/sources",
            json={
                "name": "My Repo",
                "source_type": "github_repo",
                "config": {"owner": "testuser"},  # Missing "repo"
            },
        )
        assert response.status_code == 400
        assert "missing required fields" in response.json()["detail"].lower()
        assert "repo" in response.json()["detail"]

    def test_create_github_source_requires_connection(self, client, _dev_user: User):
        """Test 400 when GitHub not connected."""
        response = client.post(
            "/api/knowledge/sources",
            json={
                "name": "My Repo",
                "source_type": "github_repo",
                "config": {"owner": "testuser", "repo": "testrepo"},
            },
        )
        assert response.status_code == 400
        assert "GitHub must be connected" in response.json()["detail"]

    def test_create_github_source_success(self, client, db_session: Session, _dev_user: User):
        """Test successful creation when GitHub is connected."""
        # Create GitHub credential
        _create_github_credential(db_session, _dev_user)

        response = client.post(
            "/api/knowledge/sources",
            json={
                "name": "My Repo",
                "source_type": "github_repo",
                "config": {
                    "owner": "testuser",
                    "repo": "testrepo",
                    "branch": "main",
                    "include_paths": ["**/*.md"],
                },
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "My Repo"
        assert data["source_type"] == "github_repo"
        assert data["config"]["owner"] == "testuser"
        assert data["config"]["repo"] == "testrepo"
        assert data["sync_status"] == "pending"
