"""Tests for Notion tools."""

import pytest
from unittest.mock import Mock, patch
from zerg.tools.builtin.notion_tools import (
    notion_create_page,
    notion_get_page,
    notion_update_page,
    notion_search,
    notion_query_database,
    notion_append_blocks,
)


class TestNotionCreatePage:
    """Tests for notion_create_page function."""

    def test_empty_api_key(self):
        """Test that empty API key is rejected."""
        result = notion_create_page(
            api_key="",
            parent_id="page_123",
            title="Test Page"
        )
        assert result["success"] is False

    def test_empty_parent_id(self):
        """Test that empty parent ID is rejected."""
        result = notion_create_page(
            api_key="secret_test",
            parent_id="",
            title="Test Page"
        )
        assert result["success"] is False

    def test_empty_title(self):
        """Test that empty title is rejected."""
        result = notion_create_page(
            api_key="secret_test",
            parent_id="page_123",
            title=""
        )
        assert result["success"] is False

    @patch("zerg.tools.builtin.notion_tools.httpx.Client")
    def test_successful_page_creation(self, mock_client):
        """Test successful page creation."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "id": "page_456",
            "url": "https://notion.so/Test-Page-123",
            "created_time": "2025-11-29T00:00:00.000Z"
        }
        mock_client.return_value.__enter__.return_value.request.return_value = mock_response

        result = notion_create_page(
            api_key="secret_test",
            parent_id="page_123",
            title="Test Page"
        )

        assert result["success"] is True
        assert result["page_id"] == "page_456"

    @patch("zerg.tools.builtin.notion_tools.httpx.Client")
    def test_unauthorized_error(self, mock_client):
        """Test handling of unauthorized error."""
        mock_response = Mock()
        mock_response.status_code = 401
        mock_response.json.return_value = {"message": "Invalid API key"}
        mock_client.return_value.__enter__.return_value.request.return_value = mock_response

        result = notion_create_page(
            api_key="secret_invalid",
            parent_id="page_123",
            title="Test Page"
        )

        assert result["success"] is False
        assert result["status_code"] == 401


class TestNotionGetPage:
    """Tests for notion_get_page function."""

    def test_empty_page_id(self):
        """Test that empty page ID is rejected."""
        result = notion_get_page(
            api_key="secret_test",
            page_id=""
        )
        assert result["success"] is False

    @patch("zerg.tools.builtin.notion_tools.httpx.Client")
    def test_successful_get(self, mock_client):
        """Test successful page retrieval."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "id": "page_123",
            "properties": {
                "title": {"title": [{"plain_text": "Test Page"}]}
            }
        }
        mock_client.return_value.__enter__.return_value.request.return_value = mock_response

        result = notion_get_page(
            api_key="secret_test",
            page_id="page_123"
        )

        assert result["success"] is True
        assert result["page"]["id"] == "page_123"

    @patch("zerg.tools.builtin.notion_tools.httpx.Client")
    def test_page_not_found(self, mock_client):
        """Test handling of page not found."""
        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.json.return_value = {"message": "Page not found"}
        mock_client.return_value.__enter__.return_value.request.return_value = mock_response

        result = notion_get_page(
            api_key="secret_test",
            page_id="page_999"
        )

        assert result["success"] is False
        assert result["status_code"] == 404


class TestNotionUpdatePage:
    """Tests for notion_update_page function."""

    @patch("zerg.tools.builtin.notion_tools.httpx.Client")
    def test_successful_update(self, mock_client):
        """Test successful page update."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "id": "page_123",
            "properties": {}
        }
        mock_client.return_value.__enter__.return_value.request.return_value = mock_response

        result = notion_update_page(
            api_key="secret_test",
            page_id="page_123",
            properties={"Status": {"select": {"name": "Done"}}}
        )

        assert result["success"] is True

    @patch("zerg.tools.builtin.notion_tools.httpx.Client")
    def test_archive_page(self, mock_client):
        """Test archiving a page."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "page_123", "archived": True}
        mock_client.return_value.__enter__.return_value.request.return_value = mock_response

        result = notion_update_page(
            api_key="secret_test",
            page_id="page_123",
            archived=True
        )

        assert result["success"] is True


class TestNotionSearch:
    """Tests for notion_search function."""

    @patch("zerg.tools.builtin.notion_tools.httpx.Client")
    def test_successful_search(self, mock_client):
        """Test successful search."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "results": [
                {"id": "page_1", "object": "page"},
                {"id": "page_2", "object": "page"}
            ],
            "has_more": False
        }
        mock_client.return_value.__enter__.return_value.request.return_value = mock_response

        result = notion_search(
            api_key="secret_test",
            query="test"
        )

        assert result["success"] is True
        assert len(result["results"]) == 2

    @patch("zerg.tools.builtin.notion_tools.httpx.Client")
    def test_search_with_filter(self, mock_client):
        """Test search with filter."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"results": [], "has_more": False}
        mock_client.return_value.__enter__.return_value.request.return_value = mock_response

        result = notion_search(
            api_key="secret_test",
            query="test",
            filter_type="database"
        )

        assert result["success"] is True


class TestNotionQueryDatabase:
    """Tests for notion_query_database function."""

    def test_empty_database_id(self):
        """Test that empty database ID is rejected."""
        result = notion_query_database(
            api_key="secret_test",
            database_id=""
        )
        assert result["success"] is False

    @patch("zerg.tools.builtin.notion_tools.httpx.Client")
    def test_successful_query(self, mock_client):
        """Test successful database query."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "results": [
                {"id": "row_1", "properties": {}},
                {"id": "row_2", "properties": {}}
            ],
            "has_more": False
        }
        mock_client.return_value.__enter__.return_value.request.return_value = mock_response

        result = notion_query_database(
            api_key="secret_test",
            database_id="db_123"
        )

        assert result["success"] is True
        assert len(result["results"]) == 2

    @patch("zerg.tools.builtin.notion_tools.httpx.Client")
    def test_query_with_sorts(self, mock_client):
        """Test database query with sorts."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"results": [], "has_more": False}
        mock_client.return_value.__enter__.return_value.request.return_value = mock_response

        result = notion_query_database(
            api_key="secret_test",
            database_id="db_123",
            sorts=[{"property": "Created", "direction": "descending"}]
        )

        assert result["success"] is True


class TestNotionAppendBlocks:
    """Tests for notion_append_blocks function."""

    def test_empty_blocks(self):
        """Test that empty blocks is rejected."""
        result = notion_append_blocks(
            api_key="secret_test",
            page_id="page_123",
            blocks=[]
        )
        assert result["success"] is False

    @patch("zerg.tools.builtin.notion_tools.httpx.Client")
    def test_successful_append(self, mock_client):
        """Test successful block append."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "results": [{"id": "block_1", "type": "paragraph"}]
        }
        mock_client.return_value.__enter__.return_value.request.return_value = mock_response

        result = notion_append_blocks(
            api_key="secret_test",
            page_id="page_123",
            blocks=[{"type": "paragraph", "paragraph": {"text": [{"type": "text", "text": {"content": "Test"}}]}}]
        )

        assert result["success"] is True
