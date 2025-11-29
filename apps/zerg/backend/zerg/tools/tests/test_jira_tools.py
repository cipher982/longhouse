"""Tests for Jira tools."""

import pytest
from unittest.mock import Mock, patch
from zerg.tools.builtin.jira_tools import (
    jira_create_issue,
    jira_list_issues,
    jira_get_issue,
    jira_add_comment,
    jira_transition_issue,
    jira_update_issue,
)


class TestJiraCreateIssue:
    """Tests for jira_create_issue function."""

    def test_empty_domain(self):
        """Test that empty domain is rejected."""
        result = jira_create_issue(
            domain="",
            email="test@example.com",
            api_token="test_token",
            project_key="TEST",
            issue_type="Task",
            summary="Test Issue"
        )
        assert result["success"] is False

    def test_empty_email(self):
        """Test that empty email is rejected."""
        result = jira_create_issue(
            domain="test.atlassian.net",
            email="",
            api_token="test_token",
            project_key="TEST",
            issue_type="Task",
            summary="Test Issue"
        )
        assert result["success"] is False

    def test_empty_summary(self):
        """Test that empty summary is rejected."""
        result = jira_create_issue(
            domain="test.atlassian.net",
            email="test@example.com",
            api_token="test_token",
            project_key="TEST",
            issue_type="Task",
            summary=""
        )
        assert result["success"] is False

    @patch("zerg.tools.builtin.jira_tools.httpx.Client")
    def test_successful_issue_creation(self, mock_client):
        """Test successful issue creation."""
        mock_response = Mock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "id": "10001",
            "key": "TEST-123",
            "self": "https://test.atlassian.net/rest/api/3/issue/10001"
        }
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        result = jira_create_issue(
            domain="test.atlassian.net",
            email="test@example.com",
            api_token="test_token",
            project_key="TEST",
            issue_type="Task",
            summary="Test Issue",
            description="Test description"
        )

        assert result["success"] is True
        assert result["issue_key"] == "TEST-123"

    @patch("zerg.tools.builtin.jira_tools.httpx.Client")
    def test_unauthorized_error(self, mock_client):
        """Test handling of unauthorized error."""
        mock_response = Mock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"
        mock_response.json.return_value = {"errorMessages": ["Invalid API key"]}
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        result = jira_create_issue(
            domain="test.atlassian.net",
            email="test@example.com",
            api_token="invalid_token",
            project_key="TEST",
            issue_type="Task",
            summary="Test Issue"
        )

        assert result["success"] is False
        assert result["status_code"] == 401


class TestJiraListIssues:
    """Tests for jira_list_issues function."""

    @patch("zerg.tools.builtin.jira_tools.httpx.Client")
    def test_successful_list(self, mock_client):
        """Test successful issue listing."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "total": 2,
            "issues": [
                {"key": "TEST-1", "fields": {"summary": "Issue 1"}},
                {"key": "TEST-2", "fields": {"summary": "Issue 2"}}
            ]
        }
        mock_client.return_value.__enter__.return_value.get.return_value = mock_response

        result = jira_list_issues(
            domain="test.atlassian.net",
            email="test@example.com",
            api_token="test_token",
            project_key="TEST"
        )

        assert result["success"] is True
        assert result["total"] == 2

    @patch("zerg.tools.builtin.jira_tools.httpx.Client")
    def test_list_with_jql(self, mock_client):
        """Test issue listing with custom JQL."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"total": 0, "issues": []}
        mock_get = mock_client.return_value.__enter__.return_value.get
        mock_get.return_value = mock_response

        result = jira_list_issues(
            domain="test.atlassian.net",
            email="test@example.com",
            api_token="test_token",
            project_key="TEST",
            jql="status = 'In Progress'"
        )

        assert result["success"] is True


class TestJiraGetIssue:
    """Tests for jira_get_issue function."""

    @patch("zerg.tools.builtin.jira_tools.httpx.Client")
    def test_successful_get(self, mock_client):
        """Test successful issue retrieval."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "key": "TEST-123",
            "id": "10001",
            "fields": {
                "summary": "Test Issue",
                "status": {"name": "Open"},
                "issuetype": {"name": "Task"},
                "description": {"content": []}
            }
        }
        mock_client.return_value.__enter__.return_value.get.return_value = mock_response

        result = jira_get_issue(
            domain="test.atlassian.net",
            email="test@example.com",
            api_token="test_token",
            issue_key="TEST-123"
        )

        assert result["success"] is True
        assert result["issue"]["key"] == "TEST-123"

    @patch("zerg.tools.builtin.jira_tools.httpx.Client")
    def test_issue_not_found(self, mock_client):
        """Test handling of issue not found."""
        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.text = "Issue Does Not Exist"
        mock_response.json.return_value = {"errorMessages": ["Issue Does Not Exist"]}
        mock_client.return_value.__enter__.return_value.get.return_value = mock_response

        result = jira_get_issue(
            domain="test.atlassian.net",
            email="test@example.com",
            api_token="test_token",
            issue_key="TEST-999"
        )

        assert result["success"] is False
        assert result["status_code"] == 404


class TestJiraAddComment:
    """Tests for jira_add_comment function."""

    def test_empty_body(self):
        """Test that empty comment body is rejected."""
        result = jira_add_comment(
            domain="test.atlassian.net",
            email="test@example.com",
            api_token="test_token",
            issue_key="TEST-123",
            body=""
        )
        assert result["success"] is False

    @patch("zerg.tools.builtin.jira_tools.httpx.Client")
    def test_successful_comment(self, mock_client):
        """Test successful comment addition."""
        mock_response = Mock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"id": "comment_123"}
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        result = jira_add_comment(
            domain="test.atlassian.net",
            email="test@example.com",
            api_token="test_token",
            issue_key="TEST-123",
            body="This is a test comment"
        )

        assert result["success"] is True
        assert result["comment_id"] == "comment_123"


class TestJiraTransitionIssue:
    """Tests for jira_transition_issue function."""

    @patch("zerg.tools.builtin.jira_tools.httpx.Client")
    def test_successful_transition(self, mock_client):
        """Test successful issue transition."""
        mock_response = Mock()
        mock_response.status_code = 204
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        result = jira_transition_issue(
            domain="test.atlassian.net",
            email="test@example.com",
            api_token="test_token",
            issue_key="TEST-123",
            transition_id="21"
        )

        assert result["success"] is True

    @patch("zerg.tools.builtin.jira_tools.httpx.Client")
    def test_invalid_transition(self, mock_client):
        """Test handling of invalid transition."""
        mock_response = Mock()
        mock_response.status_code = 400
        mock_response.text = "Invalid transition"
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        result = jira_transition_issue(
            domain="test.atlassian.net",
            email="test@example.com",
            api_token="test_token",
            issue_key="TEST-123",
            transition_id="999"
        )

        assert result["success"] is False


class TestJiraUpdateIssue:
    """Tests for jira_update_issue function."""

    @patch("zerg.tools.builtin.jira_tools.httpx.Client")
    def test_successful_update(self, mock_client):
        """Test successful issue update."""
        mock_response = Mock()
        mock_response.status_code = 204
        mock_client.return_value.__enter__.return_value.put.return_value = mock_response

        result = jira_update_issue(
            domain="test.atlassian.net",
            email="test@example.com",
            api_token="test_token",
            issue_key="TEST-123",
            fields={"summary": "Updated Summary"}
        )

        assert result["success"] is True
