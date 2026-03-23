"""Tests for Linear tools."""

from unittest.mock import Mock
from unittest.mock import patch

from zerg.tools.builtin.linear_tools import linear_add_comment
from zerg.tools.builtin.linear_tools import linear_create_issue
from zerg.tools.builtin.linear_tools import linear_get_issue
from zerg.tools.builtin.linear_tools import linear_list_issues
from zerg.tools.builtin.linear_tools import linear_list_teams
from zerg.tools.builtin.linear_tools import linear_update_issue


class TestLinearCreateIssue:
    """Tests for linear_create_issue function."""

    def test_empty_api_key(self):
        """Test that empty API key is rejected."""
        result = linear_create_issue(api_key="", team_id="team_123", title="Test Issue")
        assert result["ok"] is False

    def test_empty_team_id(self):
        """Test that empty team ID is rejected."""
        result = linear_create_issue(api_key="lin_api_test", team_id="", title="Test Issue")
        assert result["ok"] is False

    def test_empty_title(self):
        """Test that empty title is rejected."""
        result = linear_create_issue(api_key="lin_api_test", team_id="team_123", title="")
        assert result["ok"] is False

    @patch("zerg.tools.builtin.linear_tools.httpx.Client")
    def test_successful_issue_creation(self, mock_client):
        """Test successful issue creation."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {
                "issueCreate": {
                    "success": True,
                    "issue": {
                        "id": "issue_123",
                        "identifier": "ENG-42",
                        "title": "Test Issue",
                        "url": "https://linear.app/team/issue/ENG-42",
                    },
                }
            }
        }
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        result = linear_create_issue(api_key="lin_api_test", team_id="team_123", title="Test Issue", description="Test description")

        assert result["ok"] is True
        assert result["data"]["identifier"] == "ENG-42"

    @patch("zerg.tools.builtin.linear_tools.httpx.Client")
    def test_unauthorized_error(self, mock_client):
        """Test handling of unauthorized error."""
        mock_response = Mock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        result = linear_create_issue(api_key="lin_api_invalid", team_id="team_123", title="Test Issue")

        assert result["ok"] is False
        assert result["error_type"] == "invalid_credentials"


class TestLinearListIssues:
    """Tests for linear_list_issues function."""

    @patch("zerg.tools.builtin.linear_tools.httpx.Client")
    def test_successful_list(self, mock_client):
        """Test successful issue listing."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {
                "issues": {
                    "nodes": [
                        {"id": "1", "identifier": "ENG-1", "title": "Issue 1"},
                        {"id": "2", "identifier": "ENG-2", "title": "Issue 2"},
                    ]
                }
            }
        }
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        result = linear_list_issues(api_key="lin_api_test")

        assert result["ok"] is True
        assert len(result["data"]) == 2


class TestLinearGetIssue:
    """Tests for linear_get_issue function."""

    @patch("zerg.tools.builtin.linear_tools.httpx.Client")
    def test_successful_get(self, mock_client):
        """Test successful issue retrieval."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {
                "issue": {
                    "id": "issue_123",
                    "identifier": "ENG-42",
                    "title": "Test Issue",
                    "description": "Test description",
                }
            }
        }
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        result = linear_get_issue(api_key="lin_api_test", issue_id="issue_123")

        assert result["ok"] is True
        assert result["data"]["identifier"] == "ENG-42"

    def test_empty_issue_id(self):
        """Test that empty issue ID is rejected."""
        result = linear_get_issue(api_key="lin_api_test", issue_id="")
        assert result["ok"] is False


class TestLinearUpdateIssue:
    """Tests for linear_update_issue function."""

    @patch("zerg.tools.builtin.linear_tools.httpx.Client")
    def test_successful_update(self, mock_client):
        """Test successful issue update."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {"issueUpdate": {"success": True, "issue": {"id": "issue_123", "title": "Updated Title"}}}
        }
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        result = linear_update_issue(api_key="lin_api_test", issue_id="issue_123", title="Updated Title")

        assert result["ok"] is True


class TestLinearAddComment:
    """Tests for linear_add_comment function."""

    def test_empty_body(self):
        """Test that empty comment body is rejected."""
        result = linear_add_comment(api_key="lin_api_test", issue_id="issue_123", body="")
        assert result["ok"] is False

    @patch("zerg.tools.builtin.linear_tools.httpx.Client")
    def test_successful_comment(self, mock_client):
        """Test successful comment addition."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {"commentCreate": {"success": True, "comment": {"id": "comment_123", "body": "Test comment"}}}
        }
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        result = linear_add_comment(api_key="lin_api_test", issue_id="issue_123", body="Test comment")

        assert result["ok"] is True


class TestLinearListTeams:
    """Tests for linear_list_teams function."""

    @patch("zerg.tools.builtin.linear_tools.httpx.Client")
    def test_successful_list(self, mock_client):
        """Test successful team listing."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {
                "teams": {
                    "nodes": [
                        {"id": "team_1", "name": "Engineering", "key": "ENG"},
                        {"id": "team_2", "name": "Product", "key": "PROD"},
                    ]
                }
            }
        }
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        result = linear_list_teams(api_key="lin_api_test")

        assert result["ok"] is True
        assert len(result["data"]) == 2
