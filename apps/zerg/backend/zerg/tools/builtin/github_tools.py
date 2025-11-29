"""GitHub-related tools for interacting with GitHub repositories.

This module provides tools for managing GitHub issues, pull requests, and comments
via the GitHub REST API. All tools require a GitHub Personal Access Token (PAT) for
authentication.

Required PAT Permissions:
    - repo (full repository access) - for private repositories
    - public_repo - for public repositories only

Configuration:
    Agents should be configured with a GitHub connector that provides:
    - token: GitHub Personal Access Token (PAT)

Rate Limits:
    - Authenticated requests: 5,000 requests/hour
    - Check rate limit: GET /rate_limit
"""

import json
import logging
from typing import Any, Dict, List, Optional

import httpx
from langchain_core.tools import StructuredTool

logger = logging.getLogger(__name__)

# GitHub API base URL
GITHUB_API_BASE = "https://api.github.com"


def _make_github_request(
    token: str,
    method: str,
    endpoint: str,
    data: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """Internal helper to make authenticated GitHub API requests.

    Args:
        token: GitHub Personal Access Token
        method: HTTP method (GET, POST, PATCH, DELETE, etc.)
        endpoint: API endpoint path (e.g., "/repos/owner/repo/issues")
        data: Optional request body data
        params: Optional query parameters
        timeout: Request timeout in seconds

    Returns:
        Dictionary with success status, data, and any error messages
    """
    try:
        if not token or not isinstance(token, str):
            return {
                "success": False,
                "error": "Invalid or missing GitHub token"
            }

        url = f"{GITHUB_API_BASE}{endpoint}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Zerg-Agent/1.0"
        }

        with httpx.Client() as client:
            response = client.request(
                method=method.upper(),
                url=url,
                headers=headers,
                json=data,
                params=params,
                timeout=timeout,
                follow_redirects=True
            )

        # Check for rate limit
        if response.status_code == 403:
            rate_limit_remaining = response.headers.get("x-ratelimit-remaining", "unknown")
            if rate_limit_remaining == "0":
                reset_time = response.headers.get("x-ratelimit-reset", "unknown")
                return {
                    "success": False,
                    "error": f"GitHub API rate limit exceeded. Resets at: {reset_time}",
                    "status_code": 403
                }

        # Check for authentication errors
        if response.status_code == 401:
            return {
                "success": False,
                "error": "GitHub authentication failed. Check your Personal Access Token.",
                "status_code": 401
            }

        # Check for not found
        if response.status_code == 404:
            return {
                "success": False,
                "error": "Resource not found. Check repository owner, name, and issue/PR number.",
                "status_code": 404
            }

        # Parse response
        try:
            response_data = response.json()
        except json.JSONDecodeError:
            response_data = response.text

        # Check for success
        if 200 <= response.status_code < 300:
            return {
                "success": True,
                "data": response_data,
                "status_code": response.status_code
            }
        else:
            error_msg = response_data.get("message", str(response_data)) if isinstance(response_data, dict) else str(response_data)
            return {
                "success": False,
                "error": error_msg,
                "status_code": response.status_code
            }

    except httpx.TimeoutException:
        logger.error(f"GitHub API timeout for {method} {endpoint}")
        return {
            "success": False,
            "error": f"Request timed out after {timeout} seconds"
        }
    except httpx.RequestError as e:
        logger.error(f"GitHub API request error for {method} {endpoint}: {e}")
        return {
            "success": False,
            "error": f"Request failed: {str(e)}"
        }
    except Exception as e:
        logger.exception(f"Unexpected error in GitHub API request: {method} {endpoint}")
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }


def github_create_issue(
    token: str,
    owner: str,
    repo: str,
    title: str,
    body: Optional[str] = None,
    labels: Optional[List[str]] = None,
    assignees: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Create a new issue in a GitHub repository.

    Args:
        token: GitHub Personal Access Token
        owner: Repository owner (username or organization)
        repo: Repository name
        title: Issue title (required)
        body: Issue description/body (optional)
        labels: List of label names to apply (optional)
        assignees: List of usernames to assign (optional)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if the operation succeeded
        - data: Issue details including number, state, and html_url (if successful)
        - error: Error message (if failed)

    Example:
        >>> github_create_issue(
        ...     token="ghp_xxxxx",
        ...     owner="octocat",
        ...     repo="hello-world",
        ...     title="Bug: Login not working",
        ...     body="Users cannot log in with valid credentials",
        ...     labels=["bug", "priority-high"]
        ... )
        {'success': True, 'data': {'number': 42, 'state': 'open', 'html_url': '...'}}
    """
    if not title or not title.strip():
        return {
            "success": False,
            "error": "Issue title is required and cannot be empty"
        }

    endpoint = f"/repos/{owner}/{repo}/issues"
    payload = {"title": title.strip()}

    if body:
        payload["body"] = body
    if labels:
        payload["labels"] = labels
    if assignees:
        payload["assignees"] = assignees

    result = _make_github_request(token, "POST", endpoint, data=payload)

    # Simplify response for agent consumption
    if result.get("success") and "data" in result:
        issue_data = result["data"]
        result["data"] = {
            "number": issue_data.get("number"),
            "title": issue_data.get("title"),
            "state": issue_data.get("state"),
            "html_url": issue_data.get("html_url"),
            "created_at": issue_data.get("created_at")
        }

    return result


def github_list_issues(
    token: str,
    owner: str,
    repo: str,
    state: str = "open",
    labels: Optional[str] = None,
    per_page: int = 30,
) -> Dict[str, Any]:
    """List issues in a GitHub repository.

    Args:
        token: GitHub Personal Access Token
        owner: Repository owner (username or organization)
        repo: Repository name
        state: Issue state filter: "open", "closed", or "all" (default: "open")
        labels: Comma-separated list of label names to filter by (optional)
        per_page: Number of results per page, max 100 (default: 30)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if the operation succeeded
        - data: List of issues with number, title, state, and html_url
        - count: Number of issues returned
        - error: Error message (if failed)

    Example:
        >>> github_list_issues(
        ...     token="ghp_xxxxx",
        ...     owner="octocat",
        ...     repo="hello-world",
        ...     state="open",
        ...     labels="bug,priority-high"
        ... )
        {'success': True, 'data': [...], 'count': 5}
    """
    if state not in ["open", "closed", "all"]:
        return {
            "success": False,
            "error": "State must be 'open', 'closed', or 'all'"
        }

    if per_page < 1 or per_page > 100:
        return {
            "success": False,
            "error": "per_page must be between 1 and 100"
        }

    endpoint = f"/repos/{owner}/{repo}/issues"
    params = {
        "state": state,
        "per_page": per_page
    }

    if labels:
        params["labels"] = labels

    result = _make_github_request(token, "GET", endpoint, params=params)

    # Simplify response for agent consumption
    if result.get("success") and "data" in result:
        issues_data = result["data"]
        if isinstance(issues_data, list):
            result["data"] = [
                {
                    "number": issue.get("number"),
                    "title": issue.get("title"),
                    "state": issue.get("state"),
                    "html_url": issue.get("html_url"),
                    "labels": [label.get("name") for label in issue.get("labels", [])],
                    "created_at": issue.get("created_at")
                }
                for issue in issues_data
            ]
            result["count"] = len(result["data"])

    return result


def github_get_issue(
    token: str,
    owner: str,
    repo: str,
    issue_number: int,
) -> Dict[str, Any]:
    """Get details of a specific GitHub issue.

    Args:
        token: GitHub Personal Access Token
        owner: Repository owner (username or organization)
        repo: Repository name
        issue_number: Issue number

    Returns:
        Dictionary containing:
        - success: Boolean indicating if the operation succeeded
        - data: Issue details including number, title, body, state, and html_url
        - error: Error message (if failed)

    Example:
        >>> github_get_issue(
        ...     token="ghp_xxxxx",
        ...     owner="octocat",
        ...     repo="hello-world",
        ...     issue_number=42
        ... )
        {'success': True, 'data': {'number': 42, 'title': '...', 'body': '...'}}
    """
    if not isinstance(issue_number, int) or issue_number < 1:
        return {
            "success": False,
            "error": "Issue number must be a positive integer"
        }

    endpoint = f"/repos/{owner}/{repo}/issues/{issue_number}"
    result = _make_github_request(token, "GET", endpoint)

    # Simplify response for agent consumption
    if result.get("success") and "data" in result:
        issue_data = result["data"]
        result["data"] = {
            "number": issue_data.get("number"),
            "title": issue_data.get("title"),
            "body": issue_data.get("body"),
            "state": issue_data.get("state"),
            "html_url": issue_data.get("html_url"),
            "labels": [label.get("name") for label in issue_data.get("labels", [])],
            "created_at": issue_data.get("created_at"),
            "updated_at": issue_data.get("updated_at")
        }

    return result


def github_add_comment(
    token: str,
    owner: str,
    repo: str,
    issue_number: int,
    body: str,
) -> Dict[str, Any]:
    """Add a comment to a GitHub issue or pull request.

    Args:
        token: GitHub Personal Access Token
        owner: Repository owner (username or organization)
        repo: Repository name
        issue_number: Issue or PR number to comment on
        body: Comment text (required)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if the operation succeeded
        - data: Comment details including id and html_url (if successful)
        - error: Error message (if failed)

    Example:
        >>> github_add_comment(
        ...     token="ghp_xxxxx",
        ...     owner="octocat",
        ...     repo="hello-world",
        ...     issue_number=42,
        ...     body="Thanks for reporting this! We'll look into it."
        ... )
        {'success': True, 'data': {'id': 12345, 'html_url': '...'}}
    """
    if not body or not body.strip():
        return {
            "success": False,
            "error": "Comment body is required and cannot be empty"
        }

    if not isinstance(issue_number, int) or issue_number < 1:
        return {
            "success": False,
            "error": "Issue number must be a positive integer"
        }

    endpoint = f"/repos/{owner}/{repo}/issues/{issue_number}/comments"
    payload = {"body": body.strip()}

    result = _make_github_request(token, "POST", endpoint, data=payload)

    # Simplify response for agent consumption
    if result.get("success") and "data" in result:
        comment_data = result["data"]
        result["data"] = {
            "id": comment_data.get("id"),
            "body": comment_data.get("body"),
            "html_url": comment_data.get("html_url"),
            "created_at": comment_data.get("created_at")
        }

    return result


def github_list_pull_requests(
    token: str,
    owner: str,
    repo: str,
    state: str = "open",
    per_page: int = 30,
) -> Dict[str, Any]:
    """List pull requests in a GitHub repository.

    Args:
        token: GitHub Personal Access Token
        owner: Repository owner (username or organization)
        repo: Repository name
        state: PR state filter: "open", "closed", or "all" (default: "open")
        per_page: Number of results per page, max 100 (default: 30)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if the operation succeeded
        - data: List of PRs with number, title, state, and html_url
        - count: Number of PRs returned
        - error: Error message (if failed)

    Example:
        >>> github_list_pull_requests(
        ...     token="ghp_xxxxx",
        ...     owner="octocat",
        ...     repo="hello-world",
        ...     state="open"
        ... )
        {'success': True, 'data': [...], 'count': 3}
    """
    if state not in ["open", "closed", "all"]:
        return {
            "success": False,
            "error": "State must be 'open', 'closed', or 'all'"
        }

    if per_page < 1 or per_page > 100:
        return {
            "success": False,
            "error": "per_page must be between 1 and 100"
        }

    endpoint = f"/repos/{owner}/{repo}/pulls"
    params = {
        "state": state,
        "per_page": per_page
    }

    result = _make_github_request(token, "GET", endpoint, params=params)

    # Simplify response for agent consumption
    if result.get("success") and "data" in result:
        prs_data = result["data"]
        if isinstance(prs_data, list):
            result["data"] = [
                {
                    "number": pr.get("number"),
                    "title": pr.get("title"),
                    "state": pr.get("state"),
                    "html_url": pr.get("html_url"),
                    "created_at": pr.get("created_at"),
                    "head": pr.get("head", {}).get("ref"),
                    "base": pr.get("base", {}).get("ref")
                }
                for pr in prs_data
            ]
            result["count"] = len(result["data"])

    return result


def github_get_pull_request(
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
) -> Dict[str, Any]:
    """Get details of a specific GitHub pull request.

    Args:
        token: GitHub Personal Access Token
        owner: Repository owner (username or organization)
        repo: Repository name
        pr_number: Pull request number

    Returns:
        Dictionary containing:
        - success: Boolean indicating if the operation succeeded
        - data: PR details including number, title, body, state, and branch info
        - error: Error message (if failed)

    Example:
        >>> github_get_pull_request(
        ...     token="ghp_xxxxx",
        ...     owner="octocat",
        ...     repo="hello-world",
        ...     pr_number=10
        ... )
        {'success': True, 'data': {'number': 10, 'title': '...', 'head': 'feature', 'base': 'main'}}
    """
    if not isinstance(pr_number, int) or pr_number < 1:
        return {
            "success": False,
            "error": "PR number must be a positive integer"
        }

    endpoint = f"/repos/{owner}/{repo}/pulls/{pr_number}"
    result = _make_github_request(token, "GET", endpoint)

    # Simplify response for agent consumption
    if result.get("success") and "data" in result:
        pr_data = result["data"]
        result["data"] = {
            "number": pr_data.get("number"),
            "title": pr_data.get("title"),
            "body": pr_data.get("body"),
            "state": pr_data.get("state"),
            "html_url": pr_data.get("html_url"),
            "head": pr_data.get("head", {}).get("ref"),
            "base": pr_data.get("base", {}).get("ref"),
            "mergeable": pr_data.get("mergeable"),
            "merged": pr_data.get("merged"),
            "created_at": pr_data.get("created_at"),
            "updated_at": pr_data.get("updated_at")
        }

    return result


# Register tools with LangChain
TOOLS: List[StructuredTool] = [
    StructuredTool.from_function(
        func=github_create_issue,
        name="github_create_issue",
        description="Create a new issue in a GitHub repository. Returns the issue number and URL.",
    ),
    StructuredTool.from_function(
        func=github_list_issues,
        name="github_list_issues",
        description="List issues in a GitHub repository with optional filtering by state and labels.",
    ),
    StructuredTool.from_function(
        func=github_get_issue,
        name="github_get_issue",
        description="Get detailed information about a specific GitHub issue by number.",
    ),
    StructuredTool.from_function(
        func=github_add_comment,
        name="github_add_comment",
        description="Add a comment to an existing GitHub issue or pull request.",
    ),
    StructuredTool.from_function(
        func=github_list_pull_requests,
        name="github_list_pull_requests",
        description="List pull requests in a GitHub repository with optional filtering by state.",
    ),
    StructuredTool.from_function(
        func=github_get_pull_request,
        name="github_get_pull_request",
        description="Get detailed information about a specific GitHub pull request by number.",
    ),
]
