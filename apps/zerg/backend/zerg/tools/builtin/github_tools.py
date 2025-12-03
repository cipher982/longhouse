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

from zerg.connectors.context import get_credential_resolver
from zerg.connectors.registry import ConnectorType
from zerg.tools.error_envelope import (
    tool_error,
    tool_success,
    connector_not_configured_error,
    invalid_credentials_error,
    ErrorType,
)

logger = logging.getLogger(__name__)

# GitHub API base URL
GITHUB_API_BASE = "https://api.github.com"


def _resolve_github_token(token: Optional[str] = None) -> tuple[Optional[str], Optional[dict]]:
    """Resolve GitHub token from parameter or context.

    Returns: (token, error_response) - if error_response is not None, return it.
    """
    resolved_token = token
    if not resolved_token:
        resolver = get_credential_resolver()
        if resolver:
            creds = resolver.get(ConnectorType.GITHUB)
            if creds:
                resolved_token = creds.get("token")

    if not resolved_token:
        return None, connector_not_configured_error("github", "GitHub")
    return resolved_token, None


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
        Dictionary with ok status, data, and any error messages using error envelope
    """
    try:
        if not token or not isinstance(token, str):
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message="Invalid or missing GitHub token",
                connector="github",
            )

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
                return tool_error(
                    error_type=ErrorType.RATE_LIMITED,
                    user_message=f"GitHub API rate limit exceeded. Resets at: {reset_time}",
                    connector="github",
                )

        # Check for authentication errors
        if response.status_code == 401:
            return invalid_credentials_error("github", "GitHub")

        # Check for not found
        if response.status_code == 404:
            return tool_error(
                error_type=ErrorType.EXECUTION_ERROR,
                user_message="Resource not found. Check repository owner, name, and issue/PR number.",
                connector="github",
            )

        # Parse response
        try:
            response_data = response.json()
        except json.JSONDecodeError:
            response_data = response.text

        # Check for success
        if 200 <= response.status_code < 300:
            return tool_success(response_data)
        else:
            error_msg = response_data.get("message", str(response_data)) if isinstance(response_data, dict) else str(response_data)
            return tool_error(
                error_type=ErrorType.EXECUTION_ERROR,
                user_message=error_msg,
                connector="github",
            )

    except httpx.TimeoutException:
        logger.error(f"GitHub API timeout for {method} {endpoint}")
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message=f"Request timed out after {timeout} seconds",
            connector="github",
        )
    except httpx.RequestError as e:
        logger.error(f"GitHub API request error for {method} {endpoint}: {e}")
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message=f"Request failed: {str(e)}",
            connector="github",
        )
    except Exception as e:
        logger.exception(f"Unexpected error in GitHub API request: {method} {endpoint}")
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message=f"Unexpected error: {str(e)}",
            connector="github",
        )


def github_list_repositories(
    token: Optional[str] = None,
    visibility: str = "all",
    sort: str = "updated",
    per_page: int = 30,
) -> Dict[str, Any]:
    """List repositories for the authenticated user.

    Args:
        token: GitHub Personal Access Token (optional if configured in agent)
        visibility: Filter by visibility - 'all', 'public', or 'private' (default: 'all')
        sort: Sort order - 'created', 'updated', 'pushed', or 'full_name' (default: 'updated')
        per_page: Number of results per page, max 100 (default: 30)

    Returns:
        Dictionary containing:
        - ok (bool): Whether the request succeeded
        - data (dict): Repository data with repositories list and count
        - error_type (str): Error type if failed
        - user_message (str): Error message if failed
    """
    resolved_token, error = _resolve_github_token(token)
    if error:
        return error

    params = {
        "visibility": visibility,
        "sort": sort,
        "per_page": min(per_page, 100),
    }

    result = _make_github_request(
        token=resolved_token,
        method="GET",
        endpoint="/user/repos",
        params=params,
    )

    if not result.get("ok"):
        return result

    # Format the response
    repos = result.get("data", [])
    formatted_repos = []
    for repo in repos:
        formatted_repos.append({
            "name": repo.get("full_name"),
            "description": repo.get("description"),
            "url": repo.get("html_url"),
            "private": repo.get("private"),
            "language": repo.get("language"),
            "stars": repo.get("stargazers_count"),
            "updated_at": repo.get("updated_at"),
        })

    return tool_success({
        "repositories": formatted_repos,
        "count": len(formatted_repos),
    })


def github_create_issue(
    owner: str,
    repo: str,
    title: str,
    body: Optional[str] = None,
    labels: Optional[List[str]] = None,
    assignees: Optional[List[str]] = None,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new issue in a GitHub repository.

    Args:
        owner: Repository owner (username or organization)
        repo: Repository name
        title: Issue title (required)
        body: Issue description/body (optional)
        labels: List of label names to apply (optional)
        assignees: List of usernames to assign (optional)
        token: GitHub Personal Access Token (optional, can be configured in Agent Settings)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if the operation succeeded
        - data: Issue details including number, state, and html_url (if successful)
        - error: Error message (if failed)

    Example:
        >>> github_create_issue(
        ...     owner="octocat",
        ...     repo="hello-world",
        ...     title="Bug: Login not working",
        ...     body="Users cannot log in with valid credentials",
        ...     labels=["bug", "priority-high"]
        ... )
        {'success': True, 'data': {'number': 42, 'state': 'open', 'html_url': '...'}}
    """
    resolved_token, error = _resolve_github_token(token)
    if error:
        return error

    if not title or not title.strip():
        return tool_error(
            error_type=ErrorType.VALIDATION_ERROR,
            user_message="Issue title is required and cannot be empty",
            connector="github",
        )

    endpoint = f"/repos/{owner}/{repo}/issues"
    payload = {"title": title.strip()}

    if body:
        payload["body"] = body
    if labels:
        payload["labels"] = labels
    if assignees:
        payload["assignees"] = assignees

    result = _make_github_request(resolved_token, "POST", endpoint, data=payload)

    # Simplify response for agent consumption
    if result.get("ok") and "data" in result:
        issue_data = result["data"]
        return tool_success({
            "number": issue_data.get("number"),
            "title": issue_data.get("title"),
            "state": issue_data.get("state"),
            "html_url": issue_data.get("html_url"),
            "created_at": issue_data.get("created_at")
        })

    return result


def github_list_issues(
    owner: str,
    repo: str,
    state: str = "open",
    labels: Optional[str] = None,
    per_page: int = 30,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    """List issues in a GitHub repository.

    Args:
        owner: Repository owner (username or organization)
        repo: Repository name
        state: Issue state filter: "open", "closed", or "all" (default: "open")
        labels: Comma-separated list of label names to filter by (optional)
        per_page: Number of results per page, max 100 (default: 30)
        token: GitHub Personal Access Token (optional, can be configured in Agent Settings)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if the operation succeeded
        - data: List of issues with number, title, state, and html_url
        - count: Number of issues returned
        - error: Error message (if failed)

    Example:
        >>> github_list_issues(
        ...     owner="octocat",
        ...     repo="hello-world",
        ...     state="open",
        ...     labels="bug,priority-high"
        ... )
        {'success': True, 'data': [...], 'count': 5}
    """
    resolved_token, error = _resolve_github_token(token)
    if error:
        return error

    if state not in ["open", "closed", "all"]:
        return tool_error(
            error_type=ErrorType.VALIDATION_ERROR,
            user_message="State must be 'open', 'closed', or 'all'",
            connector="github",
        )

    if per_page < 1 or per_page > 100:
        return tool_error(
            error_type=ErrorType.VALIDATION_ERROR,
            user_message="per_page must be between 1 and 100",
            connector="github",
        )

    endpoint = f"/repos/{owner}/{repo}/issues"
    params = {
        "state": state,
        "per_page": per_page
    }

    if labels:
        params["labels"] = labels

    result = _make_github_request(resolved_token, "GET", endpoint, params=params)

    # Simplify response for agent consumption
    if result.get("ok") and "data" in result:
        issues_data = result["data"]
        if isinstance(issues_data, list):
            formatted_issues = [
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
            return tool_success({
                "issues": formatted_issues,
                "count": len(formatted_issues)
            })

    return result


def github_get_issue(
    owner: str,
    repo: str,
    issue_number: int,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    """Get details of a specific GitHub issue.

    Args:
        owner: Repository owner (username or organization)
        repo: Repository name
        issue_number: Issue number
        token: GitHub Personal Access Token (optional, can be configured in Agent Settings)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if the operation succeeded
        - data: Issue details including number, title, body, state, and html_url
        - error: Error message (if failed)

    Example:
        >>> github_get_issue(
        ...     owner="octocat",
        ...     repo="hello-world",
        ...     issue_number=42
        ... )
        {'success': True, 'data': {'number': 42, 'title': '...', 'body': '...'}}
    """
    resolved_token, error = _resolve_github_token(token)
    if error:
        return error

    if not isinstance(issue_number, int) or issue_number < 1:
        return tool_error(
            error_type=ErrorType.VALIDATION_ERROR,
            user_message="Issue number must be a positive integer",
            connector="github",
        )

    endpoint = f"/repos/{owner}/{repo}/issues/{issue_number}"
    result = _make_github_request(resolved_token, "GET", endpoint)

    # Simplify response for agent consumption
    if result.get("ok") and "data" in result:
        issue_data = result["data"]
        return tool_success({
            "number": issue_data.get("number"),
            "title": issue_data.get("title"),
            "body": issue_data.get("body"),
            "state": issue_data.get("state"),
            "html_url": issue_data.get("html_url"),
            "labels": [label.get("name") for label in issue_data.get("labels", [])],
            "created_at": issue_data.get("created_at"),
            "updated_at": issue_data.get("updated_at")
        })

    return result


def github_add_comment(
    owner: str,
    repo: str,
    issue_number: int,
    body: str,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    """Add a comment to a GitHub issue or pull request.

    Args:
        owner: Repository owner (username or organization)
        repo: Repository name
        issue_number: Issue or PR number to comment on
        body: Comment text (required)
        token: GitHub Personal Access Token (optional, can be configured in Agent Settings)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if the operation succeeded
        - data: Comment details including id and html_url (if successful)
        - error: Error message (if failed)

    Example:
        >>> github_add_comment(
        ...     owner="octocat",
        ...     repo="hello-world",
        ...     issue_number=42,
        ...     body="Thanks for reporting this! We'll look into it."
        ... )
        {'success': True, 'data': {'id': 12345, 'html_url': '...'}}
    """
    resolved_token, error = _resolve_github_token(token)
    if error:
        return error

    if not body or not body.strip():
        return tool_error(
            error_type=ErrorType.VALIDATION_ERROR,
            user_message="Comment body is required and cannot be empty",
            connector="github",
        )

    if not isinstance(issue_number, int) or issue_number < 1:
        return tool_error(
            error_type=ErrorType.VALIDATION_ERROR,
            user_message="Issue number must be a positive integer",
            connector="github",
        )

    endpoint = f"/repos/{owner}/{repo}/issues/{issue_number}/comments"
    payload = {"body": body.strip()}

    result = _make_github_request(resolved_token, "POST", endpoint, data=payload)

    # Simplify response for agent consumption
    if result.get("ok") and "data" in result:
        comment_data = result["data"]
        return tool_success({
            "id": comment_data.get("id"),
            "body": comment_data.get("body"),
            "html_url": comment_data.get("html_url"),
            "created_at": comment_data.get("created_at")
        })

    return result


def github_list_pull_requests(
    owner: str,
    repo: str,
    state: str = "open",
    per_page: int = 30,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    """List pull requests in a GitHub repository.

    Args:
        owner: Repository owner (username or organization)
        repo: Repository name
        state: PR state filter: "open", "closed", or "all" (default: "open")
        per_page: Number of results per page, max 100 (default: 30)
        token: GitHub Personal Access Token (optional, can be configured in Agent Settings)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if the operation succeeded
        - data: List of PRs with number, title, state, and html_url
        - count: Number of PRs returned
        - error: Error message (if failed)

    Example:
        >>> github_list_pull_requests(
        ...     owner="octocat",
        ...     repo="hello-world",
        ...     state="open"
        ... )
        {'success': True, 'data': [...], 'count': 3}
    """
    resolved_token, error = _resolve_github_token(token)
    if error:
        return error

    if state not in ["open", "closed", "all"]:
        return tool_error(
            error_type=ErrorType.VALIDATION_ERROR,
            user_message="State must be 'open', 'closed', or 'all'",
            connector="github",
        )

    if per_page < 1 or per_page > 100:
        return tool_error(
            error_type=ErrorType.VALIDATION_ERROR,
            user_message="per_page must be between 1 and 100",
            connector="github",
        )

    endpoint = f"/repos/{owner}/{repo}/pulls"
    params = {
        "state": state,
        "per_page": per_page
    }

    result = _make_github_request(resolved_token, "GET", endpoint, params=params)

    # Simplify response for agent consumption
    if result.get("ok") and "data" in result:
        prs_data = result["data"]
        if isinstance(prs_data, list):
            formatted_prs = [
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
            return tool_success({
                "pull_requests": formatted_prs,
                "count": len(formatted_prs)
            })

    return result


def github_get_pull_request(
    owner: str,
    repo: str,
    pr_number: int,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    """Get details of a specific GitHub pull request.

    Args:
        owner: Repository owner (username or organization)
        repo: Repository name
        pr_number: Pull request number
        token: GitHub Personal Access Token (optional, can be configured in Agent Settings)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if the operation succeeded
        - data: PR details including number, title, body, state, and branch info
        - error: Error message (if failed)

    Example:
        >>> github_get_pull_request(
        ...     owner="octocat",
        ...     repo="hello-world",
        ...     pr_number=10
        ... )
        {'success': True, 'data': {'number': 10, 'title': '...', 'head': 'feature', 'base': 'main'}}
    """
    resolved_token, error = _resolve_github_token(token)
    if error:
        return error

    if not isinstance(pr_number, int) or pr_number < 1:
        return tool_error(
            error_type=ErrorType.VALIDATION_ERROR,
            user_message="PR number must be a positive integer",
            connector="github",
        )

    endpoint = f"/repos/{owner}/{repo}/pulls/{pr_number}"
    result = _make_github_request(resolved_token, "GET", endpoint)

    # Simplify response for agent consumption
    if result.get("ok") and "data" in result:
        pr_data = result["data"]
        return tool_success({
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
        })

    return result


# Register tools with LangChain
TOOLS: List[StructuredTool] = [
    StructuredTool.from_function(
        func=github_list_repositories,
        name="github_list_repositories",
        description="List all repositories for the authenticated user. Shows repo name, description, URL, language, and stars. Token can be provided or configured in Agent Settings -> Connectors.",
    ),
    StructuredTool.from_function(
        func=github_create_issue,
        name="github_create_issue",
        description="Create a new issue in a GitHub repository. Token can be provided or configured in Agent Settings -> Connectors. Returns the issue number and URL.",
    ),
    StructuredTool.from_function(
        func=github_list_issues,
        name="github_list_issues",
        description="List issues in a GitHub repository with optional filtering by state and labels. Token can be provided or configured in Agent Settings -> Connectors.",
    ),
    StructuredTool.from_function(
        func=github_get_issue,
        name="github_get_issue",
        description="Get detailed information about a specific GitHub issue by number. Token can be provided or configured in Agent Settings -> Connectors.",
    ),
    StructuredTool.from_function(
        func=github_add_comment,
        name="github_add_comment",
        description="Add a comment to an existing GitHub issue or pull request. Token can be provided or configured in Agent Settings -> Connectors.",
    ),
    StructuredTool.from_function(
        func=github_list_pull_requests,
        name="github_list_pull_requests",
        description="List pull requests in a GitHub repository with optional filtering by state. Token can be provided or configured in Agent Settings -> Connectors.",
    ),
    StructuredTool.from_function(
        func=github_get_pull_request,
        name="github_get_pull_request",
        description="Get detailed information about a specific GitHub pull request by number. Token can be provided or configured in Agent Settings -> Connectors.",
    ),
]
