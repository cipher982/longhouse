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
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

import httpx
from langchain_core.tools import StructuredTool

from zerg.connectors.context import get_credential_resolver
from zerg.connectors.registry import ConnectorType
from zerg.tools.error_envelope import ErrorType
from zerg.tools.error_envelope import connector_not_configured_error
from zerg.tools.error_envelope import invalid_credentials_error
from zerg.tools.error_envelope import tool_error
from zerg.tools.error_envelope import tool_success

logger = logging.getLogger(__name__)


def _resolve_jira_credentials(
    domain: Optional[str] = None, email: Optional[str] = None, api_token: Optional[str] = None
) -> tuple[Optional[str], Optional[str], Optional[str], Optional[dict]]:
    """Resolve Jira credentials from parameters or context.

    Returns: (domain, email, api_token, error_response) - if error_response is not None, return it.
    """
    resolved_domain = domain
    resolved_email = email
    resolved_api_token = api_token

    if not all([resolved_domain, resolved_email, resolved_api_token]):
        resolver = get_credential_resolver()
        if resolver:
            creds = resolver.get(ConnectorType.JIRA)
            if creds:
                resolved_domain = resolved_domain or creds.get("domain")
                resolved_email = resolved_email or creds.get("email")
                resolved_api_token = resolved_api_token or creds.get("api_token")

    if not resolved_domain:
        return None, None, None, connector_not_configured_error("jira", "Jira")
    if not resolved_email:
        return (
            None,
            None,
            None,
            tool_error(
                error_type=ErrorType.CONNECTOR_NOT_CONFIGURED,
                user_message="Jira email not configured. Set it up in Settings → Integrations → Jira.",
                connector="jira",
                setup_url="/settings/integrations",
            ),
        )
    if not resolved_api_token:
        return (
            None,
            None,
            None,
            tool_error(
                error_type=ErrorType.CONNECTOR_NOT_CONFIGURED,
                user_message="Jira API token not configured. Set it up in Settings → Integrations → Jira.",
                connector="jira",
                setup_url="/settings/integrations",
            ),
        )

    return resolved_domain, resolved_email, resolved_api_token, None


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
    project_key: str,
    issue_type: str,
    summary: str,
    description: Optional[str] = None,
    priority: Optional[str] = None,
    labels: Optional[List[str]] = None,
    assignee: Optional[str] = None,
    domain: Optional[str] = None,
    email: Optional[str] = None,
    api_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new issue in a Jira project.

    Credentials are automatically resolved from agent configuration if not provided.

    Args:
        project_key: Project key (e.g., 'PROJ', 'TEAM')
        issue_type: Issue type name (e.g., 'Task', 'Bug', 'Story')
        summary: Issue summary/title (required)
        description: Issue description in plain text (optional)
        priority: Priority name (e.g., 'High', 'Medium', 'Low') (optional)
        labels: List of label strings (optional)
        assignee: Assignee account ID - NOT email or name (optional)
        domain: Jira Cloud domain (optional, resolved from context if not provided)
        email: User email for authentication (optional, resolved from context if not provided)
        api_token: API token from Atlassian account settings (optional, resolved from context if not provided)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if issue was created
        - issue_key: Created issue key (e.g., 'PROJ-123')
        - issue_id: Created issue ID
        - url: Direct URL to the created issue
        - error: Error message if request failed

    Example:
        >>> jira_create_issue(
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
        # Resolve credentials
        domain, email, api_token, error = _resolve_jira_credentials(domain, email, api_token)
        if error:
            return error

        # Validate required fields
        if not project_key:
            return tool_error(ErrorType.VALIDATION_ERROR, "Project key is required to create a Jira issue.", connector="jira")
        if not issue_type:
            return tool_error(ErrorType.VALIDATION_ERROR, "Issue type is required to create a Jira issue.", connector="jira")
        if not summary:
            return tool_error(ErrorType.VALIDATION_ERROR, "Summary is required to create a Jira issue.", connector="jira")

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
                return tool_error(
                    error_type=ErrorType.VALIDATION_ERROR,
                    user_message="Labels must be a list of strings",
                    connector="jira",
                )
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
            return tool_success(
                {
                    "issue_key": issue_key,
                    "issue_id": issue_id,
                    "url": issue_url,
                }
            )

        # Handle errors
        return _handle_jira_error(response, "create issue")

    except httpx.TimeoutException:
        logger.error(f"Jira API timeout for domain: {domain}")
        return tool_error(error_type=ErrorType.EXECUTION_ERROR, user_message="Request timed out after 30 seconds", connector="jira")
    except httpx.RequestError as e:
        logger.error(f"Jira API request error: {e}")
        return tool_error(error_type=ErrorType.EXECUTION_ERROR, user_message=f"Request failed: {str(e)}", connector="jira")
    except Exception as e:
        logger.exception("Unexpected error in jira_create_issue")
        return tool_error(error_type=ErrorType.EXECUTION_ERROR, user_message=f"Unexpected error: {str(e)}", connector="jira")


def jira_list_issues(
    project_key: str,
    jql: Optional[str] = None,
    max_results: int = 50,
    domain: Optional[str] = None,
    email: Optional[str] = None,
    api_token: Optional[str] = None,
) -> Dict[str, Any]:
    """List issues in a Jira project using JQL search.

    Credentials are automatically resolved from agent configuration if not provided.

    Args:
        project_key: Project key to search within
        jql: Custom JQL query (optional, defaults to all project issues)
        max_results: Maximum number of results to return (default: 50, max: 100)
        domain: Jira Cloud domain (optional, resolved from context if not provided)
        email: User email for authentication (optional, resolved from context if not provided)
        api_token: API token from Atlassian account settings (optional, resolved from context if not provided)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if search succeeded
        - total: Total number of matching issues
        - issues: List of issue objects with key, summary, status, etc.
        - error: Error message if request failed

    Example:
        >>> jira_list_issues(
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
        # Resolve credentials
        domain, email, api_token, error = _resolve_jira_credentials(domain, email, api_token)
        if error:
            return error

        # Validate required fields
        if not project_key:
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message="Missing required field: project_key",
                connector="jira",
            )

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
            return tool_success(
                {
                    "total": total,
                    "returned": len(issues),
                    "issues": issues,
                }
            )

        # Handle errors
        return _handle_jira_error(response, "list issues")

    except httpx.TimeoutException:
        logger.error(f"Jira API timeout for domain: {domain}")
        return tool_error(error_type=ErrorType.EXECUTION_ERROR, user_message="Request timed out after 30 seconds", connector="jira")
    except httpx.RequestError as e:
        logger.error(f"Jira API request error: {e}")
        return tool_error(error_type=ErrorType.EXECUTION_ERROR, user_message=f"Request failed: {str(e)}", connector="jira")
    except Exception as e:
        logger.exception("Unexpected error in jira_list_issues")
        return tool_error(error_type=ErrorType.EXECUTION_ERROR, user_message=f"Unexpected error: {str(e)}", connector="jira")


def jira_get_issue(
    issue_key: str,
    domain: Optional[str] = None,
    email: Optional[str] = None,
    api_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Get detailed information about a specific Jira issue.

    Credentials are automatically resolved from agent configuration if not provided.

    Args:
        issue_key: Issue key (e.g., 'PROJ-123')
        domain: Jira Cloud domain (optional, resolved from context if not provided)
        email: User email for authentication (optional, resolved from context if not provided)
        api_token: API token from Atlassian account settings (optional, resolved from context if not provided)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if request succeeded
        - issue: Detailed issue object with all fields
        - error: Error message if request failed

    Example:
        >>> jira_get_issue(issue_key="PROJ-123")
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
        # Resolve credentials
        domain, email, api_token, error = _resolve_jira_credentials(domain, email, api_token)
        if error:
            return error

        # Validate required fields
        if not issue_key:
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message="Missing required field: issue_key",
                connector="jira",
            )

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
            return tool_success({"issue": issue})

        # Handle errors
        return _handle_jira_error(response, "get issue")

    except httpx.TimeoutException:
        logger.error(f"Jira API timeout for domain: {domain}")
        return tool_error(error_type=ErrorType.EXECUTION_ERROR, user_message="Request timed out after 30 seconds", connector="jira")
    except httpx.RequestError as e:
        logger.error(f"Jira API request error: {e}")
        return tool_error(error_type=ErrorType.EXECUTION_ERROR, user_message=f"Request failed: {str(e)}", connector="jira")
    except Exception as e:
        logger.exception("Unexpected error in jira_get_issue")
        return tool_error(error_type=ErrorType.EXECUTION_ERROR, user_message=f"Unexpected error: {str(e)}", connector="jira")


def jira_add_comment(
    issue_key: str,
    body: str,
    domain: Optional[str] = None,
    email: Optional[str] = None,
    api_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Add a comment to a Jira issue.

    Credentials are automatically resolved from agent configuration if not provided.

    Args:
        issue_key: Issue key (e.g., 'PROJ-123')
        body: Comment text in plain text
        domain: Jira Cloud domain (optional, resolved from context if not provided)
        email: User email for authentication (optional, resolved from context if not provided)
        api_token: API token from Atlassian account settings (optional, resolved from context if not provided)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if comment was added
        - comment_id: ID of the created comment
        - error: Error message if request failed

    Example:
        >>> jira_add_comment(
        ...     issue_key="PROJ-123",
        ...     body="Agent completed the task successfully"
        ... )
        {"success": True, "comment_id": "10050"}
    """
    try:
        # Resolve credentials
        domain, email, api_token, error = _resolve_jira_credentials(domain, email, api_token)
        if error:
            return error

        # Validate required fields
        if not all([issue_key, body]):
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message="Missing required fields: issue_key, body",
                connector="jira",
            )

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
            return tool_success({"comment_id": comment_id})

        # Handle errors
        return _handle_jira_error(response, "add comment")

    except httpx.TimeoutException:
        logger.error(f"Jira API timeout for domain: {domain}")
        return tool_error(error_type=ErrorType.EXECUTION_ERROR, user_message="Request timed out after 30 seconds", connector="jira")
    except httpx.RequestError as e:
        logger.error(f"Jira API request error: {e}")
        return tool_error(error_type=ErrorType.EXECUTION_ERROR, user_message=f"Request failed: {str(e)}", connector="jira")
    except Exception as e:
        logger.exception("Unexpected error in jira_add_comment")
        return tool_error(error_type=ErrorType.EXECUTION_ERROR, user_message=f"Unexpected error: {str(e)}", connector="jira")


def jira_transition_issue(
    issue_key: str,
    transition_id: str,
    domain: Optional[str] = None,
    email: Optional[str] = None,
    api_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Transition a Jira issue to a new status.

    Credentials are automatically resolved from agent configuration if not provided.

    Note: To find available transitions for an issue, use jira_get_issue and check
    the available transitions, or query /rest/api/3/issue/{issueKey}/transitions

    Args:
        issue_key: Issue key (e.g., 'PROJ-123')
        transition_id: Transition ID as string (e.g., '21' for 'In Progress -> Done')
        domain: Jira Cloud domain (optional, resolved from context if not provided)
        email: User email for authentication (optional, resolved from context if not provided)
        api_token: API token from Atlassian account settings (optional, resolved from context if not provided)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if transition succeeded
        - error: Error message if request failed

    Example:
        >>> jira_transition_issue(
        ...     issue_key="PROJ-123",
        ...     transition_id="21"
        ... )
        {"success": True}
    """
    try:
        # Resolve credentials
        domain, email, api_token, error = _resolve_jira_credentials(domain, email, api_token)
        if error:
            return error

        # Validate required fields
        if not issue_key:
            return tool_error(ErrorType.VALIDATION_ERROR, "Issue key is required to transition a Jira issue.", connector="jira")
        if not transition_id:
            return tool_error(ErrorType.VALIDATION_ERROR, "Transition ID is required to transition a Jira issue.", connector="jira")

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
            return tool_success({})

        # Handle errors
        return _handle_jira_error(response, "transition issue")

    except httpx.TimeoutException:
        logger.error(f"Jira API timeout for domain: {domain}")
        return tool_error(error_type=ErrorType.EXECUTION_ERROR, user_message="Request timed out after 30 seconds", connector="jira")
    except httpx.RequestError as e:
        logger.error(f"Jira API request error: {e}")
        return tool_error(error_type=ErrorType.EXECUTION_ERROR, user_message=f"Request failed: {str(e)}", connector="jira")
    except Exception as e:
        logger.exception("Unexpected error in jira_transition_issue")
        return tool_error(error_type=ErrorType.EXECUTION_ERROR, user_message=f"Unexpected error: {str(e)}", connector="jira")


def jira_update_issue(
    issue_key: str,
    fields: Dict[str, Any],
    domain: Optional[str] = None,
    email: Optional[str] = None,
    api_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Update fields on an existing Jira issue.

    Credentials are automatically resolved from agent configuration if not provided.

    Args:
        issue_key: Issue key (e.g., 'PROJ-123')
        fields: Dictionary of field updates (e.g., {"summary": "New title", "priority": {"name": "Low"}})
        domain: Jira Cloud domain (optional, resolved from context if not provided)
        email: User email for authentication (optional, resolved from context if not provided)
        api_token: API token from Atlassian account settings (optional, resolved from context if not provided)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if update succeeded
        - error: Error message if request failed

    Example:
        >>> jira_update_issue(
        ...     issue_key="PROJ-123",
        ...     fields={"summary": "Updated title", "priority": {"name": "Low"}}
        ... )
        {"success": True}
    """
    try:
        # Resolve credentials
        domain, email, api_token, error = _resolve_jira_credentials(domain, email, api_token)
        if error:
            return error

        # Validate required fields
        if not all([issue_key, fields]):
            return tool_error(
                error_type=ErrorType.VALIDATION_ERROR,
                user_message="Missing required fields: issue_key, fields",
                connector="jira",
            )

        if not isinstance(fields, dict):
            return tool_error(error_type=ErrorType.VALIDATION_ERROR, user_message="Fields must be a dictionary", connector="jira")

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
            return tool_success({})

        # Handle errors
        return _handle_jira_error(response, "update issue")

    except httpx.TimeoutException:
        logger.error(f"Jira API timeout for domain: {domain}")
        return tool_error(error_type=ErrorType.EXECUTION_ERROR, user_message="Request timed out after 30 seconds", connector="jira")
    except httpx.RequestError as e:
        logger.error(f"Jira API request error: {e}")
        return tool_error(error_type=ErrorType.EXECUTION_ERROR, user_message=f"Request failed: {str(e)}", connector="jira")
    except Exception as e:
        logger.exception("Unexpected error in jira_update_issue")
        return tool_error(error_type=ErrorType.EXECUTION_ERROR, user_message=f"Unexpected error: {str(e)}", connector="jira")


def _handle_jira_error(response: httpx.Response, operation: str) -> Dict[str, Any]:
    """Handle Jira API error responses consistently.

    Args:
        response: httpx Response object
        operation: Description of the operation that failed

    Returns:
        Dictionary with error envelope format
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

    # Map common status codes to error types
    if status_code == 401:
        return invalid_credentials_error("jira", "Jira")
    elif status_code == 403:
        return tool_error(
            error_type=ErrorType.PERMISSION_DENIED,
            user_message=f"Permission denied: {error_msg}",
            connector="jira",
        )
    elif status_code == 404:
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message=f"Resource not found: {error_msg}",
            connector="jira",
        )
    elif status_code == 429:
        retry_after = response.headers.get("Retry-After", "unknown")
        logger.warning(f"Jira rate limit hit during {operation}")
        return tool_error(
            error_type=ErrorType.RATE_LIMITED,
            user_message=f"Rate limit exceeded. Retry after {retry_after} seconds",
            connector="jira",
        )
    elif status_code >= 500:
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message=f"Jira server error: {error_msg}",
            connector="jira",
        )
    elif status_code == 400:
        return tool_error(
            error_type=ErrorType.VALIDATION_ERROR,
            user_message=error_msg,
            connector="jira",
        )
    else:
        logger.error(f"Jira API error during {operation}: {status_code} - {error_msg}")
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message=error_msg,
            connector="jira",
        )


# Export tools for registration
TOOLS: List[StructuredTool] = [
    StructuredTool.from_function(
        func=jira_create_issue,
        name="jira_create_issue",
        description="Create a new issue in a Jira project with summary, description, priority, labels, and assignee. Credentials are automatically resolved from agent configuration.",
    ),
    StructuredTool.from_function(
        func=jira_list_issues,
        name="jira_list_issues",
        description="List issues in a Jira project using JQL search. Supports custom queries and result limits. Credentials are automatically resolved from agent configuration.",
    ),
    StructuredTool.from_function(
        func=jira_get_issue,
        name="jira_get_issue",
        description="Get detailed information about a specific Jira issue by its key. Credentials are automatically resolved from agent configuration.",
    ),
    StructuredTool.from_function(
        func=jira_add_comment,
        name="jira_add_comment",
        description="Add a comment to an existing Jira issue. Credentials are automatically resolved from agent configuration.",
    ),
    StructuredTool.from_function(
        func=jira_transition_issue,
        name="jira_transition_issue",
        description="Transition a Jira issue to a new status using a transition ID. Credentials are automatically resolved from agent configuration.",
    ),
    StructuredTool.from_function(
        func=jira_update_issue,
        name="jira_update_issue",
        description="Update fields on an existing Jira issue (summary, priority, labels, etc.). Credentials are automatically resolved from agent configuration.",
    ),
]
