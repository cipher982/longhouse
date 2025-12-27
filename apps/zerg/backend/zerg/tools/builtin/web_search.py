"""Web search tool using Tavily API.

Features:
- General web search, news search, and finance search
- AI-generated answer summaries
- Time-based filtering (day/week/month/year or custom date range)
- Domain filtering (include/exclude)
- Geographic filtering by country
- Multiple search depths (fast, basic, advanced)
"""

import logging
import os
import time
from typing import Any
from typing import Dict
from typing import List
from typing import Literal
from typing import Optional
from typing import Union

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
    topic: Literal["general", "news", "finance"] = Field(
        default="general",
        description="Search topic: 'general' for web search, 'news' for recent news, 'finance' for financial data",
    )
    search_depth: Literal["fast", "basic", "advanced"] = Field(
        default="basic",
        description="Search depth: 'fast' for quick results (cheapest), 'basic' for standard search, 'advanced' for thorough search (2x credits)",
    )
    time_range: Optional[Literal["day", "week", "month", "year"]] = Field(
        default=None,
        description="Filter results by time: 'day', 'week', 'month', or 'year'. For news, prefer using 'days' parameter.",
    )
    days: Optional[int] = Field(
        default=None,
        description="For news search: number of days back to search (e.g., 7 for past week). Only used when topic='news'.",
        ge=1,
        le=365,
    )
    include_answer: Union[bool, Literal["basic", "advanced"]] = Field(
        default=False,
        description="Include AI-generated answer: False (none), True or 'basic' (quick answer), 'advanced' (detailed answer, uses more credits)",
    )
    include_raw_content: bool = Field(
        default=False,
        description="Include full page content (markdown format). Useful for deep analysis but increases response size.",
    )
    include_domains: Optional[List[str]] = Field(
        default=None,
        description="Optional list of domains to include (e.g., ['python.org', 'github.com'])",
    )
    exclude_domains: Optional[List[str]] = Field(
        default=None,
        description="Optional list of domains to exclude (e.g., ['wikipedia.org'])",
    )
    country: Optional[str] = Field(
        default=None,
        description="Two-letter country code to prioritize results from (e.g., 'us', 'uk', 'de'). Only for topic='general'.",
    )


def web_search(
    query: str,
    max_results: int = 5,
    topic: Literal["general", "news", "finance"] = "general",
    search_depth: Literal["fast", "basic", "advanced"] = "basic",
    time_range: Optional[Literal["day", "week", "month", "year"]] = None,
    days: Optional[int] = None,
    include_answer: Union[bool, Literal["basic", "advanced"]] = False,
    include_raw_content: bool = False,
    include_domains: Optional[List[str]] = None,
    exclude_domains: Optional[List[str]] = None,
    country: Optional[str] = None,
) -> Dict[str, Any]:
    """Search the web using Tavily API.

    This tool searches the web for relevant information using the Tavily search API.
    Use this to find current information, documentation, tutorials, news, or any other
    web-accessible content.

    Args:
        query: Search query string. Be specific for better results.
        max_results: Maximum number of results to return (1-20, default 5)
        topic: Search topic - 'general' (default), 'news' for recent news, 'finance' for financial data
        search_depth: Search depth - 'fast' (quickest), 'basic' (default), 'advanced' (thorough, 2x credits)
        time_range: Filter by time - 'day', 'week', 'month', or 'year'
        days: For news search - number of days back (1-365)
        include_answer: Include AI summary - False, True/'basic', or 'advanced' (detailed)
        include_raw_content: Include full page content in markdown format
        include_domains: List of domains to restrict search to
        exclude_domains: List of domains to exclude from results
        country: Two-letter country code to prioritize results from (e.g., 'us', 'uk')

    Returns:
        Dictionary containing:
        - ok: Boolean indicating success
        - results: List of search results (if successful)
          Each result contains:
          - title: Page title
          - url: Page URL
          - content: Main content/summary
          - score: Relevance score
          - raw_content: Full page content (if include_raw_content=True)
          - published_date: Publication date (for news)
        - answer: AI-generated answer (if include_answer is set)
        - query: The search query that was executed
        - response_time: Time taken for the search in seconds
        - error: Error message (if failed)

    Examples:
        # Basic search
        >>> web_search("Python asyncio tutorial", max_results=3)

        # News search with AI answer
        >>> web_search("AI regulation", topic="news", days=7, include_answer=True)

        # Finance search
        >>> web_search("NVIDIA stock analysis", topic="finance", include_answer="advanced")

        # Search specific sites
        >>> web_search("react hooks", include_domains=["reactjs.org", "github.com"])
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
        valid_depths = ["fast", "basic", "advanced"]
        if search_depth not in valid_depths:
            return {
                "ok": False,
                "error": f"Invalid search_depth '{search_depth}'. Must be one of: {valid_depths}",
            }

        # Validate topic
        valid_topics = ["general", "news", "finance"]
        if topic not in valid_topics:
            return {
                "ok": False,
                "error": f"Invalid topic '{topic}'. Must be one of: {valid_topics}",
            }

        # Initialize Tavily client
        client = TavilyClient(api_key=api_key)

        # Build search parameters
        search_params: Dict[str, Any] = {
            "query": query,
            "max_results": max_results,
            "search_depth": search_depth,
            "topic": topic,
        }

        # Time filtering
        if time_range:
            search_params["time_range"] = time_range

        if days is not None and topic == "news":
            search_params["days"] = days

        # Answer generation
        if include_answer:
            search_params["include_answer"] = include_answer

        # Raw content
        if include_raw_content:
            search_params["include_raw_content"] = "markdown"

        # Domain filtering
        if include_domains:
            search_params["include_domains"] = include_domains

        if exclude_domains:
            search_params["exclude_domains"] = exclude_domains

        # Geographic filtering (only for general topic)
        if country and topic == "general":
            search_params["country"] = country.lower()

        # Execute search
        logger.info(f"Executing Tavily search: query='{query}', topic={topic}, depth={search_depth}, max_results={max_results}")
        response = client.search(**search_params)

        # Calculate response time
        response_time = time.time() - start_time

        # Extract results
        results = []
        for item in response.get("results", []):
            result_item = {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "content": item.get("content", ""),
                "score": item.get("score", 0.0),
            }
            # Include raw content if requested and available
            if include_raw_content and item.get("raw_content"):
                result_item["raw_content"] = item.get("raw_content")
            # Include published date for news
            if item.get("published_date"):
                result_item["published_date"] = item.get("published_date")
            results.append(result_item)

        response_data: Dict[str, Any] = {
            "ok": True,
            "query": query,
            "results": results,
            "response_time": round(response_time, 2),
        }

        # Include AI answer if requested and available
        if include_answer and response.get("answer"):
            response_data["answer"] = response.get("answer")

        return response_data

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
        "Search the web for current information, news, finance data, documentation, or other content. "
        "Features: topic filtering (general/news/finance), AI-generated answers, time-based filtering, "
        "domain filtering, and geographic targeting. Use topic='news' for recent events, topic='finance' "
        "for stock/market data. Set include_answer=True for AI summaries. Returns web pages with titles, "
        "URLs, content summaries, and optionally full page content."
    ),
    args_schema=WebSearchInput,
)

# Export tools list for registry
TOOLS = [web_search_tool]
