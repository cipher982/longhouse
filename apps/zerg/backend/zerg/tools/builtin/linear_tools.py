"""Linear-related tools for interacting with Linear issue tracking.

This module provides tools for managing Linear issues, teams, and comments
via the Linear GraphQL API. All tools require a Linear API key for authentication.

Configuration:
    Agents should be configured with a Linear connector that provides:
    - api_key: Linear Personal API Key

Rate Limits:
    - API Key Authentication: 1,500 requests/hour per user
    - Complexity: 250,000 complexity points/hour per user
    - Headers: x-ratelimit-requests-remaining, x-complexity-remaining

How to get API Key:
    Generate at: https://linear.app/settings/api
"""

import json
import logging
from typing import Any, Dict, List, Optional

import httpx
from langchain_core.tools import StructuredTool

from zerg.connectors.context import get_credential_resolver
from zerg.connectors.registry import ConnectorType

logger = logging.getLogger(__name__)

# Linear GraphQL API endpoint
LINEAR_GRAPHQL_ENDPOINT = "https://api.linear.app/graphql"


def _resolve_linear_api_key(api_key: Optional[str] = None) -> tuple[Optional[str], Optional[dict]]:
    """Resolve Linear API key from parameter or context.

    Returns: (api_key, error_response) - if error_response is not None, return it.
    """
    resolved_api_key = api_key
    if not resolved_api_key:
        resolver = get_credential_resolver()
        if resolver:
            creds = resolver.get(ConnectorType.LINEAR)
            if creds:
                resolved_api_key = creds.get("api_key")

    if not resolved_api_key:
        return None, {
            "success": False,
            "error": "Linear API key not configured. Either provide api_key parameter or configure Linear in Agent Settings -> Connectors."
        }
    return resolved_api_key, None


def _make_linear_request(
    api_key: str,
    query: str,
    variables: Optional[Dict[str, Any]] = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """Internal helper to make authenticated Linear GraphQL API requests.

    Args:
        api_key: Linear Personal API Key
        query: GraphQL query or mutation string
        variables: Optional GraphQL variables dictionary
        timeout: Request timeout in seconds

    Returns:
        Dictionary with success status, data, and any error messages
    """
    try:
        if not api_key or not isinstance(api_key, str):
            return {
                "success": False,
                "error": "Invalid or missing Linear API key"
            }

        headers = {
            "Authorization": api_key,  # Personal API keys don't use Bearer prefix
            "Content-Type": "application/json",
            "User-Agent": "Zerg-Agent/1.0"
        }

        payload = {
            "query": query
        }
        if variables:
            payload["variables"] = variables

        with httpx.Client() as client:
            response = client.post(
                url=LINEAR_GRAPHQL_ENDPOINT,
                headers=headers,
                json=payload,
                timeout=timeout,
                follow_redirects=True
            )

        # Check for authentication errors
        if response.status_code == 401:
            return {
                "success": False,
                "error": "Linear authentication failed. Check your API key.",
                "status_code": 401
            }

        # Check for rate limit
        if response.status_code == 403:
            rate_limit_remaining = response.headers.get("x-ratelimit-requests-remaining", "unknown")
            if rate_limit_remaining == "0":
                reset_time = response.headers.get("x-ratelimit-requests-reset", "unknown")
                return {
                    "success": False,
                    "error": f"Linear API rate limit exceeded. Resets at: {reset_time}",
                    "status_code": 403
                }

        # Parse GraphQL response
        try:
            response_data = response.json()
        except json.JSONDecodeError:
            return {
                "success": False,
                "error": "Failed to parse Linear API response",
                "status_code": response.status_code
            }

        # Check for GraphQL errors
        if "errors" in response_data and response_data["errors"]:
            errors = response_data["errors"]
            error_messages = [err.get("message", str(err)) for err in errors]
            return {
                "success": False,
                "error": "; ".join(error_messages),
                "status_code": response.status_code,
                "graphql_errors": errors
            }

        # Check for success
        if 200 <= response.status_code < 300:
            return {
                "success": True,
                "data": response_data.get("data", {}),
                "status_code": response.status_code
            }
        else:
            return {
                "success": False,
                "error": f"Request failed with status {response.status_code}",
                "status_code": response.status_code
            }

    except httpx.TimeoutException:
        logger.error(f"Linear API timeout")
        return {
            "success": False,
            "error": f"Request timed out after {timeout} seconds"
        }
    except httpx.RequestError as e:
        logger.error(f"Linear API request error: {e}")
        return {
            "success": False,
            "error": f"Request failed: {str(e)}"
        }
    except Exception as e:
        logger.exception(f"Unexpected error in Linear API request")
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }


def linear_create_issue(
    team_id: str,
    title: str,
    description: Optional[str] = None,
    priority: Optional[int] = None,
    state_id: Optional[str] = None,
    assignee_id: Optional[str] = None,
    label_ids: Optional[List[str]] = None,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new issue in Linear.

    Args:
        team_id: ID of the team to create the issue in (required)
        title: Issue title (required)
        description: Issue description/body (optional)
        priority: Priority level: 0=None, 1=Urgent, 2=High, 3=Medium, 4=Low (optional)
        state_id: ID of the workflow state (optional)
        assignee_id: ID of the user to assign to (optional)
        label_ids: List of label IDs to apply (optional)
        api_key: Linear Personal API Key (optional, uses agent context if not provided)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if the operation succeeded
        - data: Issue details including id, identifier, title, url (if successful)
        - error: Error message (if failed)

    Example:
        >>> linear_create_issue(
        ...     team_id="abc123",
        ...     title="Bug: Login not working",
        ...     description="Users cannot log in with valid credentials",
        ...     priority=2
        ... )
        {'success': True, 'data': {'id': '...', 'identifier': 'ENG-42', 'url': '...'}}
    """
    # Resolve API key from parameter or context
    resolved_api_key, error = _resolve_linear_api_key(api_key)
    if error:
        return error

    if not title or not title.strip():
        return {
            "success": False,
            "error": "Issue title is required and cannot be empty"
        }

    if not team_id or not team_id.strip():
        return {
            "success": False,
            "error": "Team ID is required and cannot be empty"
        }

    if priority is not None and (not isinstance(priority, int) or priority < 0 or priority > 4):
        return {
            "success": False,
            "error": "Priority must be an integer between 0 (None) and 4 (Low)"
        }

    mutation = """
    mutation IssueCreate($input: IssueCreateInput!) {
      issueCreate(input: $input) {
        success
        issue {
          id
          identifier
          title
          description
          priority
          state {
            id
            name
          }
          assignee {
            id
            name
          }
          team {
            id
            key
          }
          url
          createdAt
        }
      }
    }
    """

    input_data = {
        "teamId": team_id.strip(),
        "title": title.strip()
    }

    if description:
        input_data["description"] = description
    if priority is not None:
        input_data["priority"] = priority
    if state_id:
        input_data["stateId"] = state_id
    if assignee_id:
        input_data["assigneeId"] = assignee_id
    if label_ids:
        input_data["labelIds"] = label_ids

    variables = {"input": input_data}
    result = _make_linear_request(resolved_api_key, mutation, variables)

    # Simplify response for agent consumption
    if result.get("success") and "data" in result:
        issue_create = result["data"].get("issueCreate", {})
        if issue_create.get("success"):
            issue_data = issue_create.get("issue", {})
            result["data"] = {
                "id": issue_data.get("id"),
                "identifier": issue_data.get("identifier"),
                "title": issue_data.get("title"),
                "url": issue_data.get("url"),
                "priority": issue_data.get("priority"),
                "state": issue_data.get("state", {}).get("name"),
                "team": issue_data.get("team", {}).get("key"),
                "created_at": issue_data.get("createdAt")
            }
        else:
            return {
                "success": False,
                "error": "Issue creation failed"
            }

    return result


def linear_list_issues(
    team_id: Optional[str] = None,
    state: Optional[str] = None,
    first: int = 50,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """List issues in Linear with optional filtering.

    Args:
        team_id: Filter by team ID (optional)
        state: Filter by workflow state name (e.g., "In Progress", "Done") (optional)
        first: Number of results to return, max 250 (default: 50)
        api_key: Linear Personal API Key (optional, uses agent context if not provided)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if the operation succeeded
        - data: List of issues with id, identifier, title, state, url
        - count: Number of issues returned
        - error: Error message (if failed)

    Example:
        >>> linear_list_issues(
        ...     team_id="abc123",
        ...     state="In Progress",
        ...     first=25
        ... )
        {'success': True, 'data': [...], 'count': 5}
    """
    # Resolve API key from parameter or context
    resolved_api_key, error = _resolve_linear_api_key(api_key)
    if error:
        return error

    if first < 1 or first > 250:
        return {
            "success": False,
            "error": "first must be between 1 and 250"
        }

    query = """
    query Issues($filter: IssueFilter, $first: Int) {
      issues(filter: $filter, first: $first) {
        nodes {
          id
          identifier
          title
          description
          priority
          state {
            id
            name
          }
          assignee {
            id
            name
          }
          team {
            id
            key
          }
          url
          createdAt
          updatedAt
        }
        pageInfo {
          hasNextPage
          endCursor
        }
      }
    }
    """

    # Build filter
    filter_obj = {}
    if team_id:
        filter_obj["team"] = {"id": {"eq": team_id}}
    if state:
        filter_obj["state"] = {"name": {"eq": state}}

    variables = {
        "first": first
    }
    if filter_obj:
        variables["filter"] = filter_obj

    result = _make_linear_request(resolved_api_key, query, variables)

    # Simplify response for agent consumption
    if result.get("success") and "data" in result:
        raw_data = result["data"]
        issues_data = raw_data.get("issues", {}).get("nodes", [])
        page_info = raw_data.get("issues", {}).get("pageInfo", {})

        result["data"] = [
            {
                "id": issue.get("id"),
                "identifier": issue.get("identifier"),
                "title": issue.get("title"),
                "priority": issue.get("priority"),
                "state": issue.get("state", {}).get("name"),
                "assignee": issue.get("assignee", {}).get("name") if issue.get("assignee") else None,
                "team": issue.get("team", {}).get("key"),
                "url": issue.get("url"),
                "created_at": issue.get("createdAt"),
                "updated_at": issue.get("updatedAt")
            }
            for issue in issues_data
        ]
        result["count"] = len(result["data"])

        # Include pagination info
        if page_info.get("hasNextPage"):
            result["has_next_page"] = True
            result["end_cursor"] = page_info.get("endCursor")

    return result


def linear_get_issue(
    issue_id: str,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Get details of a specific Linear issue.

    Args:
        issue_id: Issue ID (not identifier - use the UUID, not "ENG-42")
        api_key: Linear Personal API Key (optional, uses agent context if not provided)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if the operation succeeded
        - data: Issue details including id, identifier, title, description, state, comments
        - error: Error message (if failed)

    Example:
        >>> linear_get_issue(
        ...     issue_id="abc-123-def-456"
        ... )
        {'success': True, 'data': {'identifier': 'ENG-42', 'title': '...', 'comments': [...]}}
    """
    # Resolve API key from parameter or context
    resolved_api_key, error = _resolve_linear_api_key(api_key)
    if error:
        return error

    if not issue_id or not issue_id.strip():
        return {
            "success": False,
            "error": "Issue ID is required and cannot be empty"
        }

    query = """
    query Issue($id: String!) {
      issue(id: $id) {
        id
        identifier
        title
        description
        priority
        state {
          id
          name
        }
        assignee {
          id
          name
        }
        team {
          id
          key
        }
        labels {
          nodes {
            id
            name
          }
        }
        comments {
          nodes {
            id
            body
            user {
              id
              name
            }
            createdAt
          }
        }
        url
        createdAt
        updatedAt
      }
    }
    """

    variables = {"id": issue_id.strip()}
    result = _make_linear_request(resolved_api_key, query, variables)

    # Simplify response for agent consumption
    if result.get("success") and "data" in result:
        issue_data = result["data"].get("issue", {})
        if issue_data:
            result["data"] = {
                "id": issue_data.get("id"),
                "identifier": issue_data.get("identifier"),
                "title": issue_data.get("title"),
                "description": issue_data.get("description"),
                "priority": issue_data.get("priority"),
                "state": issue_data.get("state", {}).get("name"),
                "assignee": issue_data.get("assignee", {}).get("name") if issue_data.get("assignee") else None,
                "team": issue_data.get("team", {}).get("key"),
                "labels": [label.get("name") for label in issue_data.get("labels", {}).get("nodes", [])],
                "comments": [
                    {
                        "id": comment.get("id"),
                        "body": comment.get("body"),
                        "author": comment.get("user", {}).get("name"),
                        "created_at": comment.get("createdAt")
                    }
                    for comment in issue_data.get("comments", {}).get("nodes", [])
                ],
                "url": issue_data.get("url"),
                "created_at": issue_data.get("createdAt"),
                "updated_at": issue_data.get("updatedAt")
            }
        else:
            return {
                "success": False,
                "error": "Issue not found"
            }

    return result


def linear_update_issue(
    issue_id: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    state_id: Optional[str] = None,
    priority: Optional[int] = None,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Update an existing Linear issue.

    Args:
        issue_id: Issue ID to update (UUID, not identifier)
        title: New issue title (optional)
        description: New issue description (optional)
        state_id: New workflow state ID (optional)
        priority: New priority level: 0=None, 1=Urgent, 2=High, 3=Medium, 4=Low (optional)
        api_key: Linear Personal API Key (optional, uses agent context if not provided)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if the operation succeeded
        - data: Updated issue details (if successful)
        - error: Error message (if failed)

    Example:
        >>> linear_update_issue(
        ...     issue_id="abc-123-def-456",
        ...     title="Bug: Login fixed",
        ...     priority=4
        ... )
        {'success': True, 'data': {'identifier': 'ENG-42', 'title': 'Bug: Login fixed'}}
    """
    # Resolve API key from parameter or context
    resolved_api_key, error = _resolve_linear_api_key(api_key)
    if error:
        return error

    if not issue_id or not issue_id.strip():
        return {
            "success": False,
            "error": "Issue ID is required and cannot be empty"
        }

    if priority is not None and (not isinstance(priority, int) or priority < 0 or priority > 4):
        return {
            "success": False,
            "error": "Priority must be an integer between 0 (None) and 4 (Low)"
        }

    # At least one field must be provided
    if not any([title, description, state_id, priority is not None]):
        return {
            "success": False,
            "error": "At least one field to update must be provided (title, description, state_id, or priority)"
        }

    mutation = """
    mutation IssueUpdate($id: String!, $input: IssueUpdateInput!) {
      issueUpdate(id: $id, input: $input) {
        success
        issue {
          id
          identifier
          title
          description
          priority
          state {
            id
            name
          }
          updatedAt
        }
      }
    }
    """

    input_data = {}
    if title:
        input_data["title"] = title.strip()
    if description:
        input_data["description"] = description
    if state_id:
        input_data["stateId"] = state_id
    if priority is not None:
        input_data["priority"] = priority

    variables = {
        "id": issue_id.strip(),
        "input": input_data
    }

    result = _make_linear_request(resolved_api_key, mutation, variables)

    # Simplify response for agent consumption
    if result.get("success") and "data" in result:
        issue_update = result["data"].get("issueUpdate", {})
        if issue_update.get("success"):
            issue_data = issue_update.get("issue", {})
            result["data"] = {
                "id": issue_data.get("id"),
                "identifier": issue_data.get("identifier"),
                "title": issue_data.get("title"),
                "description": issue_data.get("description"),
                "priority": issue_data.get("priority"),
                "state": issue_data.get("state", {}).get("name"),
                "updated_at": issue_data.get("updatedAt")
            }
        else:
            return {
                "success": False,
                "error": "Issue update failed"
            }

    return result


def linear_add_comment(
    issue_id: str,
    body: str,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Add a comment to a Linear issue.

    Args:
        issue_id: Issue ID to comment on (UUID, not identifier)
        body: Comment text (required)
        api_key: Linear Personal API Key (optional, uses agent context if not provided)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if the operation succeeded
        - data: Comment details including id and url (if successful)
        - error: Error message (if failed)

    Example:
        >>> linear_add_comment(
        ...     issue_id="abc-123-def-456",
        ...     body="Thanks for reporting this! We'll look into it."
        ... )
        {'success': True, 'data': {'id': '...', 'body': '...', 'url': '...'}}
    """
    # Resolve API key from parameter or context
    resolved_api_key, error = _resolve_linear_api_key(api_key)
    if error:
        return error

    if not body or not body.strip():
        return {
            "success": False,
            "error": "Comment body is required and cannot be empty"
        }

    if not issue_id or not issue_id.strip():
        return {
            "success": False,
            "error": "Issue ID is required and cannot be empty"
        }

    mutation = """
    mutation CommentCreate($input: CommentCreateInput!) {
      commentCreate(input: $input) {
        success
        comment {
          id
          body
          user {
            id
            name
          }
          issue {
            id
            identifier
          }
          createdAt
          url
        }
      }
    }
    """

    variables = {
        "input": {
            "issueId": issue_id.strip(),
            "body": body.strip()
        }
    }

    result = _make_linear_request(resolved_api_key, mutation, variables)

    # Simplify response for agent consumption
    if result.get("success") and "data" in result:
        comment_create = result["data"].get("commentCreate", {})
        if comment_create.get("success"):
            comment_data = comment_create.get("comment", {})
            result["data"] = {
                "id": comment_data.get("id"),
                "body": comment_data.get("body"),
                "author": comment_data.get("user", {}).get("name"),
                "issue_identifier": comment_data.get("issue", {}).get("identifier"),
                "url": comment_data.get("url"),
                "created_at": comment_data.get("createdAt")
            }
        else:
            return {
                "success": False,
                "error": "Comment creation failed"
            }

    return result


def linear_list_teams(
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """List all teams accessible to the API key.

    Args:
        api_key: Linear Personal API Key (optional, uses agent context if not provided)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if the operation succeeded
        - data: List of teams with id, name, key, description
        - count: Number of teams returned
        - error: Error message (if failed)

    Example:
        >>> linear_list_teams()
        {'success': True, 'data': [{'id': '...', 'name': 'Engineering', 'key': 'ENG'}], 'count': 3}
    """
    # Resolve API key from parameter or context
    resolved_api_key, error = _resolve_linear_api_key(api_key)
    if error:
        return error

    query = """
    query Teams {
      teams {
        nodes {
          id
          name
          key
          description
        }
      }
    }
    """

    result = _make_linear_request(resolved_api_key, query)

    # Simplify response for agent consumption
    if result.get("success") and "data" in result:
        teams_data = result["data"].get("teams", {}).get("nodes", [])
        result["data"] = [
            {
                "id": team.get("id"),
                "name": team.get("name"),
                "key": team.get("key"),
                "description": team.get("description")
            }
            for team in teams_data
        ]
        result["count"] = len(result["data"])

    return result


# Register tools with LangChain
TOOLS: List[StructuredTool] = [
    StructuredTool.from_function(
        func=linear_create_issue,
        name="linear_create_issue",
        description="Create a new issue in Linear. Returns the issue identifier and URL. Requires team_id. API key can be provided or uses agent's configured Linear connector.",
    ),
    StructuredTool.from_function(
        func=linear_list_issues,
        name="linear_list_issues",
        description="List issues in Linear with optional filtering by team and state. API key can be provided or uses agent's configured Linear connector.",
    ),
    StructuredTool.from_function(
        func=linear_get_issue,
        name="linear_get_issue",
        description="Get detailed information about a specific Linear issue by ID including comments. API key can be provided or uses agent's configured Linear connector.",
    ),
    StructuredTool.from_function(
        func=linear_update_issue,
        name="linear_update_issue",
        description="Update an existing Linear issue's title, description, state, or priority. API key can be provided or uses agent's configured Linear connector.",
    ),
    StructuredTool.from_function(
        func=linear_add_comment,
        name="linear_add_comment",
        description="Add a comment to an existing Linear issue. API key can be provided or uses agent's configured Linear connector.",
    ),
    StructuredTool.from_function(
        func=linear_list_teams,
        name="linear_list_teams",
        description="List all teams accessible to the API key. Use this to find team IDs for creating issues. API key can be provided or uses agent's configured Linear connector.",
    ),
]
