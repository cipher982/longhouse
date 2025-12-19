"""Unit tests for prompt composition functions.

Tests the prompt composer module that builds complete system prompts
from base templates and user context.
"""

import pytest

from zerg.prompts.composer import (
    format_user_context,
    format_servers,
    format_server_names,
    format_integrations,
    build_supervisor_prompt,
    build_worker_prompt,
    build_jarvis_prompt,
)


# ---------------------------------------------------------------------------
# Mock User object for tests
# ---------------------------------------------------------------------------


class MockUser:
    """Mock User object for testing prompt composition."""

    def __init__(self, context=None):
        self.context = context or {}


# ---------------------------------------------------------------------------
# Test format_user_context
# ---------------------------------------------------------------------------


class TestFormatUserContext:
    """Test user context formatting."""

    def test_format_user_context_full_context(self):
        """Test formatting with all fields present."""
        ctx = {
            "display_name": "Alice Smith",
            "role": "Senior Engineer",
            "location": "San Francisco",
            "description": "Full-stack developer focused on AI systems",
            "custom_instructions": "Always prefer TypeScript over JavaScript",
        }

        result = format_user_context(ctx)

        assert "Alice Smith" in result
        assert "Senior Engineer" in result
        assert "San Francisco" in result
        assert "Full-stack developer" in result
        assert "User preferences:" in result
        assert "TypeScript over JavaScript" in result

    def test_format_user_context_minimal(self):
        """Test formatting with only display_name."""
        ctx = {"display_name": "Bob"}

        result = format_user_context(ctx)

        assert "Bob" in result
        assert "user" in result  # default role
        assert "User preferences:" not in result

    def test_format_user_context_empty(self):
        """Test formatting with empty dict returns default message."""
        result = format_user_context({})

        assert "(No user context configured)" in result

    def test_format_user_context_with_custom_instructions(self):
        """Test that custom instructions are included in preferences section."""
        ctx = {
            "display_name": "Charlie",
            "custom_instructions": "Prefer concise explanations. Avoid verbose output.",
        }

        result = format_user_context(ctx)

        assert "Charlie" in result
        assert "User preferences:" in result
        assert "concise explanations" in result

    def test_format_user_context_with_location_no_role(self):
        """Test that location is included when present."""
        ctx = {"display_name": "Dana", "location": "New York"}

        result = format_user_context(ctx)

        assert "Dana" in result
        assert "New York" in result

    def test_format_user_context_none_values(self):
        """Test that None values are handled gracefully."""
        ctx = {
            "display_name": "Eve",
            "role": None,
            "location": None,
            "description": None,
        }

        result = format_user_context(ctx)

        assert "Eve" in result
        # Should not crash, should use defaults


# ---------------------------------------------------------------------------
# Test format_servers
# ---------------------------------------------------------------------------


class TestFormatServers:
    """Test server list formatting."""

    def test_format_servers_multiple(self):
        """Test formatting with multiple servers with all fields."""
        servers = [
            {
                "name": "clifford",
                "ip": "REDACTED_IP",
                "purpose": "Production VPS",
                "platform": "Ubuntu + Coolify",
                "notes": "Hosts 90% of web apps",
            },
            {
                "name": "cube",
                "ip": "100.70.237.79",
                "purpose": "Home GPU server",
                "platform": "Ubuntu",
                "notes": "AI/ML workloads",
            },
            {
                "name": "zerg",
                "ip": "5.161.92.127",
                "purpose": "Project server",
                "platform": "Ubuntu",
            },
        ]

        result = format_servers(servers)

        # Check all server names are present
        assert "clifford" in result
        assert "cube" in result
        assert "zerg" in result

        # Check IPs are present
        assert "REDACTED_IP" in result
        assert "100.70.237.79" in result

        # Check purposes
        assert "Production VPS" in result
        assert "Home GPU server" in result

        # Check notes appear on separate line
        assert "Notes: Hosts 90%" in result
        assert "Notes: AI/ML" in result

    def test_format_servers_minimal_fields(self):
        """Test formatting with only required fields (name and purpose)."""
        servers = [
            {"name": "server1", "purpose": "Testing"},
            {"name": "server2", "purpose": "Development"},
        ]

        result = format_servers(servers)

        assert "server1" in result
        assert "server2" in result
        assert "Testing" in result
        assert "Development" in result

    def test_format_servers_empty(self):
        """Test formatting with empty list returns default message."""
        result = format_servers([])

        assert "(No servers configured)" in result

    def test_format_servers_with_notes(self):
        """Test that notes appear on separate line."""
        servers = [
            {
                "name": "test-server",
                "ip": "10.0.0.1",
                "purpose": "Testing",
                "notes": "This is a detailed note about the server",
            }
        ]

        result = format_servers(servers)

        assert "test-server" in result
        assert "Notes:" in result
        assert "detailed note" in result

    def test_format_servers_missing_name(self):
        """Test that missing name defaults to 'unknown'."""
        servers = [{"ip": "10.0.0.1", "purpose": "No name server"}]

        result = format_servers(servers)

        assert "unknown" in result
        assert "10.0.0.1" in result


# ---------------------------------------------------------------------------
# Test format_server_names
# ---------------------------------------------------------------------------


class TestFormatServerNames:
    """Test server name list formatting."""

    def test_format_server_names_multiple(self):
        """Test formatting returns comma-separated list."""
        servers = [
            {"name": "clifford", "ip": "REDACTED_IP"},
            {"name": "cube", "ip": "100.70.237.79"},
            {"name": "zerg", "ip": "5.161.92.127"},
        ]

        result = format_server_names(servers)

        assert result == "clifford, cube, zerg"

    def test_format_server_names_single(self):
        """Test formatting with single server."""
        servers = [{"name": "solo-server"}]

        result = format_server_names(servers)

        assert result == "solo-server"

    def test_format_server_names_empty(self):
        """Test formatting with empty list returns default message."""
        result = format_server_names([])

        assert result == "no servers configured"

    def test_format_server_names_missing_names(self):
        """Test that missing names default to 'unknown'."""
        servers = [{"ip": "10.0.0.1"}, {"ip": "10.0.0.2"}]

        result = format_server_names(servers)

        assert result == "unknown, unknown"


# ---------------------------------------------------------------------------
# Test format_integrations
# ---------------------------------------------------------------------------


class TestFormatIntegrations:
    """Test integrations formatting."""

    def test_format_integrations_multiple(self):
        """Test formatting with multiple integrations."""
        integrations = {
            "notes": "Obsidian vault for knowledge management",
            "calendar": "Google Calendar for scheduling",
            "email": "Gmail for notifications",
        }

        result = format_integrations(integrations)

        assert "notes" in result
        assert "Obsidian vault" in result
        assert "calendar" in result
        assert "Google Calendar" in result
        assert "email" in result
        assert "Gmail" in result

    def test_format_integrations_single(self):
        """Test formatting with single integration."""
        integrations = {"notes": "Obsidian"}

        result = format_integrations(integrations)

        assert "notes" in result
        assert "Obsidian" in result

    def test_format_integrations_empty(self):
        """Test formatting with empty dict returns default message."""
        result = format_integrations({})

        assert "(No integrations configured)" in result


# ---------------------------------------------------------------------------
# Test build_supervisor_prompt
# ---------------------------------------------------------------------------


class TestBuildSupervisorPrompt:
    """Test complete supervisor prompt building."""

    def test_build_supervisor_prompt_with_full_context(self):
        """Test building prompt with all sections populated."""
        user = MockUser(
            context={
                "display_name": "Alice",
                "role": "engineer",
                "servers": [
                    {"name": "clifford", "ip": "REDACTED_IP", "purpose": "Production"},
                    {"name": "cube", "ip": "100.70.237.79", "purpose": "GPU workloads"},
                ],
                "integrations": {
                    "notes": "Obsidian",
                    "calendar": "Google Calendar",
                },
                "custom_instructions": "Keep responses concise",
            }
        )

        prompt = build_supervisor_prompt(user)

        # Check user context is injected
        assert "Alice" in prompt
        assert "engineer" in prompt
        assert "Keep responses concise" in prompt

        # Check servers are injected
        assert "clifford" in prompt
        assert "REDACTED_IP" in prompt
        assert "cube" in prompt

        # Check integrations are injected
        assert "Obsidian" in prompt
        assert "Google Calendar" in prompt

        # Check base template content is preserved
        assert "Supervisor" in prompt
        assert "spawn_worker" in prompt
        assert "Your Role" in prompt

    def test_build_supervisor_prompt_empty_context(self):
        """Test building prompt with empty context uses defaults."""
        user = MockUser(context={})

        prompt = build_supervisor_prompt(user)

        # Should include default messages
        assert "(No user context configured)" in prompt
        assert "(No servers configured)" in prompt
        assert "(No integrations configured)" in prompt

        # Base template should still be present
        assert "Supervisor" in prompt
        assert "spawn_worker" in prompt

    def test_build_supervisor_prompt_contains_required_sections(self):
        """Test that prompt contains all required sections."""
        user = MockUser(context={"display_name": "Bob"})

        prompt = build_supervisor_prompt(user)

        # Check for key sections from base template
        assert "Your Role" in prompt
        assert "Worker Lifecycle" in prompt or "When to Spawn Workers" in prompt
        assert "Querying Past Work" in prompt
        assert "Response Style" in prompt
        assert "Error Handling" in prompt

    def test_build_supervisor_prompt_none_context(self):
        """Test that None context is handled gracefully."""
        user = MockUser(context=None)

        prompt = build_supervisor_prompt(user)

        # Should use defaults for all sections
        assert "(No user context configured)" in prompt
        assert "Supervisor" in prompt


# ---------------------------------------------------------------------------
# Test build_worker_prompt
# ---------------------------------------------------------------------------


class TestBuildWorkerPrompt:
    """Test complete worker prompt building."""

    def test_build_worker_prompt_with_servers(self):
        """Test building prompt with servers configured."""
        user = MockUser(
            context={
                "display_name": "Charlie",
                "servers": [
                    {"name": "clifford", "ip": "REDACTED_IP", "purpose": "Production"},
                    {"name": "zerg", "ip": "5.161.92.127", "purpose": "Projects"},
                ],
            }
        )

        prompt = build_worker_prompt(user)

        # Check servers appear in prompt
        assert "clifford" in prompt
        assert "REDACTED_IP" in prompt
        assert "zerg" in prompt

        # Check user context
        assert "Charlie" in prompt

        # Check base template content
        assert "Worker agent" in prompt
        assert "ssh_exec" in prompt or "SSH" in prompt

    def test_build_worker_prompt_empty_context(self):
        """Test building prompt with empty context uses defaults."""
        user = MockUser(context={})

        prompt = build_worker_prompt(user)

        # Should include default messages
        assert "(No servers configured)" in prompt
        assert "(No user context configured)" in prompt

        # Base template should still be present
        assert "Worker" in prompt

    def test_build_worker_prompt_contains_commands_section(self):
        """Test that worker prompt includes useful commands section."""
        user = MockUser(context={})

        prompt = build_worker_prompt(user)

        # Check for command guidance
        assert "df -h" in prompt or "disk" in prompt.lower()
        assert "docker" in prompt.lower()


# ---------------------------------------------------------------------------
# Test build_jarvis_prompt
# ---------------------------------------------------------------------------


class TestBuildJarvisPrompt:
    """Test complete Jarvis prompt building."""

    def test_build_jarvis_prompt_with_tools(self):
        """Test building prompt with tools configured."""
        user = MockUser(
            context={
                "display_name": "Dana",
                "servers": [{"name": "clifford", "purpose": "Production"}],
            }
        )

        # v2.1: route_to_supervisor removed - use other tools
        enabled_tools = [
            {"name": "get_current_location", "description": "Get current GPS location"},
            {"name": "get_current_time", "description": "Get the current time"},
        ]

        prompt = build_jarvis_prompt(user, enabled_tools)

        # Check user context
        assert "Dana" in prompt

        # Check tools appear in prompt (v2.1: route_to_supervisor removed)
        assert "get_current_location" in prompt
        assert "get_current_time" in prompt

        # Check server names appear
        assert "clifford" in prompt

        # Check base template
        assert "Jarvis" in prompt

    def test_build_jarvis_prompt_no_tools(self):
        """Test building prompt with no tools shows default message."""
        user = MockUser(context={})

        prompt = build_jarvis_prompt(user, [])

        # Should show no tools message
        assert "(No direct tools currently enabled)" in prompt

        # Base template should be present
        assert "Jarvis" in prompt

    def test_build_jarvis_prompt_limitations(self):
        """Test that limitations are shown for missing tools."""
        user = MockUser(context={})

        # Only provide one tool, so limitations should be shown
        enabled_tools = [{"name": "get_current_time", "description": "Get time"}]

        prompt = build_jarvis_prompt(user, enabled_tools)

        # Should show limitations for missing calendar and smart_home
        assert "Calendar/reminders" in prompt or "calendar" in prompt.lower()
        assert "Smart home" in prompt or "smart_home" in prompt.lower()

    def test_build_jarvis_prompt_no_limitations_when_all_present(self):
        """Test that no limitations when all expected tools present."""
        user = MockUser(context={})

        enabled_tools = [
            {"name": "calendar", "description": "Calendar access"},
            {"name": "smart_home", "description": "Smart home control"},
            {"name": "get_current_time", "description": "Get time"},
        ]

        prompt = build_jarvis_prompt(user, enabled_tools)

        # Check that "None currently" appears in limitations section
        # or that limitation section mentions no limitations
        assert "None currently" in prompt or "(No " in prompt
