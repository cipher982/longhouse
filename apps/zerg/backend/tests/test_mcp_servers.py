"""Tests for MCP server management API endpoints."""

import pytest
from fastapi import status
from sqlalchemy.orm import Session

from tests.conftest import TEST_WORKER_MODEL
from zerg.crud import crud
from zerg.models.models import Agent
from zerg.models.models import User


@pytest.fixture
def test_agent(db: Session, test_user: User) -> Agent:
    """Create a test agent for MCP tests."""
    agent = crud.create_agent(
        db=db,
        owner_id=test_user.id,
        name="Test Agent for MCP",
        system_instructions="You are a test agent",
        task_instructions="Test MCP functionality",
        model=TEST_WORKER_MODEL,
        schedule=None,
        config=None,
    )
    return agent


class TestMCPServers:
    """Test MCP server management endpoints."""

    def test_list_mcp_servers_empty(self, client, auth_headers, test_agent):
        """Test listing MCP servers when none are configured."""
        response = client.get(
            f"/api/agents/{test_agent.id}/mcp-servers/",
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == []

    def test_add_preset_mcp_server(self, client, auth_headers, test_agent, db):
        """Test adding a preset MCP server."""
        response = client.post(
            f"/api/agents/{test_agent.id}/mcp-servers/",
            headers=auth_headers,
            json={
                "preset": "github",
                "auth_token": "ghp_test_token",
            },
        )
        assert response.status_code == status.HTTP_201_CREATED

        # Verify the agent's config was updated
        updated_agent = response.json()
        assert "config" in updated_agent
        assert "mcp_servers" in updated_agent["config"]
        assert len(updated_agent["config"]["mcp_servers"]) == 1
        assert updated_agent["config"]["mcp_servers"][0]["preset"] == "github"

        # Token should be encrypted
        encrypted_token = updated_agent["config"]["mcp_servers"][0]["auth_token"]
        assert encrypted_token != "ghp_test_token"

        from zerg.utils import crypto

        assert crypto.decrypt(encrypted_token) == "ghp_test_token"

    def test_add_custom_mcp_server(self, client, auth_headers, test_agent, db):
        """Test adding a custom MCP server."""
        response = client.post(
            f"/api/agents/{test_agent.id}/mcp-servers/",
            headers=auth_headers,
            json={
                "url": "https://custom.example.com/mcp",
                "name": "custom",
                "auth_token": "custom_token",
                "allowed_tools": ["tool1", "tool2"],
            },
        )
        assert response.status_code == status.HTTP_201_CREATED

        # Verify the agent's config was updated
        updated_agent = response.json()
        assert "config" in updated_agent
        assert "mcp_servers" in updated_agent["config"]
        assert len(updated_agent["config"]["mcp_servers"]) == 1

        server_config = updated_agent["config"]["mcp_servers"][0]
        assert server_config["url"] == "https://custom.example.com/mcp"
        assert server_config["name"] == "custom"

        # Token should be encrypted
        encrypted_token = server_config["auth_token"]
        assert encrypted_token != "custom_token"

        from zerg.utils import crypto

        assert crypto.decrypt(encrypted_token) == "custom_token"

        assert server_config["allowed_tools"] == ["tool1", "tool2"]

    def test_add_mcp_server_invalid_request(self, client, auth_headers, test_agent):
        """Test adding MCP server with invalid request (missing required fields)."""
        response = client.post(
            f"/api/agents/{test_agent.id}/mcp-servers/",
            headers=auth_headers,
            json={
                # Missing both preset and url/name
                "auth_token": "token",
            },
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_add_duplicate_preset(self, client, auth_headers, test_agent, db):
        """Test adding duplicate preset MCP server."""
        # Add first time
        response = client.post(
            f"/api/agents/{test_agent.id}/mcp-servers/",
            headers=auth_headers,
            json={
                "preset": "github",
                "auth_token": "ghp_test_token",
            },
        )
        assert response.status_code == status.HTTP_201_CREATED

        # Try to add again
        response = client.post(
            f"/api/agents/{test_agent.id}/mcp-servers/",
            headers=auth_headers,
            json={
                "preset": "github",
                "auth_token": "ghp_test_token2",
            },
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "already configured" in response.json()["detail"]

    def test_remove_mcp_server(self, client, auth_headers, test_agent, db):
        """Test removing an MCP server."""
        # First add a server
        response = client.post(
            f"/api/agents/{test_agent.id}/mcp-servers/",
            headers=auth_headers,
            json={
                "preset": "github",
                "auth_token": "ghp_test_token",
            },
        )
        assert response.status_code == status.HTTP_201_CREATED

        # Remove it
        response = client.delete(
            f"/api/agents/{test_agent.id}/mcp-servers/github",
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_204_NO_CONTENT

        # Verify it's gone
        response = client.get(
            f"/api/agents/{test_agent.id}/mcp-servers/",
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json() == []

    def test_remove_nonexistent_mcp_server(self, client, auth_headers, test_agent):
        """Test removing a non-existent MCP server."""
        response = client.delete(
            f"/api/agents/{test_agent.id}/mcp-servers/nonexistent",
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_test_mcp_connection(self, client, auth_headers, test_agent):
        """Test the connection test endpoint."""
        response = client.post(
            f"/api/agents/{test_agent.id}/mcp-servers/test",
            headers=auth_headers,
            json={
                "preset": "github",
                "auth_token": "ghp_test_token",
            },
        )
        assert response.status_code == status.HTTP_200_OK
        result = response.json()
        assert "success" in result
        assert "message" in result
        assert "tools" in result

    def test_get_available_tools(self, client, auth_headers, test_agent):
        """Test getting available tools for an agent."""
        response = client.get(
            f"/api/agents/{test_agent.id}/mcp-servers/available-tools",
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_200_OK
        result = response.json()
        assert "builtin" in result
        assert "mcp" in result
        assert isinstance(result["builtin"], list)
        assert isinstance(result["mcp"], dict)

    def test_unauthorized_access(self, unauthenticated_client, test_agent, monkeypatch):
        """Test accessing MCP endpoints without authentication."""
        # Temporarily enable auth for this test
        monkeypatch.setattr("zerg.dependencies.auth.AUTH_DISABLED", False)

        response = unauthenticated_client.get(f"/api/agents/{test_agent.id}/mcp-servers/")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

        # Restore the original value
        monkeypatch.setattr("zerg.dependencies.auth.AUTH_DISABLED", True)

    def test_access_other_users_agent(self, client, auth_headers, test_agent, db):
        """Test accessing another user's agent MCP servers."""
        # Create another user and their agent
        other_user = crud.create_user(
            db=db,
            email="other@example.com",
            provider="google",
            provider_user_id="other123",
        )
        other_agent = crud.create_agent(
            db=db,
            owner_id=other_user.id,
            name="Other Agent",
            system_instructions="Other agent",
            task_instructions="Other task",
            model=TEST_WORKER_MODEL,
            schedule=None,
            config=None,
        )

        # Try to access other user's agent
        response = client.get(
            f"/api/agents/{other_agent.id}/mcp-servers/",
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_list_mcp_servers_with_multiple_servers(self, client, auth_headers, test_agent, db):
        """Test listing multiple MCP servers."""
        # Add multiple servers
        servers = [
            {"preset": "github", "auth_token": "ghp_token"},
            {"preset": "linear", "auth_token": "lin_token"},
            {"url": "https://custom.com/mcp", "name": "custom", "auth_token": "custom_token"},
        ]

        for server in servers:
            response = client.post(
                f"/api/agents/{test_agent.id}/mcp-servers/",
                headers=auth_headers,
                json=server,
            )
            assert response.status_code == status.HTTP_201_CREATED

        # List all servers
        response = client.get(
            f"/api/agents/{test_agent.id}/mcp-servers/",
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_200_OK
        result = response.json()
        assert len(result) == 3

        # Verify each server is present
        server_names = [s["name"] for s in result]
        assert "github" in server_names
        assert "linear" in server_names
        assert "custom" in server_names
