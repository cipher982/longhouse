"""Tests for GitHub tools."""

import pytest
from unittest.mock import Mock, patch
from zerg.tools.builtin.github_tools import (
    github_create_issue,
    github_list_issues,
    github_get_issue,
    github_add_comment,
    github_list_pull_requests,
    github_get_pull_request,
)


class TestGitHubCreateIssue:
    """Tests for github_create_issue function."""

    def test_empty_token(self):
        """Test that empty token is rejected."""
        result = github_create_issue(
            token="",
            owner="octocat",
            repo="hello-world",
            title="Test Issue"
        )
        assert result["success"] is False
        assert "Invalid or missing GitHub token" in result["error"]

    def test_empty_title(self):
        """Test that empty title is rejected."""
        result = github_create_issue(
            token="ghp_test_token",
            owner="octocat",
            repo="hello-world",
            title=""
        )
        assert result["success"] is False
        assert "title is required" in result["error"]

    @patch("zerg.tools.builtin.github_tools.httpx.Client")
    def test_successful_issue_creation(self, mock_client):
        """Test successful issue creation."""
        mock_response = Mock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "number": 42,
            "title": "Test Issue",
            "state": "open",
            "html_url": "https://github.com/octocat/hello-world/issues/42",
            "created_at": "2025-11-29T00:00:00Z"
        }
        mock_client.return_value.__enter__.return_value.request.return_value = mock_response

        result = github_create_issue(
            token="ghp_test_token",
            owner="octocat",
            repo="hello-world",
            title="Test Issue",
            body="This is a test",
            labels=["bug", "enhancement"],
            assignees=["octocat"]
        )

        assert result["success"] is True
        assert result["data"]["number"] == 42
        assert result["data"]["title"] == "Test Issue"
        assert result["data"]["state"] == "open"

    @patch("zerg.tools.builtin.github_tools.httpx.Client")
    def test_rate_limit_error(self, mock_client):
        """Test rate limit handling."""
        mock_response = Mock()
        mock_response.status_code = 403
        mock_response.headers = {
            "x-ratelimit-remaining": "0",
            "x-ratelimit-reset": "1732800000"
        }
        mock_response.json.return_value = {"message": "API rate limit exceeded"}
        mock_client.return_value.__enter__.return_value.request.return_value = mock_response

        result = github_create_issue(
            token="ghp_test_token",
            owner="octocat",
            repo="hello-world",
            title="Test Issue"
        )

        assert result["success"] is False
        assert "rate limit exceeded" in result["error"]
        assert result["status_code"] == 403

    @patch("zerg.tools.builtin.github_tools.httpx.Client")
    def test_authentication_error(self, mock_client):
        """Test authentication error handling."""
        mock_response = Mock()
        mock_response.status_code = 401
        mock_response.json.return_value = {"message": "Bad credentials"}
        mock_client.return_value.__enter__.return_value.request.return_value = mock_response

        result = github_create_issue(
            token="invalid_token",
            owner="octocat",
            repo="hello-world",
            title="Test Issue"
        )

        assert result["success"] is False
        assert "authentication failed" in result["error"]
        assert result["status_code"] == 401


class TestGitHubListIssues:
    """Tests for github_list_issues function."""

    def test_invalid_state(self):
        """Test that invalid state values are rejected."""
        result = github_list_issues(
            token="ghp_test_token",
            owner="octocat",
            repo="hello-world",
            state="invalid"
        )
        assert result["success"] is False
        assert "State must be" in result["error"]

    def test_invalid_per_page(self):
        """Test that invalid per_page values are rejected."""
        result = github_list_issues(
            token="ghp_test_token",
            owner="octocat",
            repo="hello-world",
            per_page=101
        )
        assert result["success"] is False
        assert "per_page must be between" in result["error"]

    @patch("zerg.tools.builtin.github_tools.httpx.Client")
    def test_successful_list_issues(self, mock_client):
        """Test successful issue listing."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "number": 1,
                "title": "Issue 1",
                "state": "open",
                "html_url": "https://github.com/octocat/hello-world/issues/1",
                "labels": [{"name": "bug"}],
                "created_at": "2025-11-29T00:00:00Z"
            },
            {
                "number": 2,
                "title": "Issue 2",
                "state": "open",
                "html_url": "https://github.com/octocat/hello-world/issues/2",
                "labels": [],
                "created_at": "2025-11-29T01:00:00Z"
            }
        ]
        mock_client.return_value.__enter__.return_value.request.return_value = mock_response

        result = github_list_issues(
            token="ghp_test_token",
            owner="octocat",
            repo="hello-world",
            state="open"
        )

        assert result["success"] is True
        assert result["count"] == 2
        assert len(result["data"]) == 2
        assert result["data"][0]["number"] == 1
        assert result["data"][0]["labels"] == ["bug"]


class TestGitHubGetIssue:
    """Tests for github_get_issue function."""

    def test_invalid_issue_number(self):
        """Test that invalid issue numbers are rejected."""
        result = github_get_issue(
            token="ghp_test_token",
            owner="octocat",
            repo="hello-world",
            issue_number=-1
        )
        assert result["success"] is False
        assert "positive integer" in result["error"]

    @patch("zerg.tools.builtin.github_tools.httpx.Client")
    def test_successful_get_issue(self, mock_client):
        """Test successful issue retrieval."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "number": 42,
            "title": "Test Issue",
            "body": "This is the issue body",
            "state": "open",
            "html_url": "https://github.com/octocat/hello-world/issues/42",
            "labels": [{"name": "bug"}, {"name": "priority-high"}],
            "created_at": "2025-11-29T00:00:00Z",
            "updated_at": "2025-11-29T01:00:00Z"
        }
        mock_client.return_value.__enter__.return_value.request.return_value = mock_response

        result = github_get_issue(
            token="ghp_test_token",
            owner="octocat",
            repo="hello-world",
            issue_number=42
        )

        assert result["success"] is True
        assert result["data"]["number"] == 42
        assert result["data"]["title"] == "Test Issue"
        assert result["data"]["body"] == "This is the issue body"
        assert len(result["data"]["labels"]) == 2
        assert "bug" in result["data"]["labels"]

    @patch("zerg.tools.builtin.github_tools.httpx.Client")
    def test_issue_not_found(self, mock_client):
        """Test issue not found handling."""
        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.json.return_value = {"message": "Not Found"}
        mock_client.return_value.__enter__.return_value.request.return_value = mock_response

        result = github_get_issue(
            token="ghp_test_token",
            owner="octocat",
            repo="hello-world",
            issue_number=999
        )

        assert result["success"] is False
        assert result["status_code"] == 404
        assert "not found" in result["error"]


class TestGitHubAddComment:
    """Tests for github_add_comment function."""

    def test_empty_comment_body(self):
        """Test that empty comment body is rejected."""
        result = github_add_comment(
            token="ghp_test_token",
            owner="octocat",
            repo="hello-world",
            issue_number=42,
            body=""
        )
        assert result["success"] is False
        assert "body is required" in result["error"]

    def test_invalid_issue_number(self):
        """Test that invalid issue numbers are rejected."""
        result = github_add_comment(
            token="ghp_test_token",
            owner="octocat",
            repo="hello-world",
            issue_number=0,
            body="Test comment"
        )
        assert result["success"] is False
        assert "positive integer" in result["error"]

    @patch("zerg.tools.builtin.github_tools.httpx.Client")
    def test_successful_add_comment(self, mock_client):
        """Test successful comment addition."""
        mock_response = Mock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "id": 12345,
            "body": "Test comment",
            "html_url": "https://github.com/octocat/hello-world/issues/42#issuecomment-12345",
            "created_at": "2025-11-29T00:00:00Z"
        }
        mock_client.return_value.__enter__.return_value.request.return_value = mock_response

        result = github_add_comment(
            token="ghp_test_token",
            owner="octocat",
            repo="hello-world",
            issue_number=42,
            body="Test comment"
        )

        assert result["success"] is True
        assert result["data"]["id"] == 12345
        assert result["data"]["body"] == "Test comment"


class TestGitHubListPullRequests:
    """Tests for github_list_pull_requests function."""

    def test_invalid_state(self):
        """Test that invalid state values are rejected."""
        result = github_list_pull_requests(
            token="ghp_test_token",
            owner="octocat",
            repo="hello-world",
            state="invalid"
        )
        assert result["success"] is False
        assert "State must be" in result["error"]

    @patch("zerg.tools.builtin.github_tools.httpx.Client")
    def test_successful_list_prs(self, mock_client):
        """Test successful PR listing."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "number": 10,
                "title": "Add feature",
                "state": "open",
                "html_url": "https://github.com/octocat/hello-world/pull/10",
                "head": {"ref": "feature-branch"},
                "base": {"ref": "main"},
                "created_at": "2025-11-29T00:00:00Z"
            }
        ]
        mock_client.return_value.__enter__.return_value.request.return_value = mock_response

        result = github_list_pull_requests(
            token="ghp_test_token",
            owner="octocat",
            repo="hello-world",
            state="open"
        )

        assert result["success"] is True
        assert result["count"] == 1
        assert result["data"][0]["number"] == 10
        assert result["data"][0]["head"] == "feature-branch"
        assert result["data"][0]["base"] == "main"


class TestGitHubGetPullRequest:
    """Tests for github_get_pull_request function."""

    def test_invalid_pr_number(self):
        """Test that invalid PR numbers are rejected."""
        result = github_get_pull_request(
            token="ghp_test_token",
            owner="octocat",
            repo="hello-world",
            pr_number=-1
        )
        assert result["success"] is False
        assert "positive integer" in result["error"]

    @patch("zerg.tools.builtin.github_tools.httpx.Client")
    def test_successful_get_pr(self, mock_client):
        """Test successful PR retrieval."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "number": 10,
            "title": "Add feature",
            "body": "This PR adds a new feature",
            "state": "open",
            "html_url": "https://github.com/octocat/hello-world/pull/10",
            "head": {"ref": "feature-branch"},
            "base": {"ref": "main"},
            "mergeable": True,
            "merged": False,
            "created_at": "2025-11-29T00:00:00Z",
            "updated_at": "2025-11-29T01:00:00Z"
        }
        mock_client.return_value.__enter__.return_value.request.return_value = mock_response

        result = github_get_pull_request(
            token="ghp_test_token",
            owner="octocat",
            repo="hello-world",
            pr_number=10
        )

        assert result["success"] is True
        assert result["data"]["number"] == 10
        assert result["data"]["title"] == "Add feature"
        assert result["data"]["head"] == "feature-branch"
        assert result["data"]["base"] == "main"
        assert result["data"]["mergeable"] is True
        assert result["data"]["merged"] is False
