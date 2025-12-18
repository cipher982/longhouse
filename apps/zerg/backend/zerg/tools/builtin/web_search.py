"""Web search tool using Tavily API."""

import logging
import os
import time
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel
from pydantic import Field
from tavily import InvalidAPIKeyError
from tavily import TavilyClient
from tavily import UsageLimitExceededError

logger = logging.getLogger(__name__)


class WebSearchInput(BaseModel):
    """Input schema for web_search tool."""

    query: str = Field(description="Search query string")
    max_results: int = Field(
        default=5,
        description="Maximum number of results to return (1-20)",
        ge=1,
        le=20,
    )
    search_depth: str = Field(
        default="basic",
        description="Search depth: 'basic' for quick results, 'advanced' for more thorough search",
    )
    include_domains: Optional[List[str]] = Field(
        default=None,
        description="Optional list of domains to include (e.g., ['python.org', 'github.com'])",
    )
    exclude_domains: Optional[List[str]] = Field(
        default=None,
        description="Optional list of domains to exclude (e.g., ['wikipedia.org'])",
    )


def web_search(
    query: str,
    max_results: int = 5,
    search_depth: str = "basic",
    include_domains: Optional[List[str]] = None,
    exclude_domains: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Search the web using Tavily API.

    This tool searches the web for relevant information using the Tavily search API.
    Use this to find current information, documentation, tutorials, or any other
    web-accessible content.

    Args:
        query: Search query string. Be specific for better results.
        max_results: Maximum number of results to return (1-20, default 5)
        search_depth: Search depth - 'basic' for quick results (default),
                     'advanced' for more thorough search (slower, uses more credits)
        include_domains: Optional list of domains to restrict search to
        exclude_domains: Optional list of domains to exclude from results

    Returns:
        Dictionary containing:
        - ok: Boolean indicating success
        - results: List of search results (if successful)
          Each result contains:
          - title: Page title
          - url: Page URL
          - content: Main content/summary
          - score: Relevance score
        - query: The search query that was executed
        - response_time: Time taken for the search in seconds
        - error: Error message (if failed)

    Example:
        >>> web_search("Python asyncio tutorial", max_results=3)
        {
            "ok": True,
            "query": "Python asyncio tutorial",
            "results": [
                {
                    "title": "asyncio - Python Documentation",
                    "url": "https://docs.python.org/3/library/asyncio.html",
                    "content": "asyncio is a library to write concurrent code...",
                    "score": 0.95
                }
            ],
            "response_time": 0.6
        }
    """
    start_time = time.time()

    try:
        # Get API key from environment
        api_key = os.getenv("TAVILY_API_KEY")
        if not api_key:
            logger.error("TAVILY_API_KEY not found in environment")
            return {
                "ok": False,
                "error": "TAVILY_API_KEY not configured. Please set the environment variable.",
            }

        # Validate search_depth
        if search_depth not in ["basic", "advanced"]:
            return {
                "ok": False,
                "error": f"Invalid search_depth '{search_depth}'. Must be 'basic' or 'advanced'.",
            }

        # Initialize Tavily client
        client = TavilyClient(api_key=api_key)

        # Build search parameters
        search_params: Dict[str, Any] = {
            "query": query,
            "max_results": max_results,
            "search_depth": search_depth,
        }

        if include_domains:
            search_params["include_domains"] = include_domains

        if exclude_domains:
            search_params["exclude_domains"] = exclude_domains

        # Execute search
        logger.info(f"Executing Tavily search: query='{query}', max_results={max_results}, depth={search_depth}")
        response = client.search(**search_params)

        # Calculate response time
        response_time = time.time() - start_time

        # Extract results
        results = []
        for item in response.get("results", []):
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "content": item.get("content", ""),
                "score": item.get("score", 0.0),
            })

        return {
            "ok": True,
            "query": query,
            "results": results,
            "response_time": round(response_time, 2),
        }

    except InvalidAPIKeyError:
        logger.error("Invalid Tavily API key")
        return {
            "ok": False,
            "error": "Invalid Tavily API key. Please check your TAVILY_API_KEY environment variable.",
        }

    except UsageLimitExceededError:
        logger.error("Tavily API usage limit exceeded")
        return {
            "ok": False,
            "error": "Tavily API usage limit exceeded. Please check your account quota.",
        }

    except Exception as e:
        logger.exception(f"Unexpected error in web_search: {e}")
        return {
            "ok": False,
            "error": f"Search failed: {str(e)}",
        }


# Create LangChain tool
web_search_tool = StructuredTool.from_function(
    func=web_search,
    name="web_search",
    description=(
        "Search the web for current information, documentation, tutorials, or other content. "
        "Use this when you need to look up facts, find resources, or get up-to-date information "
        "that may not be in your training data. Returns relevant web pages with titles, URLs, "
        "and content summaries."
    ),
    args_schema=WebSearchInput,
)

# Export tools list for registry
TOOLS = [web_search_tool]
