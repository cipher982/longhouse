"""Integration tests for user context system.

Tests the end-to-end flow of user context from API updates through
to prompt composition in agents.
"""

import pytest

from zerg.prompts.composer import build_supervisor_prompt
from zerg.prompts.composer import build_worker_prompt

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def test_user_context():
    """Realistic test context data."""
    return {
        "display_name": "Test User",
        "role": "developer",
        "location": "San Francisco",
        "servers": [
            {
                "name": "clifford",
                "ip": "REDACTED_IP",
                "purpose": "Production VPS",
                "platform": "Ubuntu + Coolify",
            },
            {
                "name": "cube",
                "ip": "100.70.237.79",
                "purpose": "Home GPU server",
                "platform": "Ubuntu",
            },
        ],
        "integrations": {
            "notes": "Obsidian vault",
            "calendar": "Google Calendar",
        },
        "custom_instructions": "Prefer TypeScript. Keep responses concise.",
    }


# ---------------------------------------------------------------------------
# Supervisor prompt integration
# ---------------------------------------------------------------------------


class TestSupervisorPromptIntegration:
    """Test that supervisor prompts include user context correctly."""

    def test_supervisor_prompt_includes_user_servers(self, db_session, test_user, test_user_context):
        """Test that supervisor prompt includes user's servers."""
        # Set user context with servers
        test_user.context = test_user_context
        db_session.commit()
        db_session.refresh(test_user)

        # Build supervisor prompt
        prompt = build_supervisor_prompt(test_user)

        # Verify servers appear in prompt
        assert "clifford" in prompt
        assert "REDACTED_IP" in prompt
        assert "cube" in prompt
        assert "100.70.237.79" in prompt
        assert "Production VPS" in prompt
        assert "Home GPU server" in prompt

    def test_supervisor_prompt_includes_user_info(self, db_session, test_user, test_user_context):
        """Test that supervisor prompt includes user's personal info."""
        test_user.context = test_user_context
        db_session.commit()
        db_session.refresh(test_user)

        prompt = build_supervisor_prompt(test_user)

        # Verify user info appears
        assert "Test User" in prompt
        assert "developer" in prompt
        assert "San Francisco" in prompt
        assert "Prefer TypeScript" in prompt

    def test_supervisor_prompt_includes_integrations(self, db_session, test_user, test_user_context):
        """Test that supervisor prompt includes user's integrations."""
        test_user.context = test_user_context
        db_session.commit()
        db_session.refresh(test_user)

        prompt = build_supervisor_prompt(test_user)

        # Verify integrations appear
        assert "Obsidian" in prompt
        assert "Google Calendar" in prompt

    def test_supervisor_agent_uses_user_context(self, db_session, test_user, test_user_context):
        """Test that created supervisor agent has user context in system_instructions."""
        # Set user context
        test_user.context = test_user_context
        db_session.commit()

        # Create supervisor agent using the service
        from zerg.services.supervisor_service import SupervisorService

        service = SupervisorService(db_session)
        supervisor = service.get_or_create_supervisor_agent(test_user.id)

        # Verify agent was created
        assert supervisor is not None
        assert supervisor.name == "Supervisor"

        # Verify system instructions include user context
        assert "clifford" in supervisor.system_instructions
        assert "Test User" in supervisor.system_instructions
        assert "Obsidian" in supervisor.system_instructions

    def test_supervisor_prompt_updates_with_context_changes(self, db_session, test_user):
        """Test that prompt changes when context is updated."""
        # Initial context
        test_user.context = {"display_name": "Alice", "servers": []}
        db_session.commit()

        prompt1 = build_supervisor_prompt(test_user)
        assert "Alice" in prompt1
        assert "(No servers configured)" in prompt1

        # Update context
        test_user.context = {
            "display_name": "Alice",
            "servers": [{"name": "new-server", "ip": "10.0.0.1"}],
        }
        db_session.commit()
        db_session.refresh(test_user)

        prompt2 = build_supervisor_prompt(test_user)
        assert "Alice" in prompt2
        assert "new-server" in prompt2
        assert "(No servers configured)" not in prompt2

        # Prompts should be different
        assert prompt1 != prompt2


# ---------------------------------------------------------------------------
# Worker prompt integration
# ---------------------------------------------------------------------------


class TestWorkerPromptIntegration:
    """Test that worker prompts include user context correctly."""

    def test_worker_prompt_includes_user_servers(self, db_session, test_user, test_user_context):
        """Test that worker prompt includes user's servers."""
        test_user.context = test_user_context
        db_session.commit()
        db_session.refresh(test_user)

        prompt = build_worker_prompt(test_user)

        # Verify servers appear in prompt
        assert "clifford" in prompt
        assert "REDACTED_IP" in prompt
        assert "cube" in prompt

    def test_worker_prompt_includes_user_context(self, db_session, test_user, test_user_context):
        """Test that worker prompt includes user context."""
        test_user.context = test_user_context
        db_session.commit()
        db_session.refresh(test_user)

        prompt = build_worker_prompt(test_user)

        # Verify user info appears
        assert "Test User" in prompt
        assert "developer" in prompt

    @pytest.mark.asyncio
    async def test_worker_runner_uses_user_context(self, db_session, test_user, test_user_context):
        """Test that worker runner includes user context in worker prompt."""

        # Set user context
        test_user.context = test_user_context
        db_session.commit()

        # Create a worker config and verify prompt building
        # This tests that the worker system would get user context without actually running
        worker_prompt = build_worker_prompt(test_user)

        # Verify user context appears in worker prompt
        assert "clifford" in worker_prompt
        assert "REDACTED_IP" in worker_prompt
        assert "Test User" in worker_prompt
        assert "developer" in worker_prompt

        # Verify base worker template is present
        assert "Worker agent" in worker_prompt
        assert "SSH" in worker_prompt or "ssh" in worker_prompt.lower()


# ---------------------------------------------------------------------------
# End-to-end flow tests
# ---------------------------------------------------------------------------


class TestEndToEndContextFlow:
    """Test complete flow from API update to agent execution."""

    def test_update_context_affects_new_prompts(self, client, db_session, test_user):
        """Test that updating context via API affects new agent prompts."""
        # Initial context (empty)
        initial_prompt = build_supervisor_prompt(test_user)
        assert "(No servers configured)" in initial_prompt

        # Update context via API
        context_update = {
            "context": {
                "display_name": "Bob",
                "servers": [{"name": "test-server", "ip": "10.0.0.1", "purpose": "Testing"}],
            }
        }
        response = client.put("/api/users/me/context", json=context_update)
        assert response.status_code == 200

        # Refresh user from DB
        db_session.refresh(test_user)

        # Build new prompt
        new_prompt = build_supervisor_prompt(test_user)

        # Verify context appears in new prompt
        assert "Bob" in new_prompt
        assert "test-server" in new_prompt
        assert "10.0.0.1" in new_prompt
        assert "(No servers configured)" not in new_prompt

        # Prompts should be different
        assert initial_prompt != new_prompt

    def test_context_persists_across_agent_creation(self, client, db_session, test_user):
        """Test that context persists when creating multiple agents."""
        # Set context via API
        context_data = {
            "context": {
                "display_name": "Charlie",
                "servers": [{"name": "server1", "ip": "10.0.0.1"}],
            }
        }
        client.put("/api/users/me/context", json=context_data)

        # Refresh user
        db_session.refresh(test_user)

        # Build supervisor prompt with current context
        prompt1 = build_supervisor_prompt(test_user)
        assert "Charlie" in prompt1
        assert "server1" in prompt1

        # Update context
        context_data2 = {
            "context": {
                "display_name": "Charlie",
                "servers": [{"name": "server2", "ip": "10.0.0.2"}],
            }
        }
        client.put("/api/users/me/context", json=context_data2)
        db_session.refresh(test_user)

        # Build another prompt with updated context
        prompt2 = build_supervisor_prompt(test_user)
        assert "Charlie" in prompt2
        assert "server2" in prompt2

        # Prompts should be different due to different servers
        assert prompt1 != prompt2

    def test_patch_accumulates_context_for_agents(self, client, db_session, test_user):
        """Test that PATCH accumulates context that appears in agents."""
        # First patch - add user info
        client.patch("/api/users/me/context", json={"context": {"display_name": "Dana"}})
        db_session.refresh(test_user)

        prompt1 = build_supervisor_prompt(test_user)
        assert "Dana" in prompt1

        # Second patch - add servers
        client.patch(
            "/api/users/me/context",
            json={"context": {"servers": [{"name": "server1", "ip": "10.0.0.1"}]}},
        )
        db_session.refresh(test_user)

        prompt2 = build_supervisor_prompt(test_user)
        assert "Dana" in prompt2  # from first patch
        assert "server1" in prompt2  # from second patch

        # Third patch - add integrations
        client.patch("/api/users/me/context", json={"context": {"integrations": {"notes": "Obsidian"}}})
        db_session.refresh(test_user)

        prompt3 = build_supervisor_prompt(test_user)
        assert "Dana" in prompt3
        assert "server1" in prompt3
        assert "Obsidian" in prompt3


# ---------------------------------------------------------------------------
# Context isolation tests
# ---------------------------------------------------------------------------


class TestContextIsolation:
    """Test that user contexts are properly isolated."""

    def test_different_users_have_different_contexts(self, db_session, test_user, other_user):
        """Test that different users have independent contexts."""
        # Set different contexts for each user
        test_user.context = {
            "display_name": "User One",
            "servers": [{"name": "user1-server"}],
        }
        other_user.context = {
            "display_name": "User Two",
            "servers": [{"name": "user2-server"}],
        }
        db_session.commit()

        # Build prompts for each user
        prompt1 = build_supervisor_prompt(test_user)
        prompt2 = build_supervisor_prompt(other_user)

        # Each prompt should contain only their own context
        assert "User One" in prompt1
        assert "user1-server" in prompt1
        assert "User Two" not in prompt1
        assert "user2-server" not in prompt1

        assert "User Two" in prompt2
        assert "user2-server" in prompt2
        assert "User One" not in prompt2
        assert "user1-server" not in prompt2

    def test_agents_inherit_owner_context(self, db_session, test_user, other_user):
        """Test that agents get context from their owner, not other users."""
        from zerg.services.supervisor_service import SupervisorService

        # Set contexts
        test_user.context = {"display_name": "Owner One", "servers": [{"name": "server-a"}]}
        other_user.context = {"display_name": "Owner Two", "servers": [{"name": "server-b"}]}
        db_session.commit()

        # Create agents for each user
        service = SupervisorService(db_session)
        agent1 = service.get_or_create_supervisor_agent(test_user.id)
        agent2 = service.get_or_create_supervisor_agent(other_user.id)

        # Each agent should have only their owner's context
        assert "Owner One" in agent1.system_instructions
        assert "server-a" in agent1.system_instructions
        assert "Owner Two" not in agent1.system_instructions

        assert "Owner Two" in agent2.system_instructions
        assert "server-b" in agent2.system_instructions
        assert "Owner One" not in agent2.system_instructions


# ---------------------------------------------------------------------------
# Performance and edge case tests
# ---------------------------------------------------------------------------


class TestContextEdgeCases:
    """Test edge cases and performance considerations."""

    def test_large_context_under_limit(self, client, db_session, test_user):
        """Test that large but valid context works."""
        # Create context near 64KB limit
        large_servers = [
            {"name": f"server-{i}", "ip": f"10.0.{i//255}.{i%255}", "purpose": f"Purpose {i}"} for i in range(100)
        ]

        context = {"context": {"servers": large_servers, "display_name": "Large Context User"}}

        response = client.put("/api/users/me/context", json=context)
        assert response.status_code == 200

        # Verify it can be retrieved
        db_session.refresh(test_user)
        prompt = build_supervisor_prompt(test_user)
        assert "Large Context User" in prompt
        assert "server-0" in prompt

    def test_empty_context_produces_valid_prompt(self, db_session, test_user):
        """Test that empty context still produces valid prompt."""
        test_user.context = {}
        db_session.commit()

        prompt = build_supervisor_prompt(test_user)

        # Should have default messages
        assert "(No user context configured)" in prompt
        assert "(No servers configured)" in prompt

        # Should still have base template
        assert "Supervisor" in prompt
        assert "spawn_worker" in prompt

    def test_none_context_produces_valid_prompt(self, db_session, test_user):
        """Test that None context is handled gracefully."""
        test_user.context = None
        db_session.commit()

        prompt = build_supervisor_prompt(test_user)

        # Should use defaults
        assert "(No user context configured)" in prompt
        assert "Supervisor" in prompt

    def test_context_with_special_characters(self, client, db_session, test_user):
        """Test that special characters in context are handled."""
        context = {
            "context": {
                "display_name": "Test <User>",
                "servers": [
                    {
                        "name": "server & more",
                        "purpose": 'Test "quoted" purpose',
                        "notes": "Line 1\nLine 2",
                    }
                ],
            }
        }

        response = client.put("/api/users/me/context", json=context)
        assert response.status_code == 200

        db_session.refresh(test_user)
        prompt = build_supervisor_prompt(test_user)

        # Characters should be preserved
        assert "Test <User>" in prompt
        assert "server & more" in prompt
        assert 'Test "quoted" purpose' in prompt or "Test &quot;quoted&quot; purpose" in prompt
