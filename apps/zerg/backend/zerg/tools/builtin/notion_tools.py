"""Notion API tools for interacting with Notion workspaces.

These tools allow agents to create pages, query databases, append blocks, and search
within a Notion workspace. Authentication is handled via Notion Integration Tokens.

Configuration:
- Configure Notion credentials in Agent Settings -> Connectors, OR
- Pass api_key parameter directly to each tool function
- Create integration at notion.so/my-integrations
- The integration must be shared with target pages/databases

API Documentation: https://developers.notion.com/reference/intro
"""

import json
import logging
from typing import Any, Dict, List, Optional

import httpx
from langchain_core.tools import StructuredTool

from zerg.connectors.context import get_credential_resolver
from zerg.connectors.registry import ConnectorType

logger = logging.getLogger(__name__)

# Notion API Configuration
NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"  # Latest version as of Nov 2025


def _resolve_notion_api_key(api_key: Optional[str] = None) -> tuple[Optional[str], Optional[dict]]:
    """Resolve Notion API key from parameter or context.

    Returns: (api_key, error_response) - if error_response is not None, return it.
    """
    resolved_api_key = api_key
    if not resolved_api_key:
        resolver = get_credential_resolver()
        if resolver:
            creds = resolver.get(ConnectorType.NOTION)
            if creds:
                resolved_api_key = creds.get("api_key")

    if not resolved_api_key:
        return None, {
            "success": False,
            "error": "Notion API key not configured. Either provide api_key parameter or configure Notion in Agent Settings -> Connectors."
        }
    return resolved_api_key, None


def _make_notion_request(
    api_key: str,
    endpoint: str,
    method: str = "GET",
    data: Optional[Dict[str, Any]] = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """Make a request to the Notion API.

    Args:
        api_key: Notion Integration Token
        endpoint: API endpoint path (e.g., '/pages', '/search')
        method: HTTP method (GET, POST, PATCH, DELETE)
        data: Optional request body data
        timeout: Request timeout in seconds

    Returns:
        Dict containing:
        - success: Boolean indicating if request succeeded
        - data: Response data (if successful)
        - error: Error message (if failed)
        - status_code: HTTP status code
    """
    if not api_key:
        return {
            "success": False,
            "error": "API key is required",
            "status_code": 0,
        }

    url = f"{NOTION_API_BASE}{endpoint}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client() as client:
            response = client.request(
                method=method,
                url=url,
                headers=headers,
                json=data,
                timeout=timeout,
            )

        # Handle successful responses
        if response.status_code in [200, 201]:
            return {
                "success": True,
                "data": response.json(),
                "status_code": response.status_code,
            }

        # Handle error responses
        error_data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        error_message = error_data.get("message", response.text)
        error_code = error_data.get("code", "unknown")

        # Provide helpful error messages
        if response.status_code == 401:
            error_message = f"Authentication failed: {error_message}. Check your API key."
        elif response.status_code == 404:
            error_message = f"Resource not found: {error_message}. Verify the page/database ID and that your integration has access."
        elif response.status_code == 429:
            error_message = f"Rate limit exceeded: {error_message}. Notion API limit is ~3 requests/second."
        elif response.status_code == 400:
            error_message = f"Invalid request: {error_message}"

        logger.warning(f"Notion API error {response.status_code} ({error_code}): {error_message}")

        return {
            "success": False,
            "error": error_message,
            "error_code": error_code,
            "status_code": response.status_code,
        }

    except httpx.TimeoutException:
        logger.error(f"Notion API timeout for {endpoint}")
        return {
            "success": False,
            "error": f"Request timed out after {timeout} seconds",
            "status_code": 0,
        }
    except httpx.RequestError as e:
        logger.error(f"Notion API request error for {endpoint}: {e}")
        return {
            "success": False,
            "error": f"Request failed: {str(e)}",
            "status_code": 0,
        }
    except Exception as e:
        logger.exception(f"Unexpected error in Notion API request to {endpoint}")
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}",
            "status_code": 0,
        }


def notion_create_page(
    parent_id: str,
    title: str,
    content_blocks: Optional[List[Dict[str, Any]]] = None,
    properties: Optional[Dict[str, Any]] = None,
    is_database_item: bool = False,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new page in Notion.

    Args:
        parent_id: Parent page ID or database ID where the page will be created
        title: Page title
        content_blocks: Optional list of block objects to add as page content
        properties: Optional properties dict (required if parent is a database)
        is_database_item: If True, parent_id is treated as database_id, otherwise as page_id
        api_key: Optional Notion Integration Token (uses configured credentials if not provided)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if page was created
        - page_id: ID of the created page (if successful)
        - url: URL to the created page (if successful)
        - error: Error message (if failed)

    Example:
        >>> # Create a simple page (using configured credentials)
        >>> result = notion_create_page(
        ...     parent_id="parent-page-uuid",
        ...     title="Meeting Notes",
        ...     is_database_item=False
        ... )

        >>> # Create a database item with properties
        >>> result = notion_create_page(
        ...     parent_id="database-uuid",
        ...     title="New Task",
        ...     properties={"Status": {"select": {"name": "In Progress"}}},
        ...     is_database_item=True
        ... )

        >>> # Create page with content blocks and explicit API key
        >>> blocks = [
        ...     {
        ...         "object": "block",
        ...         "type": "paragraph",
        ...         "paragraph": {
        ...             "rich_text": [{"text": {"content": "Page content here"}}]
        ...         }
        ...     }
        ... ]
        >>> result = notion_create_page(
        ...     parent_id="parent-uuid",
        ...     title="My Page",
        ...     content_blocks=blocks,
        ...     is_database_item=False,
        ...     api_key="secret_abc123"
        ... )
    """
    # Resolve API key
    resolved_api_key, error = _resolve_notion_api_key(api_key)
    if error:
        return error

    # Build parent object
    parent_key = "database_id" if is_database_item else "page_id"
    parent = {parent_key: parent_id}

    # Build properties - different structure for database vs page
    if is_database_item:
        # Database item: use provided properties or default title
        page_properties = properties or {}
        if "Name" not in page_properties and "title" not in page_properties:
            # Add title property if not provided
            page_properties["Name"] = {"title": [{"text": {"content": title}}]}
    else:
        # Regular page: use title property
        page_properties = {"title": {"title": [{"text": {"content": title}}]}}
        # Add any additional properties if provided
        if properties:
            page_properties.update(properties)

    # Build request body
    request_body = {
        "parent": parent,
        "properties": page_properties,
    }

    # Add content blocks if provided
    if content_blocks:
        request_body["children"] = content_blocks

    # Make API request
    result = _make_notion_request(resolved_api_key, "/pages", method="POST", data=request_body)

    if result["success"]:
        page_data = result["data"]
        return {
            "success": True,
            "page_id": page_data["id"],
            "url": page_data.get("url", ""),
            "created_time": page_data.get("created_time", ""),
        }
    else:
        return {
            "success": False,
            "error": result.get("error", "Unknown error"),
            "status_code": result.get("status_code", 0),
        }


def notion_get_page(page_id: str, api_key: Optional[str] = None) -> Dict[str, Any]:
    """Retrieve a page from Notion.

    Args:
        page_id: ID of the page to retrieve
        api_key: Optional Notion Integration Token (uses configured credentials if not provided)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if page was retrieved
        - page: Page object with properties (if successful)
        - error: Error message (if failed)

    Example:
        >>> # Using configured credentials
        >>> result = notion_get_page(page_id="page-uuid")
        >>> if result["success"]:
        ...     print(result["page"]["properties"])

        >>> # Using explicit API key
        >>> result = notion_get_page(page_id="page-uuid", api_key="secret_abc123")
    """
    # Resolve API key
    resolved_api_key, error = _resolve_notion_api_key(api_key)
    if error:
        return error

    result = _make_notion_request(resolved_api_key, f"/pages/{page_id}", method="GET")

    if result["success"]:
        return {
            "success": True,
            "page": result["data"],
        }
    else:
        return {
            "success": False,
            "error": result.get("error", "Unknown error"),
            "status_code": result.get("status_code", 0),
        }


def notion_update_page(
    page_id: str,
    properties: Optional[Dict[str, Any]] = None,
    archived: Optional[bool] = None,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Update a page's properties or archive status.

    Args:
        page_id: ID of the page to update
        properties: Optional properties to update
        archived: Optional boolean to archive/unarchive the page
        api_key: Optional Notion Integration Token (uses configured credentials if not provided)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if page was updated
        - page: Updated page object (if successful)
        - error: Error message (if failed)

    Example:
        >>> # Update properties (using configured credentials)
        >>> result = notion_update_page(
        ...     page_id="page-uuid",
        ...     properties={"Status": {"select": {"name": "Done"}}}
        ... )

        >>> # Archive a page with explicit API key
        >>> result = notion_update_page(
        ...     page_id="page-uuid",
        ...     archived=True,
        ...     api_key="secret_abc123"
        ... )
    """
    # Resolve API key
    resolved_api_key, error = _resolve_notion_api_key(api_key)
    if error:
        return error

    request_body = {}

    if properties is not None:
        request_body["properties"] = properties

    if archived is not None:
        request_body["archived"] = archived

    if not request_body:
        return {
            "success": False,
            "error": "Must provide either properties or archived parameter",
        }

    result = _make_notion_request(resolved_api_key, f"/pages/{page_id}", method="PATCH", data=request_body)

    if result["success"]:
        return {
            "success": True,
            "page": result["data"],
        }
    else:
        return {
            "success": False,
            "error": result.get("error", "Unknown error"),
            "status_code": result.get("status_code", 0),
        }


def notion_search(
    query: str,
    filter_type: Optional[str] = None,
    page_size: int = 10,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Search across pages and databases in the workspace.

    Args:
        query: Search query string
        filter_type: Optional filter - "page" or "database" to limit results
        page_size: Number of results to return (default: 10, max: 100)
        api_key: Optional Notion Integration Token (uses configured credentials if not provided)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if search succeeded
        - results: List of matching pages/databases (if successful)
        - has_more: Boolean indicating if more results exist
        - error: Error message (if failed)

    Example:
        >>> # Search all content (using configured credentials)
        >>> result = notion_search(query="meeting notes")

        >>> # Search only pages with explicit API key
        >>> result = notion_search(
        ...     query="project",
        ...     filter_type="page",
        ...     api_key="secret_abc123"
        ... )
    """
    # Resolve API key
    resolved_api_key, error = _resolve_notion_api_key(api_key)
    if error:
        return error

    request_body: Dict[str, Any] = {
        "query": query,
        "page_size": min(page_size, 100),  # Cap at API maximum
    }

    if filter_type:
        if filter_type not in ["page", "database"]:
            return {
                "success": False,
                "error": "filter_type must be 'page' or 'database'",
            }
        request_body["filter"] = {"value": filter_type, "property": "object"}

    result = _make_notion_request(resolved_api_key, "/search", method="POST", data=request_body)

    if result["success"]:
        data = result["data"]
        return {
            "success": True,
            "results": data.get("results", []),
            "has_more": data.get("has_more", False),
            "next_cursor": data.get("next_cursor"),
        }
    else:
        return {
            "success": False,
            "error": result.get("error", "Unknown error"),
            "status_code": result.get("status_code", 0),
        }


def notion_query_database(
    database_id: str,
    filter_conditions: Optional[Dict[str, Any]] = None,
    sorts: Optional[List[Dict[str, str]]] = None,
    page_size: int = 100,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Query a Notion database with filters and sorting.

    Args:
        database_id: ID of the database to query
        filter_conditions: Optional filter object (see Notion API docs for structure)
        sorts: Optional list of sort objects, e.g., [{"property": "Name", "direction": "ascending"}]
        page_size: Number of results to return (default: 100, max: 100)
        api_key: Optional Notion Integration Token (uses configured credentials if not provided)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if query succeeded
        - results: List of database pages (if successful)
        - has_more: Boolean indicating if more results exist
        - error: Error message (if failed)

    Example:
        >>> # Query all items (using configured credentials)
        >>> result = notion_query_database(
        ...     database_id="database-uuid"
        ... )

        >>> # Query with filter and sort using explicit API key
        >>> result = notion_query_database(
        ...     database_id="database-uuid",
        ...     filter_conditions={
        ...         "property": "Status",
        ...         "select": {"equals": "In Progress"}
        ...     },
        ...     sorts=[{"property": "Due Date", "direction": "ascending"}],
        ...     api_key="secret_abc123"
        ... )
    """
    # Resolve API key
    resolved_api_key, error = _resolve_notion_api_key(api_key)
    if error:
        return error

    request_body: Dict[str, Any] = {
        "page_size": min(page_size, 100),  # Cap at API maximum
    }

    if filter_conditions:
        request_body["filter"] = filter_conditions

    if sorts:
        request_body["sorts"] = sorts

    result = _make_notion_request(
        resolved_api_key, f"/databases/{database_id}/query", method="POST", data=request_body
    )

    if result["success"]:
        data = result["data"]
        return {
            "success": True,
            "results": data.get("results", []),
            "has_more": data.get("has_more", False),
            "next_cursor": data.get("next_cursor"),
        }
    else:
        return {
            "success": False,
            "error": result.get("error", "Unknown error"),
            "status_code": result.get("status_code", 0),
        }


def notion_append_blocks(
    page_id: str,
    blocks: List[Dict[str, Any]],
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Append content blocks to a page.

    Args:
        page_id: ID of the page to append blocks to
        blocks: List of block objects to append
        api_key: Optional Notion Integration Token (uses configured credentials if not provided)

    Returns:
        Dictionary containing:
        - success: Boolean indicating if blocks were appended
        - blocks: List of created block objects (if successful)
        - error: Error message (if failed)

    Example:
        >>> blocks = [
        ...     {
        ...         "object": "block",
        ...         "type": "heading_2",
        ...         "heading_2": {
        ...             "rich_text": [{"text": {"content": "New Section"}}]
        ...         }
        ...     },
        ...     {
        ...         "object": "block",
        ...         "type": "paragraph",
        ...         "paragraph": {
        ...             "rich_text": [{"text": {"content": "Some content here."}}]
        ...         }
        ...     },
        ...     {
        ...         "object": "block",
        ...         "type": "to_do",
        ...         "to_do": {
        ...             "rich_text": [{"text": {"content": "Task item"}}],
        ...             "checked": False
        ...         }
        ...     }
        ... ]
        >>> # Using configured credentials
        >>> result = notion_append_blocks(
        ...     page_id="page-uuid",
        ...     blocks=blocks
        ... )
        >>> # Using explicit API key
        >>> result = notion_append_blocks(
        ...     page_id="page-uuid",
        ...     blocks=blocks,
        ...     api_key="secret_abc123"
        ... )
    """
    # Resolve API key
    resolved_api_key, error = _resolve_notion_api_key(api_key)
    if error:
        return error

    if not blocks:
        return {
            "success": False,
            "error": "No blocks provided to append",
        }

    request_body = {"children": blocks}

    result = _make_notion_request(resolved_api_key, f"/blocks/{page_id}/children", method="PATCH", data=request_body)

    if result["success"]:
        data = result["data"]
        return {
            "success": True,
            "blocks": data.get("results", []),
        }
    else:
        return {
            "success": False,
            "error": result.get("error", "Unknown error"),
            "status_code": result.get("status_code", 0),
        }


# Register tools with LangChain
TOOLS: List[StructuredTool] = [
    StructuredTool.from_function(
        func=notion_create_page,
        name="notion_create_page",
        description="Create a new page in Notion workspace (in a page or database). Uses configured Notion credentials from Agent Settings if api_key not provided.",
    ),
    StructuredTool.from_function(
        func=notion_get_page,
        name="notion_get_page",
        description="Retrieve a page from Notion by its ID. Uses configured Notion credentials from Agent Settings if api_key not provided.",
    ),
    StructuredTool.from_function(
        func=notion_update_page,
        name="notion_update_page",
        description="Update a Notion page's properties or archive status. Uses configured Notion credentials from Agent Settings if api_key not provided.",
    ),
    StructuredTool.from_function(
        func=notion_search,
        name="notion_search",
        description="Search across pages and databases in Notion workspace. Uses configured Notion credentials from Agent Settings if api_key not provided.",
    ),
    StructuredTool.from_function(
        func=notion_query_database,
        name="notion_query_database",
        description="Query a Notion database with filters and sorting. Uses configured Notion credentials from Agent Settings if api_key not provided.",
    ),
    StructuredTool.from_function(
        func=notion_append_blocks,
        name="notion_append_blocks",
        description="Append content blocks to an existing Notion page. Uses configured Notion credentials from Agent Settings if api_key not provided.",
    ),
]
