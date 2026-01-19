"""HTTP-related tools for making web requests."""

import ipaddress
import json
import logging
import os
import socket
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from urllib.parse import urlencode
from urllib.parse import urljoin
from urllib.parse import urlparse

import httpx
from langchain_core.tools import StructuredTool

logger = logging.getLogger(__name__)

ALLOWED_SCHEMES = {"http", "https"}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 5


def _is_private_address(hostname: str) -> bool:
    host = (hostname or "").strip().lower().strip(".")
    if not host:
        return True

    # Localhost aliases
    if host == "localhost" or host.endswith(".localhost"):
        return True

    # IPv6 literal in URL may include brackets
    host = host.strip("[]")

    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False

    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return True

    return False


def _validate_url(url: str) -> tuple[bool, str | None]:
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        return False, f"Invalid URL: {exc}"

    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        return False, f"Blocked URL scheme '{parsed.scheme}'"

    if not parsed.hostname:
        return False, "Invalid URL hostname"

    allow_private = os.getenv("ALLOW_PRIVATE_HTTP_REQUESTS") == "1"
    if not allow_private and _is_private_address(parsed.hostname):
        return False, f"Blocked private host '{parsed.hostname}'"

    return True, None


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

        with httpx.Client() as client:
            current_url = url
            response = None
            for _ in range(MAX_REDIRECTS + 1):
                valid, error = _validate_url(current_url)
                if not valid:
                    return {"status_code": 0, "error": error, "url": current_url}

                response = client.request(
                    method=method,
                    url=current_url,
                    headers=default_headers,
                    json=json_data,
                    timeout=timeout,
                    follow_redirects=False,
                )

                if response.status_code in REDIRECT_STATUSES and response.headers.get("location"):
                    location = response.headers["location"]
                    current_url = urljoin(current_url, location)
                    # Per RFC, 303 should switch to GET without body
                    if response.status_code == 303 and method != "GET":
                        method = "GET"
                        json_data = None
                    continue
                break

            if response is None:
                return {"status_code": 0, "error": "No response received", "url": current_url}

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
