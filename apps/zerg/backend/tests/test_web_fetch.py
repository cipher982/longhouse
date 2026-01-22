"""Tests for web_fetch tool."""

from unittest.mock import MagicMock
from unittest.mock import patch

import httpx

from zerg.tools.builtin.web_fetch import web_fetch


class TestWebFetch:
    """Test suite for web_fetch tool."""

    @patch("zerg.tools.builtin.web_fetch.httpx.Client")
    @patch("zerg.tools.builtin.web_fetch.trafilatura.extract")
    def test_successful_fetch(self, mock_extract, mock_client_cls):
        """Test successful webpage fetch and extraction."""
        # Arrange
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.text = "<html>test content</html>"
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_extract.return_value = "# Test Page\n\nThis is test content."

        # Act
        result = web_fetch(url="https://example.com/page")

        # Assert
        assert result["ok"] is True
        assert result["url"] == "https://example.com/page"
        assert "Test Page" in result["content"]
        assert result["word_count"] > 0
        assert "error" not in result

        # Verify calls
        mock_client.get.assert_called_once()
        mock_extract.assert_called_once()

    @patch("zerg.tools.builtin.web_fetch.httpx.Client")
    def test_fetch_returns_none(self, mock_client_cls):
        """Test when response body is empty."""
        # Arrange
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.text = ""
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value.__enter__.return_value = mock_client

        # Act
        result = web_fetch(url="https://nonexistent-domain.com")

        # Assert
        assert result["ok"] is False
        assert "error" in result
        assert "empty response" in result["error"]

    @patch("zerg.tools.builtin.web_fetch.httpx.Client")
    @patch("zerg.tools.builtin.web_fetch.trafilatura.extract")
    def test_extract_returns_none(self, mock_extract, mock_client_cls):
        """Test when extract returns None (failed extraction)."""
        # Arrange
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.text = "<html>content</html>"
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_extract.return_value = None

        # Act
        result = web_fetch(url="https://example.com")

        # Assert
        assert result["ok"] is False
        assert "error" in result
        assert "Failed to extract content" in result["error"]

    @patch("zerg.tools.builtin.web_fetch.httpx.Client")
    @patch("zerg.tools.builtin.web_fetch.trafilatura.extract")
    def test_with_all_options(self, mock_extract, mock_client_cls):
        """Test with all optional parameters."""
        # Arrange
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.text = "<html>test</html>"
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_extract.return_value = "Test content with [link](https://example.com) and ![image](img.jpg)"

        # Act
        result = web_fetch(
            url="https://example.com",
            include_links=True,
            include_images=True,
            timeout_secs=60,
        )

        # Assert
        assert result["ok"] is True
        mock_extract.assert_called_once_with(
            mock_response.text,
            include_links=True,
            include_images=True,
            output_format="markdown",
        )

    @patch("zerg.tools.builtin.web_fetch.httpx.Client")
    @patch("zerg.tools.builtin.web_fetch.trafilatura.extract")
    def test_word_count_calculation(self, mock_extract, mock_client_cls):
        """Test word count is calculated correctly."""
        # Arrange
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.text = "<html>test</html>"
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_extract.return_value = "One two three four five"

        # Act
        result = web_fetch(url="https://example.com")

        # Assert
        assert result["ok"] is True
        assert result["word_count"] == 5

    @patch("zerg.tools.builtin.web_fetch.httpx.Client")
    def test_fetch_exception(self, mock_client_cls):
        """Test handling of exceptions during fetch."""
        # Arrange
        mock_client = MagicMock()
        mock_client.get.side_effect = httpx.RequestError("Network error", request=httpx.Request("GET", "https://example.com"))
        mock_client_cls.return_value.__enter__.return_value = mock_client

        # Act
        result = web_fetch(url="https://example.com")

        # Assert
        assert result["ok"] is False
        assert "error" in result
        assert "Network error" in result["error"]

    def test_ssrf_protection_localhost(self):
        """Test SSRF protection blocks localhost."""
        result = web_fetch(url="http://localhost:8080/admin")
        assert result["ok"] is False
        assert "security" in result["error"].lower() or "blocked" in result["error"].lower()

    def test_ssrf_protection_127_0_0_1(self):
        """Test SSRF protection blocks 127.0.0.1."""
        result = web_fetch(url="http://127.0.0.1/secret")
        assert result["ok"] is False
        assert "security" in result["error"].lower() or "blocked" in result["error"].lower()

    def test_ssrf_protection_private_ip_10(self):
        """Test SSRF protection blocks 10.x.x.x range."""
        result = web_fetch(url="http://10.0.0.1/internal")
        assert result["ok"] is False
        assert "security" in result["error"].lower() or "blocked" in result["error"].lower()

    def test_ssrf_protection_private_ip_192(self):
        """Test SSRF protection blocks 192.168.x.x range."""
        result = web_fetch(url="http://192.168.1.1/router")
        assert result["ok"] is False
        assert "security" in result["error"].lower() or "blocked" in result["error"].lower()

    def test_ssrf_protection_private_ip_172(self):
        """Test SSRF protection blocks 172.16-31.x.x range."""
        result = web_fetch(url="http://172.16.0.1/private")
        assert result["ok"] is False
        assert "security" in result["error"].lower() or "blocked" in result["error"].lower()

    def test_ssrf_protection_aws_metadata(self):
        """Test SSRF protection blocks AWS metadata endpoint."""
        result = web_fetch(url="http://169.254.169.254/latest/meta-data/")
        assert result["ok"] is False
        assert "security" in result["error"].lower() or "blocked" in result["error"].lower()

    def test_ssrf_protection_ipv6_localhost(self):
        """Test SSRF protection blocks IPv6 localhost."""
        result = web_fetch(url="http://[::1]/admin")
        assert result["ok"] is False
        assert "security" in result["error"].lower() or "blocked" in result["error"].lower()

    def test_invalid_url_format(self):
        """Test handling of invalid URL format."""
        result = web_fetch(url="not-a-valid-url")
        assert result["ok"] is False
        assert "error" in result

    def test_empty_url(self):
        """Test handling of empty URL."""
        result = web_fetch(url="")
        assert result["ok"] is False
        assert "error" in result

    @patch("zerg.tools.builtin.web_fetch.httpx.Client")
    @patch("zerg.tools.builtin.web_fetch.trafilatura.extract")
    def test_empty_content_extraction(self, mock_extract, mock_client_cls):
        """Test handling of empty content after extraction."""
        # Arrange
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.text = "<html>test</html>"
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_extract.return_value = ""

        # Act
        result = web_fetch(url="https://example.com")

        # Assert
        assert result["ok"] is True
        assert result["content"] == ""
        assert result["word_count"] == 0

    @patch("zerg.tools.builtin.web_fetch.httpx.Client")
    @patch("zerg.tools.builtin.web_fetch.trafilatura.extract")
    def test_whitespace_only_content(self, mock_extract, mock_client_cls):
        """Test handling of whitespace-only content."""
        # Arrange
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.text = "<html>test</html>"
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_extract.return_value = "   \n\n   "

        # Act
        result = web_fetch(url="https://example.com")

        # Assert
        assert result["ok"] is True
        assert result["word_count"] == 0
