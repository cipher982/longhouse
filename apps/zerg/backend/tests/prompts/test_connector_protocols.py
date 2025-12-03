"""Tests for connector protocol definitions.

This module tests the static protocol strings that are injected into the
agent system prompt to define how the agent should interpret dynamic
connector status.
"""

import re

import pytest

from zerg.prompts.connector_protocols import (
    CAPABILITY_PROTOCOL,
    CONNECTOR_PROTOCOL,
    ERROR_HANDLING_PROTOCOL,
    TEMPORAL_AWARENESS_PROTOCOL,
    get_connector_protocols,
)


def test_get_connector_protocols_contains_all_sections():
    """Test that get_connector_protocols includes all 4 protocol blocks."""
    protocols = get_connector_protocols()

    assert isinstance(protocols, str)

    # Should contain all 4 protocol sections
    assert "<connector_protocol>" in protocols
    assert "<capability_protocol>" in protocols
    assert "<error_handling>" in protocols
    assert "<temporal_awareness>" in protocols


def test_get_connector_protocols_has_closing_tags():
    """Test that all XML tags in protocols are properly closed."""
    protocols = get_connector_protocols()

    # Check that opening and closing tags match
    assert protocols.count("<connector_protocol>") == protocols.count("</connector_protocol>")
    assert protocols.count("<capability_protocol>") == protocols.count("</capability_protocol>")
    assert protocols.count("<error_handling>") == protocols.count("</error_handling>")
    assert protocols.count("<temporal_awareness>") == protocols.count("</temporal_awareness>")


def test_connector_protocol_xml_structure():
    """Test that CONNECTOR_PROTOCOL has proper XML structure."""
    assert CONNECTOR_PROTOCOL.startswith("<connector_protocol>")
    assert CONNECTOR_PROTOCOL.endswith("</connector_protocol>")


def test_capability_protocol_xml_structure():
    """Test that CAPABILITY_PROTOCOL has proper XML structure."""
    assert CAPABILITY_PROTOCOL.startswith("<capability_protocol>")
    assert CAPABILITY_PROTOCOL.endswith("</capability_protocol>")


def test_error_handling_protocol_xml_structure():
    """Test that ERROR_HANDLING_PROTOCOL has proper XML structure."""
    assert ERROR_HANDLING_PROTOCOL.startswith("<error_handling>")
    assert ERROR_HANDLING_PROTOCOL.endswith("</error_handling>")


def test_temporal_awareness_protocol_xml_structure():
    """Test that TEMPORAL_AWARENESS_PROTOCOL has proper XML structure."""
    assert TEMPORAL_AWARENESS_PROTOCOL.startswith("<temporal_awareness>")
    assert TEMPORAL_AWARENESS_PROTOCOL.endswith("</temporal_awareness>")


def test_connector_protocol_not_empty():
    """Test that CONNECTOR_PROTOCOL contains meaningful content."""
    # Remove XML tags and whitespace
    content = CONNECTOR_PROTOCOL.replace("<connector_protocol>", "").replace("</connector_protocol>", "").strip()

    assert len(content) > 100  # Should have substantial content
    # Should mention key status values
    assert "connected" in content
    assert "not_configured" in content
    assert "invalid_credentials" in content


def test_connector_protocol_defines_all_status_values():
    """Test that CONNECTOR_PROTOCOL defines all expected status values."""
    # Should define how to interpret each status
    assert "connected" in CONNECTOR_PROTOCOL
    assert "not_configured" in CONNECTOR_PROTOCOL
    assert "invalid_credentials" in CONNECTOR_PROTOCOL
    assert "rate_limited" in CONNECTOR_PROTOCOL
    assert "disabled_by_admin" in CONNECTOR_PROTOCOL


def test_connector_protocol_has_rules():
    """Test that CONNECTOR_PROTOCOL includes behavioral rules."""
    # Should have a Rules section with guidelines
    assert "Rules:" in CONNECTOR_PROTOCOL or "rules:" in CONNECTOR_PROTOCOL.lower()

    # Should warn against bad practices
    assert "NEVER" in CONNECTOR_PROTOCOL or "never" in CONNECTOR_PROTOCOL.lower()


def test_capability_protocol_not_empty():
    """Test that CAPABILITY_PROTOCOL contains meaningful content."""
    content = CAPABILITY_PROTOCOL.replace("<capability_protocol>", "").replace("</capability_protocol>", "").strip()

    assert len(content) > 100
    # Should provide guidance on capability responses
    assert "what can you do" in content.lower() or "capabilities" in content.lower()


def test_capability_protocol_has_response_format():
    """Test that CAPABILITY_PROTOCOL defines a response format."""
    # Should show how to structure capability responses
    assert "Ready now" in CAPABILITY_PROTOCOL or "ready now" in CAPABILITY_PROTOCOL.lower()
    assert "Available after setup" in CAPABILITY_PROTOCOL or "available" in CAPABILITY_PROTOCOL.lower()

    # Should include example
    assert "Example" in CAPABILITY_PROTOCOL or "example" in CAPABILITY_PROTOCOL.lower()


def test_error_handling_protocol_not_empty():
    """Test that ERROR_HANDLING_PROTOCOL contains meaningful content."""
    content = ERROR_HANDLING_PROTOCOL.replace("<error_handling>", "").replace("</error_handling>", "").strip()

    assert len(content) > 100
    # Should define error handling approach
    assert "error" in content.lower()


def test_error_handling_protocol_defines_error_types():
    """Test that ERROR_HANDLING_PROTOCOL defines expected error types."""
    # Should mention specific error types
    assert "connector_not_configured" in ERROR_HANDLING_PROTOCOL
    assert "invalid_credentials" in ERROR_HANDLING_PROTOCOL
    assert "rate_limited" in ERROR_HANDLING_PROTOCOL
    assert "permission_denied" in ERROR_HANDLING_PROTOCOL


def test_error_handling_protocol_has_envelope_structure():
    """Test that ERROR_HANDLING_PROTOCOL describes the error envelope."""
    # Should describe the structured error format
    assert "ok" in ERROR_HANDLING_PROTOCOL
    assert "false" in ERROR_HANDLING_PROTOCOL
    assert "error_type" in ERROR_HANDLING_PROTOCOL
    assert "user_message" in ERROR_HANDLING_PROTOCOL


def test_temporal_awareness_protocol_not_empty():
    """Test that TEMPORAL_AWARENESS_PROTOCOL contains meaningful content."""
    content = TEMPORAL_AWARENESS_PROTOCOL.replace("<temporal_awareness>", "").replace(
        "</temporal_awareness>", ""
    ).strip()

    assert len(content) > 100


def test_temporal_awareness_protocol_mentions_time_elements():
    """Test that TEMPORAL_AWARENESS_PROTOCOL mentions time-related elements."""
    # Should reference the time elements that get injected
    assert "current_time" in TEMPORAL_AWARENESS_PROTOCOL
    assert "connector_status" in TEMPORAL_AWARENESS_PROTOCOL
    assert "captured_at" in TEMPORAL_AWARENESS_PROTOCOL


def test_temporal_awareness_protocol_discusses_time_gaps():
    """Test that TEMPORAL_AWARENESS_PROTOCOL addresses conversation time gaps."""
    # Should mention that conversations can span time
    protocol_lower = TEMPORAL_AWARENESS_PROTOCOL.lower()
    assert any(word in protocol_lower for word in ["time", "gap", "span", "between messages"])


def test_protocols_are_static_strings():
    """Test that protocol constants are strings (not functions)."""
    assert isinstance(CONNECTOR_PROTOCOL, str)
    assert isinstance(CAPABILITY_PROTOCOL, str)
    assert isinstance(ERROR_HANDLING_PROTOCOL, str)
    assert isinstance(TEMPORAL_AWARENESS_PROTOCOL, str)


def test_get_connector_protocols_joins_with_newlines():
    """Test that get_connector_protocols separates protocols with newlines."""
    protocols = get_connector_protocols()

    # Should have each protocol separated by double newlines for readability
    assert "\n\n" in protocols

    # Count occurrences of each protocol block
    assert protocols.count("<connector_protocol>") == 1
    assert protocols.count("<capability_protocol>") == 1
    assert protocols.count("<error_handling>") == 1
    assert protocols.count("<temporal_awareness>") == 1


def test_protocols_order_in_combined_output():
    """Test that protocols appear in expected order in combined output."""
    protocols = get_connector_protocols()

    # Find positions of each protocol
    connector_pos = protocols.find("<connector_protocol>")
    capability_pos = protocols.find("<capability_protocol>")
    error_pos = protocols.find("<error_handling>")
    temporal_pos = protocols.find("<temporal_awareness>")

    # All should be present
    assert connector_pos != -1
    assert capability_pos != -1
    assert error_pos != -1
    assert temporal_pos != -1

    # Should be in the order: connector, capability, error, temporal
    assert connector_pos < capability_pos
    assert capability_pos < error_pos
    assert error_pos < temporal_pos


def test_protocols_no_placeholder_text():
    """Test that protocols don't contain TODO or placeholder text."""
    protocols = get_connector_protocols()

    # Should not contain development placeholders
    assert "TODO" not in protocols
    assert "FIXME" not in protocols
    assert "XXX" not in protocols
    assert "[insert" not in protocols.lower()
    assert "placeholder" not in protocols.lower()


def test_connector_protocol_complete_guidance():
    """Test that CONNECTOR_PROTOCOL provides complete guidance for each status."""
    # For each status, should explain what to do
    # Connected: should mention tools are callable
    assert "callable" in CONNECTOR_PROTOCOL.lower() or "use" in CONNECTOR_PROTOCOL.lower()

    # Not configured: should mention setup
    assert "setup" in CONNECTOR_PROTOCOL.lower() or "configure" in CONNECTOR_PROTOCOL.lower()

    # Invalid credentials: should mention reconnect
    assert "reconnect" in CONNECTOR_PROTOCOL.lower() or "issue" in CONNECTOR_PROTOCOL.lower()

    # Rate limited: should mention waiting
    assert "wait" in CONNECTOR_PROTOCOL.lower() or "temporary" in CONNECTOR_PROTOCOL.lower()


def test_error_handling_protocol_step_by_step():
    """Test that ERROR_HANDLING_PROTOCOL provides numbered steps."""
    # Should have numbered guidance (1., 2., 3., 4.)
    assert "1." in ERROR_HANDLING_PROTOCOL
    assert "2." in ERROR_HANDLING_PROTOCOL
    assert "3." in ERROR_HANDLING_PROTOCOL
    assert "4." in ERROR_HANDLING_PROTOCOL


@pytest.mark.parametrize(
    "protocol",
    [
        CONNECTOR_PROTOCOL,
        CAPABILITY_PROTOCOL,
        ERROR_HANDLING_PROTOCOL,
        TEMPORAL_AWARENESS_PROTOCOL,
    ],
)
def test_protocol_has_no_code_blocks(protocol):
    """Test that protocols don't contain code blocks (they're instructions, not code)."""
    # Should not contain code fence markers
    assert "```" not in protocol
    # Should not contain code indentation patterns (4+ spaces at start of line)
    lines = protocol.split("\n")
    for line in lines:
        if line.strip():  # Ignore empty lines
            # Allow some indentation for formatting, but not deep code-style indentation
            leading_spaces = len(line) - len(line.lstrip())
            assert leading_spaces < 8, f"Line has code-style indentation: {line[:50]}"


def test_protocols_use_consistent_terminology():
    """Test that protocols use consistent terminology across all sections."""
    protocols = get_connector_protocols()

    # Should consistently use "connector" (not "integration" or "plugin")
    connector_count = protocols.lower().count("connector")
    assert connector_count > 5  # Should appear multiple times

    # Should use "status" consistently
    assert "status" in protocols.lower()

    # Should refer to "tools" for callable functions
    assert "tool" in protocols.lower()


def test_capability_protocol_example_well_formed():
    """Test that the example in CAPABILITY_PROTOCOL looks realistic."""
    # Should contain realistic connector names in example
    example_connectors = ["github", "slack", "notion", "jira"]
    matches = sum(1 for conn in example_connectors if conn in CAPABILITY_PROTOCOL.lower())
    assert matches >= 2, "Example should mention at least 2 realistic connectors"


def test_protocols_line_length_reasonable():
    """Test that protocol lines are reasonably short for readability."""
    protocols = get_connector_protocols()
    lines = protocols.split("\n")

    # Most lines should be under 100 characters (allow some longer ones)
    long_lines = [line for line in lines if len(line) > 120]
    assert len(long_lines) < len(lines) * 0.1, "Too many lines exceed reasonable length"
