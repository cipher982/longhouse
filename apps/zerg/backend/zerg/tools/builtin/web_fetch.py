"""Web fetch tool using trafilatura for content extraction."""

import ipaddress
import logging
from typing import Any
from typing import Dict
from urllib.parse import urlparse

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
    ipaddress.ip_network("169.254.0.0/16"),  # AWS metadata endpoint
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


def _is_safe_url(url: str) -> tuple[bool, str]:
    """Check if URL is safe (not targeting private networks).

    Args:
        url: URL to check

    Returns:
        Tuple of (is_safe, error_message)
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return False, "URL has no hostname"

        # Check blocked hostnames
        if hostname in BLOCKED_HOSTS:
            return False, f"Security: Access to {hostname} is blocked"

        # Check if hostname is an IP in a blocked range
        try:
            ip = ipaddress.ip_address(hostname)
            for network in BLOCKED_RANGES:
                if ip in network:
                    return False, f"Security: Access to private IP range {network} is blocked"
        except ValueError:
            # Not an IP address, it's a hostname - this is OK
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

        # Fetch the webpage
        logger.info(f"Fetching URL: {url}")
        downloaded = trafilatura.fetch_url(url)

        if not downloaded:
            logger.warning(f"Failed to fetch URL: {url}")
            return {
                "ok": False,
                "url": url,
                "error": "Failed to fetch URL. The site may be unavailable or blocking requests.",
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
