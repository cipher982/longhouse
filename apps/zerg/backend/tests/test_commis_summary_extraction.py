"""Tests for commis summary extraction.

These tests verify that _extract_summary_from_output produces informative summaries
for both successful and failed commis executions.
"""

import pytest

from zerg.services.commis_job_processor import _extract_summary_from_output


class TestExtractSummaryFromOutput:
    """Tests for the summary extraction function."""

    # -------------------------------------------------------------------------
    # Success cases
    # -------------------------------------------------------------------------

    def test_success_short_output(self):
        """Short successful output should be returned as-is."""
        output = "Task completed successfully."
        result = _extract_summary_from_output(output, status="success")
        assert result == "Task completed successfully."

    def test_success_empty_output(self):
        """Empty output for success should return '(No output)'."""
        result = _extract_summary_from_output(None, status="success")
        assert result == "(No output)"

        result = _extract_summary_from_output("", status="success")
        assert result == "(No output)"

        result = _extract_summary_from_output("   ", status="success")
        assert result == "(No output)"

    def test_success_long_output_truncated(self):
        """Long output should be truncated at word boundary."""
        output = "A" * 200  # No spaces, will truncate with ...
        result = _extract_summary_from_output(output, status="success")
        assert len(result) <= 153  # 150 + "..."
        assert result.endswith("...")

    def test_success_truncates_at_word_boundary(self):
        """Long output should truncate at word boundary."""
        # Create output that's > 150 chars with words
        words = "word " * 40  # 200 chars
        result = _extract_summary_from_output(words, status="success")
        assert len(result) <= 153
        assert result.endswith("...")
        # Should not end in middle of "word"
        assert "word..." in result or result.endswith("word...")

    def test_success_cleans_newlines(self):
        """Newlines should be replaced with spaces."""
        output = "Line 1\nLine 2\r\nLine 3"
        result = _extract_summary_from_output(output, status="success")
        assert "\n" not in result
        assert "\r" not in result
        assert result == "Line 1 Line 2 Line 3"

    # -------------------------------------------------------------------------
    # Failure cases
    # -------------------------------------------------------------------------

    def test_failed_with_error_message(self):
        """Failed status with error should use error in summary."""
        result = _extract_summary_from_output(
            output=None,
            status="failed",
            error="Connection refused: could not connect to database"
        )
        assert result.startswith("[FAILED] ")
        assert "Connection refused" in result

    def test_failed_with_long_error_truncated(self):
        """Long error messages should be truncated."""
        error = "E" * 200
        result = _extract_summary_from_output(
            output=None,
            status="failed",
            error=error
        )
        assert result.startswith("[FAILED] ")
        assert len(result) <= 153
        assert result.endswith("...")

    def test_failed_no_error_uses_output(self):
        """Failed status with no error but with output should use output."""
        result = _extract_summary_from_output(
            output="Some partial output before failure",
            status="failed",
            error=None
        )
        assert result.startswith("[FAILED] ")
        assert "partial output" in result

    def test_failed_no_error_no_output(self):
        """Failed status with no error and no output should use fallback."""
        result = _extract_summary_from_output(
            output=None,
            status="failed",
            error=None
        )
        assert result == "[FAILED] (No error details available)"

    def test_failed_empty_error_uses_output(self):
        """Empty error string should fall back to output."""
        result = _extract_summary_from_output(
            output="Partial output",
            status="failed",
            error=""
        )
        assert result.startswith("[FAILED] ")
        assert "Partial output" in result

    def test_failed_whitespace_error_uses_output(self):
        """Whitespace-only error should fall back to output."""
        result = _extract_summary_from_output(
            output="Partial output",
            status="failed",
            error="   "
        )
        assert result.startswith("[FAILED] ")
        assert "Partial output" in result

    # -------------------------------------------------------------------------
    # Timeout cases
    # -------------------------------------------------------------------------

    def test_timeout_with_error_message(self):
        """Timeout status should use [TIMEOUT] prefix."""
        result = _extract_summary_from_output(
            output=None,
            status="timeout",
            error="Execution timed out after 300s"
        )
        assert result.startswith("[TIMEOUT] ")
        assert "timed out" in result

    def test_timeout_no_error_uses_output(self):
        """Timeout with no error but output should use output."""
        result = _extract_summary_from_output(
            output="Processing took too long",
            status="timeout",
            error=None
        )
        assert result.startswith("[TIMEOUT] ")
        assert "Processing" in result

    def test_timeout_no_error_no_output(self):
        """Timeout with no error and no output should use fallback."""
        result = _extract_summary_from_output(
            output=None,
            status="timeout",
            error=None
        )
        assert result == "[TIMEOUT] (No error details available)"

    # -------------------------------------------------------------------------
    # Cancelled cases
    # -------------------------------------------------------------------------

    def test_cancelled_with_error_message(self):
        """Cancelled status should use [CANCELLED] prefix and error."""
        result = _extract_summary_from_output(
            output=None,
            status="cancelled",
            error="Cancelled by user"
        )
        assert result.startswith("[CANCELLED] ")
        assert "Cancelled by user" in result

    def test_cancelled_no_error_uses_output(self):
        """Cancelled status with output should use output."""
        result = _extract_summary_from_output(
            output="Partial work done",
            status="cancelled",
            error=None
        )
        assert result.startswith("[CANCELLED] ")
        assert "Partial work done" in result

    def test_cancelled_no_error_no_output(self):
        """Cancelled status with no error and no output should use fallback."""
        result = _extract_summary_from_output(
            output=None,
            status="cancelled",
            error=None
        )
        assert result == "[CANCELLED] (No error details available)"

    # -------------------------------------------------------------------------
    # Edge cases
    # -------------------------------------------------------------------------

    def test_default_status_is_success(self):
        """Default status should be success."""
        result = _extract_summary_from_output("Test output")
        assert not result.startswith("[FAILED]")
        assert not result.startswith("[TIMEOUT]")
        assert result == "Test output"

    def test_summary_max_length_respected(self):
        """Summary should never exceed 150 characters."""
        # Test with all combinations
        for status in ["success", "failed", "timeout", "cancelled"]:
            for output in [None, "x" * 200]:
                for error in [None, "e" * 200]:
                    result = _extract_summary_from_output(
                        output=output,
                        status=status,
                        error=error
                    )
                    # Allow up to 153 chars (150 + "...")
                    assert len(result) <= 153, f"Too long for status={status}: {len(result)}"

    def test_error_cleans_newlines(self):
        """Error messages should have newlines cleaned."""
        result = _extract_summary_from_output(
            output=None,
            status="failed",
            error="Line 1\nLine 2\nLine 3"
        )
        assert "\n" not in result
        assert "[FAILED] Line 1 Line 2 Line 3" == result

    def test_priority_error_over_output(self):
        """For failures, error should be prioritized over output."""
        result = _extract_summary_from_output(
            output="This is the output",
            status="failed",
            error="This is the error"
        )
        assert "This is the error" in result
        assert "This is the output" not in result
