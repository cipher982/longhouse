"""HTTP-related tools for making web requests."""

import json
import logging
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from urllib.parse import urlencode

import httpx
from langchain_core.tools import StructuredTool

logger = logging.getLogger(__name__)


def http_request(
    url: str,
    method: str = "GET",
    params: Optional[Dict[str, str]] = None,
    data: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: Optional[float] = 30.0,
) -> Dict[str, Any]:
    """Make an HTTP request with specified method.

    Args:
        url: The URL to request
        method: HTTP method (GET, POST, PUT, DELETE, etc.)
        params: Optional query parameters as a dictionary
        data: Optional request body data (for POST/PUT/PATCH)
        headers: Optional HTTP headers as a dictionary
        timeout: Request timeout in seconds (default: 30)

    Returns:
        Dictionary containing:
        - status_code: HTTP status code
        - headers: Response headers as a dict
        - body: Response body (as text or parsed JSON if applicable)
        - error: Error message if request failed

    Example:
        >>> http_request("https://api.example.com/data", method="POST", data={"key": "value"})
        {"status_code": 200, "headers": {...}, "body": {...}}
    """
    try:
        method = method.upper()

        # Build URL with params if provided
        if params:
            url = f"{url}?{urlencode(params)}"

        # Default headers
        default_headers = {"User-Agent": "Zerg-Agent/1.0"}
        if headers:
            default_headers.update(headers)

        # Prepare request data
        json_data = None
        if data and method in ["POST", "PUT", "PATCH"]:
            if isinstance(data, dict):
                json_data = data
                default_headers["Content-Type"] = "application/json"

        # Make the request
        with httpx.Client() as client:
            response = client.request(
                method=method, url=url, headers=default_headers, json=json_data, timeout=timeout, follow_redirects=True
            )

        # Prepare response data
        result = {
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "url": str(response.url),  # Final URL after redirects
        }

        # Try to parse JSON response
        content_type = response.headers.get("content-type", "").lower()
        if "application/json" in content_type:
            try:
                result["body"] = response.json()
            except json.JSONDecodeError:
                result["body"] = response.text
        else:
            result["body"] = response.text

        # Truncate very long responses
        if isinstance(result["body"], str) and len(result["body"]) > 10000:
            result["body"] = result["body"][:10000] + "... (truncated)"
            result["truncated"] = True

        return result

    except httpx.TimeoutException:
        logger.error(f"HTTP {method} timeout for URL: {url}")
        return {"status_code": 0, "error": f"Request timed out after {timeout} seconds", "url": url}
    except httpx.RequestError as e:
        logger.error(f"HTTP {method} error for URL {url}: {e}")
        return {"status_code": 0, "error": f"Request failed: {str(e)}", "url": url}
    except Exception as e:
        logger.exception(f"Unexpected error in http_request for URL: {url}")
        return {"status_code": 0, "error": f"Unexpected error: {str(e)}", "url": url}


TOOLS: List[StructuredTool] = [
    StructuredTool.from_function(
        func=http_request,
        name="http_request",
        description="Make an HTTP request with specified method and return the response",
    ),
]
