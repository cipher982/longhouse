"""MCP server management routes."""

import logging
from typing import Any
from typing import Dict
from typing import List

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from zerg.crud import crud
from zerg.database import get_db
from zerg.dependencies.auth import get_current_user
from zerg.schemas.schemas import Agent

# MCP manager singleton – needed by several endpoints
from zerg.tools.mcp_adapter import MCPManager  # noqa: E402 – placed after stdlib imports
from zerg.tools.mcp_exceptions import MCPAuthenticationError
from zerg.tools.mcp_exceptions import MCPConfigurationError
from zerg.tools.mcp_exceptions import MCPConnectionError
from zerg.tools.mcp_presets import PRESET_MCP_SERVERS
from zerg.tools.unified_access import get_tool_resolver
from zerg.utils import crypto
from zerg.utils.json_helpers import set_json_field

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/agents/{agent_id}/mcp-servers",
    tags=["mcp-servers"],
    dependencies=[Depends(get_current_user)],
)


# Pydantic models for request/response
class MCPServerAddRequest(BaseModel):
    """Request model for adding an MCP server."""

    # For preset servers
    preset: str = Field(None, description="Name of a preset MCP server (e.g., 'github', 'linear')")

    # For custom servers
    url: str = Field(None, description="URL of the custom MCP server")
    name: str = Field(None, description="Name for the custom MCP server")

    # Common fields
    auth_token: str = Field(None, description="Authentication token for the MCP server")
    allowed_tools: List[str] = Field(None, description="List of allowed tools (None means all)")

    # Custom validation
    def model_post_init(self, __context: Any) -> None:
        """Validate that either preset or (url, name) is provided."""
        if self.preset and (self.url or self.name):
            raise ValueError("Cannot specify both 'preset' and custom server fields")
        if not self.preset and not (self.url and self.name):
            raise ValueError("Must specify either 'preset' or both 'url' and 'name'")


class MCPServerResponse(BaseModel):
    """Response model for MCP server info."""

    name: str
    url: str
    tools: List[str]
    status: str = "online"  # online, offline, error
    error: str = Field(None, description="Error message if status is 'error'")


class MCPTestConnectionResponse(BaseModel):
    """Response model for testing MCP server connection."""

    success: bool
    message: str
    tools: List[str] = Field(default_factory=list)


# Helper functions
def _get_mcp_servers_from_config(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract MCP server configurations from agent config."""
    if not config:
        return []
    return config.get("mcp_servers", [])


def _update_mcp_servers_in_config(config: Dict[str, Any], mcp_servers: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Update MCP server configurations in agent config."""
    if not config:
        config = {}
    config["mcp_servers"] = mcp_servers
    return config


# API endpoints
@router.get("/", response_model=List[MCPServerResponse])
async def list_mcp_servers(
    agent_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """List all MCP servers configured for an agent."""
    # Get agent and check permissions
    agent = crud.get_agent(db, agent_id=agent_id)
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    if agent.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to view this agent")

    # Get MCP servers from config
    mcp_servers = _get_mcp_servers_from_config(agent.config)

    # Build response with tool information
    response = []
    resolver = get_tool_resolver()

    for server_config in mcp_servers:
        if "preset" in server_config:
            preset_name = server_config["preset"]
            if preset_name in PRESET_MCP_SERVERS:
                preset = PRESET_MCP_SERVERS[preset_name]
                name = preset.name
                url = preset.url
            else:
                # Unknown preset – still include it in the list so the UI
                # can show *offline* status and allow the user to troubleshoot
                # or remove the entry.
                name = preset_name
                url = "unknown"
        else:
            name = server_config.get("name", "unknown")
            url = server_config.get("url", "unknown")

        # Get tools for this server
        tool_prefix = f"mcp_{name}_"
        tools = [tool.name for tool in resolver.get_all_tools() if tool.name.startswith(tool_prefix)]

        response.append(
            MCPServerResponse(
                name=name,
                url=url,
                tools=tools,
                status="online" if tools else "offline",
            )
        )

    # ------------------------------------------------------------------
    # Edge-case: In test environments where the database JSON column does
    # not properly reflect in-process mutations across separate requests the
    # ``mcp_servers`` list may contain fewer entries than actually registered
    # via :pyclass:`zerg.tools.mcp_adapter.MCPManager`.  We therefore merge
    # the adapters held by the singleton manager to ensure the API returns
    # a *complete* view that matches user expectations and the test-suite
    # assertions.
    # ------------------------------------------------------------------

    _ = MCPManager()
    # NOTE: We deliberately skip adapters that are **not** present in the stored
    # configuration so the API reflects exactly what is persisted on the Agent
    # row.  This avoids stale entries after a server is removed within the same
    # request cycle (test_remove_mcp_server regression).

    return response


@router.post("/", response_model=Agent, status_code=status.HTTP_201_CREATED)
async def add_mcp_server(
    agent_id: int,
    request: MCPServerAddRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Add an MCP server to an agent."""
    # Get agent and check permissions
    agent = crud.get_agent(db, agent_id=agent_id)
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    if agent.owner_id != current_user.id and current_user.role != "ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to modify this agent")

    # Build MCP server config
    if request.preset:
        server_config: Dict[str, Any] = {
            "preset": request.preset,
        }
        # Encrypt auth token if provided
        if request.auth_token:
            server_config["auth_token"] = crypto.encrypt(request.auth_token)
    else:
        # Validate HTTPS URL for security
        if not request.url.startswith("https://"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="MCP server URL must use HTTPS for security")

        server_config = {
            "url": request.url,
            "name": request.name,
        }
        # Encrypt auth token if provided
        if request.auth_token:
            server_config["auth_token"] = crypto.encrypt(request.auth_token)

    if request.allowed_tools:
        server_config["allowed_tools"] = request.allowed_tools

    # Add to agent config
    current_config = agent.config or {}
    mcp_servers = _get_mcp_servers_from_config(current_config)

    # Check for duplicates
    for existing in mcp_servers:
        if request.preset and existing.get("preset") == request.preset:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Preset '{request.preset}' is already configured for this agent",
            )
        elif not request.preset and existing.get("url") == request.url:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Server URL '{request.url}' is already configured for this agent",
            )

    # Try to connect to the server
    try:
        manager = MCPManager()
        manager.add_server(server_config)
    except MCPAuthenticationError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))
    except MCPConnectionError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))
    except MCPConfigurationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.exception("Failed to add MCP server")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

    # Update agent config
    mcp_servers.append(server_config)
    updated_config = _update_mcp_servers_in_config(current_config, mcp_servers)

    # Save to database
    set_json_field(agent, "config", updated_config)
    db.commit()
    db.refresh(agent)
    return agent


@router.delete("/{server_name}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_mcp_server(
    agent_id: int,
    server_name: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Remove an MCP server from an agent."""
    # Get agent and check permissions
    agent = crud.get_agent(db, agent_id=agent_id)
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    if agent.owner_id != current_user.id and current_user.role != "ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to modify this agent")

    # Get MCP servers from config
    current_config = agent.config or {}
    mcp_servers = _get_mcp_servers_from_config(current_config)

    # Find and remove the server
    found = False
    updated_servers = []

    for server_config in mcp_servers:
        if "preset" in server_config and server_config["preset"] == server_name:
            found = True
            continue  # Skip this server
        elif server_config.get("name") == server_name:
            found = True
            continue  # Skip this server
        updated_servers.append(server_config)

    if not found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"MCP server '{server_name}' not found")

    # Update agent config
    set_json_field(agent, "config", {"mcp_servers": updated_servers})
    db.commit()

    return None


@router.post("/test", response_model=MCPTestConnectionResponse)
async def test_mcp_connection(
    agent_id: int,
    request: MCPServerAddRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Test connection to an MCP server without saving it."""
    # Get agent and check permissions (for context)
    agent = crud.get_agent(db, agent_id=agent_id)
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    if agent.owner_id != current_user.id and current_user.role != "ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to test servers for this agent")

    # Build MCP server config
    if request.preset:
        server_config: Dict[str, Any] = {
            "preset": request.preset,
            "auth_token": request.auth_token,
        }
    else:
        server_config = {
            "url": request.url,
            "name": request.name,
            "auth_token": request.auth_token,
        }

    if request.allowed_tools:
        server_config["allowed_tools"] = request.allowed_tools

    # Try to connect to the server
    try:
        manager = MCPManager()
        manager.add_server(server_config)

        # Get tools that were registered
        resolver = get_tool_resolver()
        if request.preset:
            preset = PRESET_MCP_SERVERS.get(request.preset)
            tool_prefix = f"mcp_{preset.name}_" if preset else f"mcp_{request.preset}_"
        else:
            tool_prefix = f"mcp_{request.name}_"

        tools = [tool.name for tool in resolver.get_all_tools() if tool.name.startswith(tool_prefix)]

        return MCPTestConnectionResponse(
            success=True,
            message="Connection successful",
            tools=tools,
        )
    except MCPAuthenticationError as e:
        return MCPTestConnectionResponse(
            success=False,
            message=f"Authentication failed: {str(e)}",
        )
    except MCPConnectionError as e:
        return MCPTestConnectionResponse(
            success=False,
            message=f"Connection failed: {str(e)}",
        )
    except MCPConfigurationError as e:
        return MCPTestConnectionResponse(
            success=False,
            message=f"Configuration error: {str(e)}",
        )
    except Exception as e:
        logger.exception("Failed to test MCP server connection")
        return MCPTestConnectionResponse(
            success=False,
            message=f"Unexpected error: {str(e)}",
        )


@router.get("/available-tools", response_model=Dict[str, Any])
async def get_available_tools(
    agent_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Get all available tools for an agent (built-in + MCP)."""
    # Get agent and check permissions
    agent = crud.get_agent(db, agent_id=agent_id)
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    if agent.owner_id != current_user.id and current_user.role != "ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to view this agent")

    # Get all tools from registry (built-in + MCP)
    resolver = get_tool_resolver()
    all_tools = resolver.get_all_tools()

    # Categorize tools
    builtin_tools = []
    mcp_tools = {}

    for tool in all_tools:
        if tool.name.startswith("mcp_"):
            # Extract server name from tool name (mcp_<server>_<tool>)
            parts = tool.name.split("_", 2)
            if len(parts) >= 3:
                server_name = parts[1]
                # Tool name (parts[2]) not needed - using full tool.name instead
                if server_name not in mcp_tools:
                    mcp_tools[server_name] = []
                mcp_tools[server_name].append(tool.name)
        else:
            builtin_tools.append(tool.name)

    return {
        "builtin": builtin_tools,
        "mcp": mcp_tools,
    }
