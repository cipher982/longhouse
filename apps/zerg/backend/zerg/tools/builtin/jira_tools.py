"""Jira Cloud REST API tools for issue management.

These tools enable agents to interact with Jira Cloud projects using the REST API v3.
All operations require authentication via email and API token.

API Token Generation:
    https://id.atlassian.com/manage-profile/security/api-tokens

Rate Limits:
    Jira Cloud enforces per-tenant rate limits. This implementation does not include
    automatic retry logic - agents should handle 429 responses appropriately.

Reference:
    https://developer.atlassian.com/cloud/jira/platform/rest/v3/
"""

import base64
import json
import logging
from typing import Any, Dict, List, Optional

import httpx
from langchain_core.tools import StructuredTool

logger = logging.getLogger(__name__)


def _build_jira_auth_header(email: str, api_token: str) -> str:
    """Build Basic Authentication header for Jira API.

    Args:
        email: User email address
        api_token: API token from Atlassian account settings

    Returns:
        Base64 encoded authorization header value
    """
    auth_str = f"{email}:{api_token}"
    encoded = base64.b64encode(auth_str.encode()).decode()
    return f"Basic {encoded}"


def _build_jira_url(domain: str, endpoint: str) -> str:
    """Build full Jira Cloud REST API v3 URL.

    Args:
        domain: Jira Cloud domain (e.g., 'yourcompany.atlassian.net')
        endpoint: API endpoint path (e.g., '/issue')

    Returns:
        Full URL for the API request
    """
    # Clean domain of protocol and trailing slashes
    domain = domain.replace("https://", "").replace("http://", "").rstrip("/")
    base_url = f"https://{domain}/rest/api/3"
    return f"{base_url}{endpoint}"


def _text_to_adf(text: str) -> Dict[str, Any]:
    """Convert plain text to Atlassian Document Format (ADF).

    Jira Cloud API v3 uses ADF for rich text fields like descriptions and comments.

    Args:
        text: Plain text to convert

    Returns:
        ADF document structure
    """
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": text}],
            }
        ],
    }


def jira_create_issue(
    domain: str,
    email: str,
    api_token: str,
    project_key: str,
    issue_type: str,
    summary: str,
    description: Optional[str] = None,
    priority: Optional[str] = None,
    labels: Optional[List[str]] = None,
    assignee: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new issue in a Jira project.

    Args:
        domain: Jira Cloud domain (e.g., 'yourcompany.atlassian.net')
        email: User email for authentication
        api_token: API token from Atlassian account settings
        project_key: Project key (e.g., 'PROJ', 'TEAM')
        issue_type: Issue type name (e.g., 'Task', 'Bug', 'Story')
        summary: Issue summary/title (required)
        description: Issue description in plain text (optional)
        priority: Priority name (e.g., 'High', 'Medium', 'Low') (optional)
        labels: List of label strings (optional)
        assignee: Assignee account ID - NOT email or name (optional)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if issue was created
        - issue_key: Created issue key (e.g., 'PROJ-123')
        - issue_id: Created issue ID
        - url: Direct URL to the created issue
        - error: Error message if request failed

    Example:
        >>> jira_create_issue(
        ...     domain="company.atlassian.net",
        ...     email="user@company.com",
        ...     api_token="abc123",
        ...     project_key="PROJ",
        ...     issue_type="Task",
        ...     summary="Implement new feature",
        ...     description="Feature description here",
        ...     priority="High",
        ...     labels=["automation", "agent"]
        ... )
        {
            "success": True,
            "issue_key": "PROJ-123",
            "issue_id": "10001",
            "url": "https://company.atlassian.net/browse/PROJ-123"
        }
    """
    try:
        # Validate required fields
        if not all([domain, email, api_token, project_key, issue_type, summary]):
            return {
                "success": False,
                "error": "Missing required fields: domain, email, api_token, project_key, issue_type, summary",
            }

        # Build payload
        payload = {
            "fields": {
                "project": {"key": project_key},
                "issuetype": {"name": issue_type},
                "summary": summary,
            }
        }

        # Add optional fields
        if description:
            payload["fields"]["description"] = _text_to_adf(description)

        if priority:
            payload["fields"]["priority"] = {"name": priority}

        if labels:
            if not isinstance(labels, list):
                return {"success": False, "error": "Labels must be a list of strings"}
            payload["fields"]["labels"] = labels

        if assignee:
            payload["fields"]["assignee"] = {"accountId": assignee}

        # Build URL and headers
        url = _build_jira_url(domain, "/issue")
        headers = {
            "Authorization": _build_jira_auth_header(email, api_token),
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Zerg-Agent/1.0",
        }

        # Make request
        with httpx.Client() as client:
            response = client.post(url, json=payload, headers=headers, timeout=30.0)

        # Handle success
        if response.status_code == 201:
            data = response.json()
            issue_key = data.get("key")
            issue_id = data.get("id")
            domain_clean = domain.replace("https://", "").replace("http://", "").rstrip("/")
            issue_url = f"https://{domain_clean}/browse/{issue_key}"

            logger.info(f"Created Jira issue: {issue_key}")
            return {
                "success": True,
                "issue_key": issue_key,
                "issue_id": issue_id,
                "url": issue_url,
            }

        # Handle errors
        return _handle_jira_error(response, "create issue")

    except httpx.TimeoutException:
        logger.error(f"Jira API timeout for domain: {domain}")
        return {"success": False, "error": "Request timed out after 30 seconds"}
    except httpx.RequestError as e:
        logger.error(f"Jira API request error: {e}")
        return {"success": False, "error": f"Request failed: {str(e)}"}
    except Exception as e:
        logger.exception("Unexpected error in jira_create_issue")
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


def jira_list_issues(
    domain: str,
    email: str,
    api_token: str,
    project_key: str,
    jql: Optional[str] = None,
    max_results: int = 50,
) -> Dict[str, Any]:
    """List issues in a Jira project using JQL search.

    Args:
        domain: Jira Cloud domain (e.g., 'yourcompany.atlassian.net')
        email: User email for authentication
        api_token: API token from Atlassian account settings
        project_key: Project key to search within
        jql: Custom JQL query (optional, defaults to all project issues)
        max_results: Maximum number of results to return (default: 50, max: 100)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if search succeeded
        - total: Total number of matching issues
        - issues: List of issue objects with key, summary, status, etc.
        - error: Error message if request failed

    Example:
        >>> jira_list_issues(
        ...     domain="company.atlassian.net",
        ...     email="user@company.com",
        ...     api_token="abc123",
        ...     project_key="PROJ",
        ...     jql="status = 'In Progress' AND assignee = currentUser()",
        ...     max_results=20
        ... )
        {
            "success": True,
            "total": 15,
            "issues": [
                {
                    "key": "PROJ-123",
                    "summary": "Issue title",
                    "status": "In Progress",
                    "assignee": "John Doe",
                    "priority": "High"
                },
                ...
            ]
        }
    """
    try:
        # Validate required fields
        if not all([domain, email, api_token, project_key]):
            return {"success": False, "error": "Missing required fields: domain, email, api_token, project_key"}

        # Build JQL query
        if jql:
            query = jql
        else:
            query = f"project = {project_key} ORDER BY created DESC"

        # Limit max_results to reasonable bounds
        max_results = max(1, min(max_results, 100))

        # Build URL with query parameters (using new v3 /search/jql endpoint)
        url = _build_jira_url(domain, "/search/jql")
        params = {"jql": query, "maxResults": max_results}

        headers = {
            "Authorization": _build_jira_auth_header(email, api_token),
            "Accept": "application/json",
            "User-Agent": "Zerg-Agent/1.0",
        }

        # Make request
        with httpx.Client() as client:
            response = client.get(url, params=params, headers=headers, timeout=30.0)

        # Handle success
        if response.status_code == 200:
            data = response.json()
            total = data.get("total", 0)
            raw_issues = data.get("issues", [])

            # Extract key fields for easier agent consumption
            issues = []
            for issue in raw_issues:
                fields = issue.get("fields", {})
                issue_info = {
                    "key": issue.get("key"),
                    "id": issue.get("id"),
                    "summary": fields.get("summary"),
                    "status": fields.get("status", {}).get("name"),
                    "issue_type": fields.get("issuetype", {}).get("name"),
                    "priority": fields.get("priority", {}).get("name") if fields.get("priority") else None,
                    "assignee": fields.get("assignee", {}).get("displayName") if fields.get("assignee") else None,
                    "created": fields.get("created"),
                    "updated": fields.get("updated"),
                }
                issues.append(issue_info)

            logger.info(f"Listed {len(issues)} Jira issues from project {project_key}")
            return {
                "success": True,
                "total": total,
                "returned": len(issues),
                "issues": issues,
            }

        # Handle errors
        return _handle_jira_error(response, "list issues")

    except httpx.TimeoutException:
        logger.error(f"Jira API timeout for domain: {domain}")
        return {"success": False, "error": "Request timed out after 30 seconds"}
    except httpx.RequestError as e:
        logger.error(f"Jira API request error: {e}")
        return {"success": False, "error": f"Request failed: {str(e)}"}
    except Exception as e:
        logger.exception("Unexpected error in jira_list_issues")
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


def jira_get_issue(domain: str, email: str, api_token: str, issue_key: str) -> Dict[str, Any]:
    """Get detailed information about a specific Jira issue.

    Args:
        domain: Jira Cloud domain (e.g., 'yourcompany.atlassian.net')
        email: User email for authentication
        api_token: API token from Atlassian account settings
        issue_key: Issue key (e.g., 'PROJ-123')

    Returns:
        Dictionary containing:
        - success: Boolean indicating if request succeeded
        - issue: Detailed issue object with all fields
        - error: Error message if request failed

    Example:
        >>> jira_get_issue(
        ...     domain="company.atlassian.net",
        ...     email="user@company.com",
        ...     api_token="abc123",
        ...     issue_key="PROJ-123"
        ... )
        {
            "success": True,
            "issue": {
                "key": "PROJ-123",
                "summary": "Issue title",
                "description": "Detailed description...",
                "status": "In Progress",
                ...
            }
        }
    """
    try:
        # Validate required fields
        if not all([domain, email, api_token, issue_key]):
            return {"success": False, "error": "Missing required fields: domain, email, api_token, issue_key"}

        # Build URL
        url = _build_jira_url(domain, f"/issue/{issue_key}")

        headers = {
            "Authorization": _build_jira_auth_header(email, api_token),
            "Accept": "application/json",
            "User-Agent": "Zerg-Agent/1.0",
        }

        # Make request
        with httpx.Client() as client:
            response = client.get(url, headers=headers, timeout=30.0)

        # Handle success
        if response.status_code == 200:
            data = response.json()
            fields = data.get("fields", {})

            # Extract description from ADF format if present
            description = None
            if fields.get("description"):
                # Try to extract plain text from ADF
                try:
                    adf_content = fields["description"].get("content", [])
                    text_parts = []
                    for block in adf_content:
                        if block.get("type") == "paragraph":
                            for content_item in block.get("content", []):
                                if content_item.get("type") == "text":
                                    text_parts.append(content_item.get("text", ""))
                    description = "\n".join(text_parts)
                except Exception:
                    description = str(fields.get("description"))

            # Build detailed issue info
            issue = {
                "key": data.get("key"),
                "id": data.get("id"),
                "summary": fields.get("summary"),
                "description": description,
                "status": fields.get("status", {}).get("name"),
                "issue_type": fields.get("issuetype", {}).get("name"),
                "priority": fields.get("priority", {}).get("name") if fields.get("priority") else None,
                "assignee": fields.get("assignee", {}).get("displayName") if fields.get("assignee") else None,
                "reporter": fields.get("reporter", {}).get("displayName") if fields.get("reporter") else None,
                "created": fields.get("created"),
                "updated": fields.get("updated"),
                "labels": fields.get("labels", []),
            }

            logger.info(f"Retrieved Jira issue: {issue_key}")
            return {"success": True, "issue": issue}

        # Handle errors
        return _handle_jira_error(response, "get issue")

    except httpx.TimeoutException:
        logger.error(f"Jira API timeout for domain: {domain}")
        return {"success": False, "error": "Request timed out after 30 seconds"}
    except httpx.RequestError as e:
        logger.error(f"Jira API request error: {e}")
        return {"success": False, "error": f"Request failed: {str(e)}"}
    except Exception as e:
        logger.exception("Unexpected error in jira_get_issue")
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


def jira_add_comment(domain: str, email: str, api_token: str, issue_key: str, body: str) -> Dict[str, Any]:
    """Add a comment to a Jira issue.

    Args:
        domain: Jira Cloud domain (e.g., 'yourcompany.atlassian.net')
        email: User email for authentication
        api_token: API token from Atlassian account settings
        issue_key: Issue key (e.g., 'PROJ-123')
        body: Comment text in plain text

    Returns:
        Dictionary containing:
        - success: Boolean indicating if comment was added
        - comment_id: ID of the created comment
        - error: Error message if request failed

    Example:
        >>> jira_add_comment(
        ...     domain="company.atlassian.net",
        ...     email="user@company.com",
        ...     api_token="abc123",
        ...     issue_key="PROJ-123",
        ...     body="Agent completed the task successfully"
        ... )
        {"success": True, "comment_id": "10050"}
    """
    try:
        # Validate required fields
        if not all([domain, email, api_token, issue_key, body]):
            return {"success": False, "error": "Missing required fields: domain, email, api_token, issue_key, body"}

        # Build payload with ADF format
        payload = {"body": _text_to_adf(body)}

        # Build URL
        url = _build_jira_url(domain, f"/issue/{issue_key}/comment")

        headers = {
            "Authorization": _build_jira_auth_header(email, api_token),
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Zerg-Agent/1.0",
        }

        # Make request
        with httpx.Client() as client:
            response = client.post(url, json=payload, headers=headers, timeout=30.0)

        # Handle success
        if response.status_code == 201:
            data = response.json()
            comment_id = data.get("id")

            logger.info(f"Added comment to Jira issue {issue_key}")
            return {"success": True, "comment_id": comment_id}

        # Handle errors
        return _handle_jira_error(response, "add comment")

    except httpx.TimeoutException:
        logger.error(f"Jira API timeout for domain: {domain}")
        return {"success": False, "error": "Request timed out after 30 seconds"}
    except httpx.RequestError as e:
        logger.error(f"Jira API request error: {e}")
        return {"success": False, "error": f"Request failed: {str(e)}"}
    except Exception as e:
        logger.exception("Unexpected error in jira_add_comment")
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


def jira_transition_issue(domain: str, email: str, api_token: str, issue_key: str, transition_id: str) -> Dict[str, Any]:
    """Transition a Jira issue to a new status.

    Note: To find available transitions for an issue, use jira_get_issue and check
    the available transitions, or query /rest/api/3/issue/{issueKey}/transitions

    Args:
        domain: Jira Cloud domain (e.g., 'yourcompany.atlassian.net')
        email: User email for authentication
        api_token: API token from Atlassian account settings
        issue_key: Issue key (e.g., 'PROJ-123')
        transition_id: Transition ID as string (e.g., '21' for 'In Progress -> Done')

    Returns:
        Dictionary containing:
        - success: Boolean indicating if transition succeeded
        - error: Error message if request failed

    Example:
        >>> jira_transition_issue(
        ...     domain="company.atlassian.net",
        ...     email="user@company.com",
        ...     api_token="abc123",
        ...     issue_key="PROJ-123",
        ...     transition_id="21"
        ... )
        {"success": True}
    """
    try:
        # Validate required fields
        if not all([domain, email, api_token, issue_key, transition_id]):
            return {
                "success": False,
                "error": "Missing required fields: domain, email, api_token, issue_key, transition_id",
            }

        # Build payload
        payload = {"transition": {"id": str(transition_id)}}

        # Build URL
        url = _build_jira_url(domain, f"/issue/{issue_key}/transitions")

        headers = {
            "Authorization": _build_jira_auth_header(email, api_token),
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Zerg-Agent/1.0",
        }

        # Make request
        with httpx.Client() as client:
            response = client.post(url, json=payload, headers=headers, timeout=30.0)

        # Handle success (204 No Content)
        if response.status_code == 204:
            logger.info(f"Transitioned Jira issue {issue_key} with transition ID {transition_id}")
            return {"success": True}

        # Handle errors
        return _handle_jira_error(response, "transition issue")

    except httpx.TimeoutException:
        logger.error(f"Jira API timeout for domain: {domain}")
        return {"success": False, "error": "Request timed out after 30 seconds"}
    except httpx.RequestError as e:
        logger.error(f"Jira API request error: {e}")
        return {"success": False, "error": f"Request failed: {str(e)}"}
    except Exception as e:
        logger.exception("Unexpected error in jira_transition_issue")
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


def jira_update_issue(domain: str, email: str, api_token: str, issue_key: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    """Update fields on an existing Jira issue.

    Args:
        domain: Jira Cloud domain (e.g., 'yourcompany.atlassian.net')
        email: User email for authentication
        api_token: API token from Atlassian account settings
        issue_key: Issue key (e.g., 'PROJ-123')
        fields: Dictionary of field updates (e.g., {"summary": "New title", "priority": {"name": "Low"}})

    Returns:
        Dictionary containing:
        - success: Boolean indicating if update succeeded
        - error: Error message if request failed

    Example:
        >>> jira_update_issue(
        ...     domain="company.atlassian.net",
        ...     email="user@company.com",
        ...     api_token="abc123",
        ...     issue_key="PROJ-123",
        ...     fields={"summary": "Updated title", "priority": {"name": "Low"}}
        ... )
        {"success": True}
    """
    try:
        # Validate required fields
        if not all([domain, email, api_token, issue_key, fields]):
            return {"success": False, "error": "Missing required fields: domain, email, api_token, issue_key, fields"}

        if not isinstance(fields, dict):
            return {"success": False, "error": "Fields must be a dictionary"}

        # Build payload
        payload = {"fields": fields}

        # Build URL
        url = _build_jira_url(domain, f"/issue/{issue_key}")

        headers = {
            "Authorization": _build_jira_auth_header(email, api_token),
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Zerg-Agent/1.0",
        }

        # Make request
        with httpx.Client() as client:
            response = client.put(url, json=payload, headers=headers, timeout=30.0)

        # Handle success (204 No Content)
        if response.status_code == 204:
            logger.info(f"Updated Jira issue {issue_key}")
            return {"success": True}

        # Handle errors
        return _handle_jira_error(response, "update issue")

    except httpx.TimeoutException:
        logger.error(f"Jira API timeout for domain: {domain}")
        return {"success": False, "error": "Request timed out after 30 seconds"}
    except httpx.RequestError as e:
        logger.error(f"Jira API request error: {e}")
        return {"success": False, "error": f"Request failed: {str(e)}"}
    except Exception as e:
        logger.exception("Unexpected error in jira_update_issue")
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


def _handle_jira_error(response: httpx.Response, operation: str) -> Dict[str, Any]:
    """Handle Jira API error responses consistently.

    Args:
        response: httpx Response object
        operation: Description of the operation that failed

    Returns:
        Dictionary with success=False and error details
    """
    status_code = response.status_code

    # Try to extract error message from response
    try:
        error_data = response.json()
        error_messages = error_data.get("errorMessages", [])
        errors_dict = error_data.get("errors", {})

        if error_messages:
            error_msg = "; ".join(error_messages)
        elif errors_dict:
            error_msg = "; ".join([f"{k}: {v}" for k, v in errors_dict.items()])
        else:
            error_msg = response.text
    except json.JSONDecodeError:
        error_msg = response.text

    # Map common status codes to helpful messages
    if status_code == 401:
        friendly_msg = "Authentication failed - check email and API token"
    elif status_code == 403:
        friendly_msg = "Permission denied - check user permissions for this operation"
    elif status_code == 404:
        friendly_msg = "Resource not found - check project key, issue key, or field names"
    elif status_code == 429:
        # Try to get retry-after from headers
        retry_after = response.headers.get("Retry-After", "unknown")
        friendly_msg = f"Rate limit exceeded - retry after {retry_after} seconds"
        logger.warning(f"Jira rate limit hit during {operation}")
        return {
            "success": False,
            "status_code": status_code,
            "error": friendly_msg,
            "rate_limit_retry_after": retry_after,
            "details": error_msg,
        }
    elif status_code >= 500:
        friendly_msg = "Jira server error - try again later"
    else:
        friendly_msg = f"HTTP {status_code} error"

    logger.error(f"Jira API error during {operation}: {status_code} - {error_msg}")
    return {
        "success": False,
        "status_code": status_code,
        "error": friendly_msg,
        "details": error_msg,
    }


# Export tools for registration
TOOLS: List[StructuredTool] = [
    StructuredTool.from_function(
        func=jira_create_issue,
        name="jira_create_issue",
        description="Create a new issue in a Jira project with summary, description, priority, labels, and assignee",
    ),
    StructuredTool.from_function(
        func=jira_list_issues,
        name="jira_list_issues",
        description="List issues in a Jira project using JQL search. Supports custom queries and result limits.",
    ),
    StructuredTool.from_function(
        func=jira_get_issue,
        name="jira_get_issue",
        description="Get detailed information about a specific Jira issue by its key",
    ),
    StructuredTool.from_function(
        func=jira_add_comment,
        name="jira_add_comment",
        description="Add a comment to an existing Jira issue",
    ),
    StructuredTool.from_function(
        func=jira_transition_issue,
        name="jira_transition_issue",
        description="Transition a Jira issue to a new status using a transition ID",
    ),
    StructuredTool.from_function(
        func=jira_update_issue,
        name="jira_update_issue",
        description="Update fields on an existing Jira issue (summary, priority, labels, etc.)",
    ),
]
