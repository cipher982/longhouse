"""Tests for tool result utilities (error detection, redaction, previews)."""

from zerg.tools.result_utils import (
    check_tool_error,
    redact_sensitive_args,
    safe_preview,
    SENSITIVE_KEYS,
)


class TestErrorDetection:
    """Tests for check_tool_error function."""

    def test_none_input_handled(self):
        """Test that None input is handled gracefully."""
        is_error, error_msg = check_tool_error(None)

        assert is_error is False
        assert error_msg is None

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

    def test_success_envelope_not_detected_as_error(self):
        """Test that success envelopes are not detected as errors."""
        result = "{'ok': True, 'data': 'Success message'}"

        is_error, error_msg = check_tool_error(result)

        assert is_error is False
        assert error_msg is None


class TestSecretRedaction:
    """Tests for redact_sensitive_args function."""

    def test_redact_flat_dict_with_api_key(self):
        """Test redacting API key from flat dict."""
        args = {
            "host": "api.example.com",
            "api_key": "sk-secret123",
            "timeout": 30,
        }

        redacted = redact_sensitive_args(args)

        assert redacted["host"] == "api.example.com"
        assert redacted["api_key"] == "[REDACTED]"
        assert redacted["timeout"] == 30

    def test_redact_nested_dict(self):
        """Test redacting secrets in nested dicts."""
        args = {
            "config": {
                "api_key": "sk-secret123",
                "endpoint": "https://api.example.com",
            },
            "username": "test_user",
        }

        redacted = redact_sensitive_args(args)

        assert redacted["config"]["api_key"] == "[REDACTED]"
        assert redacted["config"]["endpoint"] == "https://api.example.com"
        assert redacted["username"] == "test_user"

    def test_redact_secrets_in_list_of_dicts(self):
        """Test redacting secrets inside list-of-dict structures (Slack/Discord case)."""
        args = {
            "attachments": [
                {"title": "Status", "value": "OK"},
                {"title": "token", "value": "sk-live-abc123"},
                {"title": "Server", "value": "cube"},
            ],
        }

        redacted = redact_sensitive_args(args)

        # First attachment should be unchanged
        assert redacted["attachments"][0] == {"title": "Status", "value": "OK"}

        # Second attachment has 'token' key - should be redacted
        assert redacted["attachments"][1]["title"] == "token"
        assert redacted["attachments"][1]["value"] == "[REDACTED]"

        # Third attachment should be unchanged
        assert redacted["attachments"][2] == {"title": "Server", "value": "cube"}

    def test_redact_deeply_nested_list_dict_structure(self):
        """Test redaction in complex nested structures."""
        args = {
            "blocks": [
                {
                    "type": "section",
                    "fields": [
                        {"key": "host", "value": "cube"},
                        {"key": "api_key", "value": "secret123"},
                    ],
                },
            ],
        }

        redacted = redact_sensitive_args(args)

        fields = redacted["blocks"][0]["fields"]
        assert fields[0]["key"] == "host"
        assert fields[0]["value"] == "cube"
        assert fields[1]["key"] == "api_key"
        assert fields[1]["value"] == "[REDACTED]"

    def test_redact_various_sensitive_key_names(self):
        """Test that all sensitive key patterns are caught."""
        args = {
            "api_key": "secret1",
            "apiKey": "secret2",
            "token": "secret3",
            "password": "secret4",
            "secret": "secret5",
            "bearer": "secret6",
            "authorization": "secret7",
            "access_token": "secret8",
            "private_key": "secret9",
        }

        redacted = redact_sensitive_args(args)

        # All should be redacted
        for key in args.keys():
            assert redacted[key] == "[REDACTED]", f"Key '{key}' was not redacted"

    def test_redact_case_insensitive(self):
        """Test that key matching is case-insensitive."""
        args = {
            "API_KEY": "secret1",
            "ApiKey": "secret2",
            "API_key": "secret3",
        }

        redacted = redact_sensitive_args(args)

        assert redacted["API_KEY"] == "[REDACTED]"
        assert redacted["ApiKey"] == "[REDACTED]"
        assert redacted["API_key"] == "[REDACTED]"

    def test_redact_partial_match(self):
        """Test that partial matches work (e.g., 'github_token' contains 'token')."""
        args = {
            "github_token": "ghp_secret123",
            "user_token": "usr_secret456",
            "not_sensitive": "visible",
        }

        redacted = redact_sensitive_args(args)

        assert redacted["github_token"] == "[REDACTED]"
        assert redacted["user_token"] == "[REDACTED]"
        assert redacted["not_sensitive"] == "visible"

    def test_redact_tuple_of_dicts(self):
        """Test redacting tuple containing dicts."""
        args = {
            "headers": (
                {"name": "Content-Type", "value": "application/json"},
                {"name": "Authorization", "value": "Bearer sk-123"},
            ),
        }

        redacted = redact_sensitive_args(args)

        assert isinstance(redacted["headers"], tuple)
        assert redacted["headers"][0]["value"] == "application/json"
        assert redacted["headers"][1]["value"] == "[REDACTED]"

    def test_redact_primitives_unchanged(self):
        """Test that primitive values are returned unchanged."""
        assert redact_sensitive_args("string") == "string"
        assert redact_sensitive_args(123) == 123
        assert redact_sensitive_args(True) is True
        assert redact_sensitive_args(None) is None

    def test_redact_mixed_structure(self):
        """Test redaction in mixed nested structures."""
        args = {
            "name": "test",
            "config": {
                "api_key": "secret",
                "settings": ["option1", "option2"],
            },
            "headers": [
                {"name": "X-API-Key", "value": "secret123"},
                "plain_string",
            ],
        }

        redacted = redact_sensitive_args(args)

        assert redacted["name"] == "test"
        assert redacted["config"]["api_key"] == "[REDACTED]"
        assert redacted["config"]["settings"] == ["option1", "option2"]
        assert redacted["headers"][0]["value"] == "[REDACTED]"
        assert redacted["headers"][1] == "plain_string"


class TestSafePreview:
    """Tests for safe_preview function."""

    def test_none_input_handled(self):
        """Test that None input is handled gracefully."""
        preview = safe_preview(None)
        assert preview == "(None)"

    def test_dict_input_stringified(self):
        """Test that dict input is converted to string."""
        preview = safe_preview({"key": "value"})
        assert "key" in preview
        assert "value" in preview

    def test_short_content_unchanged(self):
        """Test that content shorter than max_len is unchanged."""
        content = "Short message"
        preview = safe_preview(content, max_len=200)

        assert preview == "Short message"

    def test_long_content_truncated(self):
        """Test that long content is truncated with ellipsis."""
        content = "A" * 300
        preview = safe_preview(content, max_len=100)

        assert len(preview) == 100
        assert preview.endswith("...")
        assert preview.startswith("A" * 50)

    def test_exact_length_unchanged(self):
        """Test content exactly at max_len is unchanged."""
        content = "A" * 200
        preview = safe_preview(content, max_len=200)

        assert preview == content
        assert not preview.endswith("...")

    def test_custom_max_length(self):
        """Test using custom max_len parameter."""
        content = "This is a longer message"
        preview = safe_preview(content, max_len=10)

        assert len(preview) == 10
        assert preview == "This is..."


class TestRegressionCases:
    """Regression tests for specific security issues."""

    def test_lone_key_field_redacted(self):
        """Regression: lone {"key": "sk-123"} should be redacted."""
        args = {"key": "sk-live-abc123"}
        redacted = redact_sensitive_args(args)

        assert redacted["key"] == "[REDACTED]"

    def test_lone_api_key_field_redacted(self):
        """Regression: lone {"api_key": "..."} should always be redacted."""
        args = {"api_key": "sk-secret-xyz"}
        redacted = redact_sensitive_args(args)

        assert redacted["api_key"] == "[REDACTED]"

    def test_key_value_pair_with_sensitive_semantic_key(self):
        """Key-value pair where semantic key is sensitive should redact value."""
        args = {"key": "Authorization", "value": "Bearer token123"}
        redacted = redact_sensitive_args(args)

        # "key" field preserved, "value" field redacted
        assert redacted["key"] == "Authorization"
        assert redacted["value"] == "[REDACTED]"

    def test_key_value_pair_with_non_sensitive_semantic_key(self):
        """Key-value pair where semantic key is NOT sensitive should not redact."""
        args = {"key": "Status", "value": "Running"}
        redacted = redact_sensitive_args(args)

        # Neither should be redacted
        assert redacted["key"] == "Status"
        assert redacted["value"] == "Running"

    def test_http_headers_with_authorization(self):
        """Real-world case: HTTP headers array with Authorization header."""
        args = {
            "headers": [
                {"key": "Content-Type", "value": "application/json"},
                {"key": "Authorization", "value": "Bearer sk_live_12345"},
            ],
        }

        redacted = redact_sensitive_args(args)

        # First header unchanged
        assert redacted["headers"][0] == {"key": "Content-Type", "value": "application/json"}

        # Second header value redacted
        assert redacted["headers"][1]["key"] == "Authorization"
        assert redacted["headers"][1]["value"] == "[REDACTED]"


class TestSensitiveKeysConstant:
    """Tests for SENSITIVE_KEYS constant."""

    def test_sensitive_keys_is_frozen(self):
        """Test that SENSITIVE_KEYS is a frozenset (immutable)."""
        assert isinstance(SENSITIVE_KEYS, frozenset)

    def test_sensitive_keys_contains_common_terms(self):
        """Test that common sensitive terms are included."""
        required_terms = {
            "key",
            "api_key",
            "token",
            "secret",
            "password",
            "credential",
            "authorization",
        }

        for term in required_terms:
            assert term in SENSITIVE_KEYS, f"Missing sensitive term: {term}"
