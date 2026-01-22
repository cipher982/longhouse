"""Web fetch tool using trafilatura for content extraction."""

import ipaddress
import logging
import socket
from typing import Any
from typing import Dict
from urllib.parse import urlparse

import httpx
import trafilatura
from langchain_core.tools import StructuredTool
from pydantic import BaseModel
from pydantic import Field

logger = logging.getLogger(__name__)

# SSRF protection: Block private IP ranges
BLOCKED_HOSTS = ["localhost", "127.0.0.1", "0.0.0.0", "::1"]
BLOCKED_RANGES = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),  # Link-local / AWS metadata
    ipaddress.ip_network("127.0.0.0/8"),  # Loopback
]


class WebFetchInput(BaseModel):
    """Input schema for web_fetch tool."""

    url: str = Field(description="URL of the webpage to fetch")
    include_links: bool = Field(
        default=True,
        description="Include hyperlinks in the markdown output",
    )
    include_images: bool = Field(
        default=False,
        description="Include image references in the markdown output",
    )
    timeout_secs: int = Field(
        default=30,
        description="Request timeout in seconds",
        ge=1,
        le=120,
    )


def _is_ip_blocked(ip_str: str) -> tuple[bool, str]:
    """Check if an IP address is in a blocked range.

    Args:
        ip_str: IP address string

    Returns:
        Tuple of (is_blocked, error_message)
    """
    try:
        ip = ipaddress.ip_address(ip_str)
        for network in BLOCKED_RANGES:
            if ip in network:
                return True, f"Security: Access to private IP range {network} is blocked"
        return False, ""
    except ValueError:
        return False, ""


def _is_safe_url(url: str) -> tuple[bool, str]:
    """Check if URL is safe (not targeting private networks).

    Performs DNS resolution to detect hostname-to-private-IP SSRF bypasses.

    Args:
        url: URL to check

    Returns:
        Tuple of (is_safe, error_message)
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        scheme = parsed.scheme

        if not hostname:
            return False, "URL has no hostname"

        # Only allow http/https
        if scheme not in ("http", "https"):
            return False, f"Security: Scheme '{scheme}' is not allowed (only http/https)"

        # Check blocked hostnames
        if hostname.lower() in BLOCKED_HOSTS:
            return False, f"Security: Access to {hostname} is blocked"

        # Check if hostname is already an IP in a blocked range
        is_blocked, err = _is_ip_blocked(hostname)
        if is_blocked:
            return False, err

        # DNS resolution check: resolve hostname and verify IPs aren't private
        try:
            # Resolve all IPs for the hostname
            addr_infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
            for addr_info in addr_infos:
                resolved_ip = addr_info[4][0]
                is_blocked, err = _is_ip_blocked(resolved_ip)
                if is_blocked:
                    return False, f"Security: {hostname} resolves to blocked IP ({resolved_ip})"
        except socket.gaierror:
            # DNS resolution failed - let the actual fetch handle this error
            pass

        return True, ""
    except Exception as e:
        return False, f"Invalid URL format: {str(e)}"


def web_fetch(
    url: str,
    include_links: bool = True,
    include_images: bool = False,
    timeout_secs: int = 30,
) -> Dict[str, Any]:
    """Fetch a webpage and extract content as clean markdown.

    This tool fetches a webpage, extracts the main content (removing navigation,
    ads, and other boilerplate), and returns it as clean markdown. Use this when
    you need to read documentation, articles, or other web content.

    Args:
        url: URL of the webpage to fetch (must be http or https)
        include_links: Include hyperlinks in markdown output (default True)
        include_images: Include image references in markdown output (default False)
        timeout_secs: Request timeout in seconds (default 30, max 120)

    Returns:
        Dictionary containing:
        - ok: Boolean indicating success
        - url: The fetched URL
        - content: Extracted markdown content
        - word_count: Approximate word count
        - error: Error message (if failed)

    Example:
        >>> web_fetch("https://docs.python.org/3/library/asyncio.html")
        {
            "ok": True,
            "url": "https://docs.python.org/3/library/asyncio.html",
            "content": "# asyncio — Asynchronous I/O\\n\\nasyncio is a library...",
            "word_count": 1234
        }
    """
    try:
        # Validate URL
        if not url or not url.strip():
            return {
                "ok": False,
                "error": "URL cannot be empty",
            }

        url = url.strip()

        # SSRF protection
        is_safe, error_msg = _is_safe_url(url)
        if not is_safe:
            logger.warning(f"Blocked unsafe URL: {url} - {error_msg}")
            return {
                "ok": False,
                "error": error_msg,
            }

        # Fetch the webpage with timeout
        logger.info(f"Fetching URL: {url} (timeout={timeout_secs}s)")
        try:
            with httpx.Client(timeout=timeout_secs, follow_redirects=True) as client:
                response = client.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; Swarmlet/1.0)"})
                response.raise_for_status()
                downloaded = response.text
        except httpx.TimeoutException:
            logger.warning(f"Timeout fetching URL: {url}")
            return {
                "ok": False,
                "url": url,
                "error": f"Request timed out after {timeout_secs} seconds.",
            }
        except httpx.HTTPStatusError as e:
            logger.warning(f"HTTP error fetching URL: {url} - {e.response.status_code}")
            return {
                "ok": False,
                "url": url,
                "error": f"HTTP {e.response.status_code}: {e.response.reason_phrase}",
            }
        except httpx.RequestError as e:
            logger.warning(f"Request error fetching URL: {url} - {e}")
            return {
                "ok": False,
                "url": url,
                "error": f"Failed to fetch URL: {str(e)}",
            }

        if not downloaded:
            logger.warning(f"Empty response from URL: {url}")
            return {
                "ok": False,
                "url": url,
                "error": "Failed to fetch URL. The site returned an empty response.",
            }

        # Extract content as markdown
        logger.info(f"Extracting content from: {url}")
        content = trafilatura.extract(
            downloaded,
            include_links=include_links,
            include_images=include_images,
            output_format="markdown",
        )

        if content is None:
            logger.warning(f"Failed to extract content from: {url}")
            return {
                "ok": False,
                "url": url,
                "error": "Failed to extract content. The page may be JavaScript-rendered or have no extractable content.",
            }

        # Calculate word count (approximate)
        word_count = len(content.split())

        return {
            "ok": True,
            "url": url,
            "content": content,
            "word_count": word_count,
        }

    except Exception as e:
        logger.exception(f"Unexpected error in web_fetch for URL {url}: {e}")
        return {
            "ok": False,
            "url": url if "url" in locals() else "",
            "error": f"Failed to fetch webpage: {str(e)}",
        }


# Create LangChain tool
web_fetch_tool = StructuredTool.from_function(
    func=web_fetch,
    name="web_fetch",
    description=(
        "Fetch a webpage and extract its main content as clean markdown. "
        "Use this to read documentation, articles, blog posts, or any other web content. "
        "The tool removes navigation, ads, and other boilerplate, returning only the main content. "
        "Works best with static HTML pages; JavaScript-rendered sites may not work."
    ),
    args_schema=WebFetchInput,
)

# Export tools list for registry
TOOLS = [web_fetch_tool]
