"""Tests for tool error detection logic."""

from zerg.tools.result_utils import check_tool_error


class TestErrorEnvelopeDetection:
    """Tests for check_tool_error function."""

    def test_detects_legacy_tool_error_prefix(self):
        """Test detection of <tool-error> prefix."""
        result = "<tool-error> Connection failed"
        is_error, error_msg = check_tool_error(result)

        assert is_error is True
        assert error_msg == "<tool-error> Connection failed"

    def test_detects_error_prefix(self):
        """Test detection of Error: prefix."""
        result = "Error: Tool 'foo' not found"
        is_error, error_msg = check_tool_error(result)

        assert is_error is True
        assert error_msg == "Error: Tool 'foo' not found"

    def test_detects_json_error_envelope(self):
        """Test detection of JSON error envelope with double quotes."""
        result = '{"ok": false, "error_type": "execution_error", "user_message": "SSH failed"}'

        is_error, error_msg = check_tool_error(result)

        assert is_error is True
        assert error_msg == "SSH failed"

    def test_detects_python_literal_error_envelope(self):
        """Test detection of Python literal error envelope (str(dict) format)."""
        # This is what str(dict) produces - single quotes, capitalized False
        result = "{'ok': False, 'error_type': 'execution_error', 'user_message': 'SSH failed'}"

        is_error, error_msg = check_tool_error(result)

        assert is_error is True
        assert error_msg == "SSH failed"

    def test_error_envelope_without_user_message_uses_error_type(self):
        """Test that error_type is used when user_message is missing."""
        result = "{'ok': False, 'error_type': 'execution_error'}"

        is_error, error_msg = check_tool_error(result)

        assert is_error is True
        assert error_msg == "execution_error"

    def test_error_envelope_without_message_or_type_uses_default(self):
        """Test fallback message when neither user_message nor error_type present."""
        result = "{'ok': False}"

        is_error, error_msg = check_tool_error(result)

        assert is_error is True
        assert error_msg == "Tool returned ok=false"

    def test_success_envelope_not_detected_as_error(self):
        """Test that success envelopes are not detected as errors."""
        result = "{'ok': True, 'data': 'Success message'}"

        is_error, error_msg = check_tool_error(result)

        assert is_error is False
        assert error_msg is None

    def test_non_envelope_string_not_detected(self):
        """Test that regular strings are not detected as errors."""
        result = "This is just a normal result string"

        is_error, error_msg = check_tool_error(result)

        assert is_error is False
        assert error_msg is None

    def test_malformed_dict_string_not_detected(self):
        """Test that malformed dict strings don't cause crashes."""
        result = "{'ok': False, 'missing_quote: True}"

        is_error, error_msg = check_tool_error(result)

        # Should not crash, should return False since parsing failed
        assert is_error is False
        assert error_msg is None

    def test_error_envelope_with_nested_data(self):
        """Test error envelope with nested data structures."""
        result = (
            "{'ok': False, 'error_type': 'validation_error', "
            "'user_message': 'Invalid input', 'details': {'field': 'email'}}"
        )

        is_error, error_msg = check_tool_error(result)

        assert is_error is True
        assert error_msg == "Invalid input"

    def test_json_error_envelope_with_camelcase_keys(self):
        """Test JSON format with common variations."""
        result = '{"ok": false, "errorType": "validation_error", "userMessage": "Bad request"}'

        is_error, error_msg = check_tool_error(result)

        # Should detect ok: false even if keys don't match expected format
        assert is_error is True
        # Will use fallback since user_message (snake_case) not found
        assert "Bad request" in error_msg or "ok=false" in error_msg.lower()
