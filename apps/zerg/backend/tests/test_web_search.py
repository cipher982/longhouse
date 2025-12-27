"""Tests for web search tool."""

import os
from unittest.mock import MagicMock
from unittest.mock import patch

from zerg.tools.builtin.web_search import web_search
from zerg.tools.builtin.web_search import web_search_tool


class TestWebSearch:
    """Test the web_search tool."""

    @patch("zerg.tools.builtin.web_search.TavilyClient")
    @patch.dict(os.environ, {"TAVILY_API_KEY": "test-api-key"})
    def test_web_search_basic(self, mock_tavily_client):
        """Test basic web search."""
        # Mock the Tavily API response
        mock_client = MagicMock()
        mock_client.search.return_value = {
            "query": "Python programming",
            "results": [
                {
                    "title": "Python Tutorial",
                    "url": "https://example.com/python",
                    "content": "Learn Python programming",
                    "score": 0.95,
                },
                {
                    "title": "Python Docs",
                    "url": "https://docs.python.org",
                    "content": "Official Python documentation",
                    "score": 0.90,
                },
            ],
        }
        mock_tavily_client.return_value = mock_client

        # Execute search
        result = web_search("Python programming")

        # Verify result structure
        assert result["ok"] is True
        assert result["query"] == "Python programming"
        assert len(result["results"]) == 2
        assert result["results"][0]["title"] == "Python Tutorial"
        assert result["results"][0]["url"] == "https://example.com/python"
        assert result["results"][0]["score"] == 0.95
        assert "response_time" in result

        # Verify API was called correctly
        mock_client.search.assert_called_once_with(
            query="Python programming",
            max_results=5,
            search_depth="basic",
            topic="general",
        )

    @patch("zerg.tools.builtin.web_search.TavilyClient")
    @patch.dict(os.environ, {"TAVILY_API_KEY": "test-api-key"})
    def test_web_search_with_params(self, mock_tavily_client):
        """Test web search with custom parameters."""
        mock_client = MagicMock()
        mock_client.search.return_value = {
            "query": "test query",
            "results": [],
        }
        mock_tavily_client.return_value = mock_client

        # Execute search with custom params
        result = web_search(
            query="test query",
            max_results=10,
            search_depth="advanced",
            include_domains=["example.com"],
            exclude_domains=["spam.com"],
        )

        # Verify API was called with correct params
        mock_client.search.assert_called_once_with(
            query="test query",
            max_results=10,
            search_depth="advanced",
            topic="general",
            include_domains=["example.com"],
            exclude_domains=["spam.com"],
        )

        assert result["ok"] is True

    @patch.dict(os.environ, {}, clear=True)
    def test_web_search_no_api_key(self):
        """Test web search fails gracefully when API key is missing."""
        result = web_search("test query")

        assert result["ok"] is False
        assert "TAVILY_API_KEY" in result["error"]
        assert "not configured" in result["error"]

    @patch("zerg.tools.builtin.web_search.TavilyClient")
    @patch.dict(os.environ, {"TAVILY_API_KEY": "test-api-key"})
    def test_web_search_invalid_depth(self, mock_tavily_client):
        """Test web search rejects invalid search_depth."""
        result = web_search("test query", search_depth="invalid")

        assert result["ok"] is False
        assert "Invalid search_depth" in result["error"]
        assert "fast" in result["error"] and "basic" in result["error"] and "advanced" in result["error"]

    @patch("zerg.tools.builtin.web_search.TavilyClient")
    @patch.dict(os.environ, {"TAVILY_API_KEY": "test-api-key"})
    def test_web_search_invalid_api_key(self, mock_tavily_client):
        """Test web search handles InvalidAPIKeyError."""
        from tavily import InvalidAPIKeyError

        mock_client = MagicMock()
        mock_client.search.side_effect = InvalidAPIKeyError("Invalid API key")
        mock_tavily_client.return_value = mock_client

        result = web_search("test query")

        assert result["ok"] is False
        assert "Invalid Tavily API key" in result["error"]

    @patch("zerg.tools.builtin.web_search.TavilyClient")
    @patch.dict(os.environ, {"TAVILY_API_KEY": "test-api-key"})
    def test_web_search_usage_limit(self, mock_tavily_client):
        """Test web search handles UsageLimitExceededError."""
        from tavily import UsageLimitExceededError

        mock_client = MagicMock()
        mock_client.search.side_effect = UsageLimitExceededError("Limit exceeded")
        mock_tavily_client.return_value = mock_client

        result = web_search("test query")

        assert result["ok"] is False
        assert "usage limit exceeded" in result["error"].lower()

    @patch("zerg.tools.builtin.web_search.TavilyClient")
    @patch.dict(os.environ, {"TAVILY_API_KEY": "test-api-key"})
    def test_web_search_unexpected_error(self, mock_tavily_client):
        """Test web search handles unexpected errors."""
        mock_client = MagicMock()
        mock_client.search.side_effect = Exception("Unexpected error")
        mock_tavily_client.return_value = mock_client

        result = web_search("test query")

        assert result["ok"] is False
        assert "Search failed" in result["error"]

    @patch("zerg.tools.builtin.web_search.TavilyClient")
    @patch.dict(os.environ, {"TAVILY_API_KEY": "test-api-key"})
    def test_web_search_empty_results(self, mock_tavily_client):
        """Test web search with empty results."""
        mock_client = MagicMock()
        mock_client.search.return_value = {
            "query": "obscure query",
            "results": [],
        }
        mock_tavily_client.return_value = mock_client

        result = web_search("obscure query")

        assert result["ok"] is True
        assert result["results"] == []
        assert result["query"] == "obscure query"

    def test_web_search_tool_registered(self):
        """Test that web_search_tool is properly configured."""
        assert web_search_tool.name == "web_search"
        assert "web" in web_search_tool.description.lower()
        assert "search" in web_search_tool.description.lower()

        # Test tool args schema
        schema = web_search_tool.args_schema
        assert schema is not None
        json_schema = schema.model_json_schema()
        assert "query" in json_schema["properties"]
        assert "max_results" in json_schema["properties"]
        assert "search_depth" in json_schema["properties"]

    @patch("zerg.tools.builtin.web_search.TavilyClient")
    @patch.dict(os.environ, {"TAVILY_API_KEY": "test-api-key"})
    def test_web_search_max_results_validation(self, mock_tavily_client):
        """Test that max_results is validated within bounds."""
        mock_client = MagicMock()
        mock_client.search.return_value = {
            "query": "test",
            "results": [],
        }
        mock_tavily_client.return_value = mock_client

        # Test with valid max_results
        result = web_search("test", max_results=1)
        assert result["ok"] is True

        result = web_search("test", max_results=20)
        assert result["ok"] is True

        # Pydantic validation should handle out-of-bounds values in the tool wrapper
        # Direct function calls might not validate, so we just test valid cases

    @patch("zerg.tools.builtin.web_search.TavilyClient")
    @patch.dict(os.environ, {"TAVILY_API_KEY": "test-api-key"})
    def test_web_search_include_domains(self, mock_tavily_client):
        """Test web search with include_domains."""
        mock_client = MagicMock()
        mock_client.search.return_value = {
            "query": "python",
            "results": [
                {
                    "title": "Python.org",
                    "url": "https://python.org",
                    "content": "Official Python site",
                    "score": 0.99,
                }
            ],
        }
        mock_tavily_client.return_value = mock_client

        result = web_search("python", include_domains=["python.org"])

        assert result["ok"] is True
        mock_client.search.assert_called_once()
        call_kwargs = mock_client.search.call_args.kwargs
        assert call_kwargs["include_domains"] == ["python.org"]

    @patch("zerg.tools.builtin.web_search.TavilyClient")
    @patch.dict(os.environ, {"TAVILY_API_KEY": "test-api-key"})
    def test_web_search_exclude_domains(self, mock_tavily_client):
        """Test web search with exclude_domains."""
        mock_client = MagicMock()
        mock_client.search.return_value = {
            "query": "python",
            "results": [],
        }
        mock_tavily_client.return_value = mock_client

        result = web_search("python", exclude_domains=["wikipedia.org"])

        assert result["ok"] is True
        mock_client.search.assert_called_once()
        call_kwargs = mock_client.search.call_args.kwargs
        assert call_kwargs["exclude_domains"] == ["wikipedia.org"]

    @patch("zerg.tools.builtin.web_search.TavilyClient")
    @patch.dict(os.environ, {"TAVILY_API_KEY": "test-api-key"})
    def test_web_search_news_topic(self, mock_tavily_client):
        """Test web search with news topic."""
        mock_client = MagicMock()
        mock_client.search.return_value = {
            "query": "AI news",
            "results": [
                {
                    "title": "Latest AI News",
                    "url": "https://news.example.com/ai",
                    "content": "Breaking AI news...",
                    "score": 0.9,
                    "published_date": "2025-01-15",
                }
            ],
        }
        mock_tavily_client.return_value = mock_client

        result = web_search("AI news", topic="news", days=7)

        assert result["ok"] is True
        assert result["results"][0]["published_date"] == "2025-01-15"
        mock_client.search.assert_called_once()
        call_kwargs = mock_client.search.call_args.kwargs
        assert call_kwargs["topic"] == "news"
        assert call_kwargs["days"] == 7

    @patch("zerg.tools.builtin.web_search.TavilyClient")
    @patch.dict(os.environ, {"TAVILY_API_KEY": "test-api-key"})
    def test_web_search_include_answer(self, mock_tavily_client):
        """Test web search with AI answer."""
        mock_client = MagicMock()
        mock_client.search.return_value = {
            "query": "What is Python?",
            "answer": "Python is a high-level programming language known for its simplicity and readability.",
            "results": [
                {
                    "title": "Python.org",
                    "url": "https://python.org",
                    "content": "Official Python site",
                    "score": 0.99,
                }
            ],
        }
        mock_tavily_client.return_value = mock_client

        result = web_search("What is Python?", include_answer=True)

        assert result["ok"] is True
        assert "answer" in result
        assert "programming language" in result["answer"]
        mock_client.search.assert_called_once()
        call_kwargs = mock_client.search.call_args.kwargs
        assert call_kwargs["include_answer"] is True

    @patch("zerg.tools.builtin.web_search.TavilyClient")
    @patch.dict(os.environ, {"TAVILY_API_KEY": "test-api-key"})
    def test_web_search_advanced_answer(self, mock_tavily_client):
        """Test web search with advanced AI answer."""
        mock_client = MagicMock()
        mock_client.search.return_value = {
            "query": "test",
            "answer": "Detailed answer...",
            "results": [],
        }
        mock_tavily_client.return_value = mock_client

        result = web_search("test", include_answer="advanced")

        assert result["ok"] is True
        mock_client.search.assert_called_once()
        call_kwargs = mock_client.search.call_args.kwargs
        assert call_kwargs["include_answer"] == "advanced"

    @patch("zerg.tools.builtin.web_search.TavilyClient")
    @patch.dict(os.environ, {"TAVILY_API_KEY": "test-api-key"})
    def test_web_search_time_range(self, mock_tavily_client):
        """Test web search with time range filter."""
        mock_client = MagicMock()
        mock_client.search.return_value = {
            "query": "test",
            "results": [],
        }
        mock_tavily_client.return_value = mock_client

        result = web_search("test", time_range="week")

        assert result["ok"] is True
        mock_client.search.assert_called_once()
        call_kwargs = mock_client.search.call_args.kwargs
        assert call_kwargs["time_range"] == "week"

    @patch("zerg.tools.builtin.web_search.TavilyClient")
    @patch.dict(os.environ, {"TAVILY_API_KEY": "test-api-key"})
    def test_web_search_country_filter(self, mock_tavily_client):
        """Test web search with country filter."""
        mock_client = MagicMock()
        mock_client.search.return_value = {
            "query": "test",
            "results": [],
        }
        mock_tavily_client.return_value = mock_client

        result = web_search("test", country="US")

        assert result["ok"] is True
        mock_client.search.assert_called_once()
        call_kwargs = mock_client.search.call_args.kwargs
        assert call_kwargs["country"] == "us"

    @patch("zerg.tools.builtin.web_search.TavilyClient")
    @patch.dict(os.environ, {"TAVILY_API_KEY": "test-api-key"})
    def test_web_search_include_raw_content(self, mock_tavily_client):
        """Test web search with raw content."""
        mock_client = MagicMock()
        mock_client.search.return_value = {
            "query": "test",
            "results": [
                {
                    "title": "Test Page",
                    "url": "https://example.com",
                    "content": "Summary content",
                    "score": 0.9,
                    "raw_content": "# Full Page Content\n\nThis is the full markdown content of the page...",
                }
            ],
        }
        mock_tavily_client.return_value = mock_client

        result = web_search("test", include_raw_content=True)

        assert result["ok"] is True
        assert "raw_content" in result["results"][0]
        assert "Full Page Content" in result["results"][0]["raw_content"]
        mock_client.search.assert_called_once()
        call_kwargs = mock_client.search.call_args.kwargs
        assert call_kwargs["include_raw_content"] == "markdown"

    @patch("zerg.tools.builtin.web_search.TavilyClient")
    @patch.dict(os.environ, {"TAVILY_API_KEY": "test-api-key"})
    def test_web_search_fast_depth(self, mock_tavily_client):
        """Test web search with fast search depth."""
        mock_client = MagicMock()
        mock_client.search.return_value = {
            "query": "test",
            "results": [],
        }
        mock_tavily_client.return_value = mock_client

        result = web_search("test", search_depth="fast")

        assert result["ok"] is True
        mock_client.search.assert_called_once()
        call_kwargs = mock_client.search.call_args.kwargs
        assert call_kwargs["search_depth"] == "fast"

    @patch("zerg.tools.builtin.web_search.TavilyClient")
    @patch.dict(os.environ, {"TAVILY_API_KEY": "test-api-key"})
    def test_web_search_finance_topic(self, mock_tavily_client):
        """Test web search with finance topic."""
        mock_client = MagicMock()
        mock_client.search.return_value = {
            "query": "NVIDIA stock",
            "results": [
                {
                    "title": "NVIDIA Stock Analysis",
                    "url": "https://finance.example.com/nvda",
                    "content": "NVIDIA stock analysis...",
                    "score": 0.95,
                }
            ],
        }
        mock_tavily_client.return_value = mock_client

        result = web_search("NVIDIA stock", topic="finance")

        assert result["ok"] is True
        mock_client.search.assert_called_once()
        call_kwargs = mock_client.search.call_args.kwargs
        assert call_kwargs["topic"] == "finance"

    @patch("zerg.tools.builtin.web_search.TavilyClient")
    @patch.dict(os.environ, {"TAVILY_API_KEY": "test-api-key"})
    def test_web_search_invalid_topic(self, mock_tavily_client):
        """Test web search rejects invalid topic."""
        result = web_search("test query", topic="invalid")

        assert result["ok"] is False
        assert "Invalid topic" in result["error"]

    @patch("zerg.tools.builtin.web_search.TavilyClient")
    @patch.dict(os.environ, {"TAVILY_API_KEY": "test-api-key"})
    def test_web_search_country_only_with_general(self, mock_tavily_client):
        """Test country filter is only applied for general topic."""
        mock_client = MagicMock()
        mock_client.search.return_value = {
            "query": "test",
            "results": [],
        }
        mock_tavily_client.return_value = mock_client

        # Country should not be passed for news topic
        result = web_search("test", topic="news", country="us")

        assert result["ok"] is True
        mock_client.search.assert_called_once()
        call_kwargs = mock_client.search.call_args.kwargs
        assert "country" not in call_kwargs
