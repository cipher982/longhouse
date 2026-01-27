"""MCP server management routes."""

import logging
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

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
from zerg.schemas.schemas import Fiche

# MCP manager singleton – needed by several endpoints
from zerg.tools.mcp_adapter import MCPManager  # noqa: E402 – placed after stdlib imports
from zerg.tools.mcp_exceptions import MCPAuthenticationError
from zerg.tools.mcp_exceptions import MCPConfigurationError
from zerg.tools.mcp_exceptions import MCPConnectionError
from zerg.tools.mcp_presets import PRESET_MCP_SERVERS
from zerg.tools.mcp_transport import MCPServerConfig
from zerg.tools.unified_access import get_tool_resolver
from zerg.utils import crypto
from zerg.utils.json_helpers import set_json_field

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/fiches/{fiche_id}/mcp-servers",
    tags=["mcp-servers"],
    dependencies=[Depends(get_current_user)],
)


# Pydantic models for request/response
class MCPServerAddRequest(BaseModel):
    """Request model for adding an MCP server."""

    # Transport type - determines which fields are required
    transport: str = Field("http", description="Transport type: 'http' (default) or 'stdio'")

    # For preset servers
    preset: str = Field(None, description="Name of a preset MCP server (e.g., 'github', 'linear')")

    # For custom HTTP servers
    url: str = Field(None, description="URL of the custom MCP server (http transport)")
    name: str = Field(None, description="Name for the MCP server")

    # For stdio servers
    command: str = Field(None, description="Command to spawn the MCP server (stdio transport)")
    env: Dict[str, str] = Field(None, description="Environment variables for stdio server")

    # Common fields
    auth_token: str = Field(None, description="Authentication token for the MCP server")
    allowed_tools: List[str] = Field(None, description="List of allowed tools (None means all)")

    # Custom validation
    def model_post_init(self, __context: Any) -> None:
        """Validate based on transport type."""
        if self.preset:
            # Preset mode - no other server fields needed
            if self.url or self.command:
                raise ValueError("Cannot specify both 'preset' and custom server fields")
            return

        if self.transport == "stdio":
            # Stdio transport requires command and name
            if not self.command or not self.name:
                raise ValueError("Stdio transport requires both 'command' and 'name'")
            if self.url:
                raise ValueError("Cannot specify 'url' for stdio transport")
        else:  # http transport
            # HTTP transport requires url and name
            if not self.url or not self.name:
                raise ValueError("HTTP transport requires both 'url' and 'name'")
            if self.command:
                raise ValueError("Cannot specify 'command' for http transport")


class MCPServerResponse(BaseModel):
    """Response model for MCP server info."""

    name: str
    transport: str = "http"  # http or stdio
    url: Optional[str] = Field(None, description="Server URL (http transport)")
    command: Optional[str] = Field(None, description="Server command (stdio transport)")
    tools: List[str]
    status: str = "online"  # online, offline, error
    error: Optional[str] = Field(None, description="Error message if status is 'error'")


class MCPTestConnectionResponse(BaseModel):
    """Response model for testing MCP server connection."""

    success: bool
    message: str
    tools: List[str] = Field(default_factory=list)


# Helper functions
def _get_mcp_servers_from_config(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract MCP server configurations from fiche config."""
    if not config:
        return []
    return config.get("mcp_servers", [])


def _update_mcp_servers_in_config(config: Dict[str, Any], mcp_servers: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Update MCP server configurations in fiche config."""
    if not config:
        config = {}
    config["mcp_servers"] = mcp_servers
    return config


# API endpoints
@router.get("/", response_model=List[MCPServerResponse])
async def list_mcp_servers(
    fiche_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """List all MCP servers configured for a fiche."""
    # Get fiche and check permissions
    fiche = crud.get_fiche(db, fiche_id=fiche_id)
    if not fiche:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fiche not found")

    if fiche.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to view this fiche")

    # Get MCP servers from config
    mcp_servers = _get_mcp_servers_from_config(fiche.config)

    # Build response with tool information
    response = []
    resolver = get_tool_resolver()

    for server_config in mcp_servers:
        if "preset" in server_config:
            preset_name = server_config["preset"]
            if preset_name in PRESET_MCP_SERVERS:
                preset = PRESET_MCP_SERVERS[preset_name]
                name = preset.name
                transport = getattr(preset, "transport", "http")
                url = getattr(preset, "url", None)
                command = getattr(preset, "command", None)
            else:
                # Unknown preset – still include it in the list so the UI
                # can show *offline* status and allow the user to troubleshoot
                # or remove the entry.
                name = preset_name
                transport = "http"
                url = "unknown"
                command = None
        else:
            name = server_config.get("name", "unknown")
            transport = server_config.get("transport", "http")
            if server_config.get("type") == "stdio" or transport == "stdio":
                transport = "stdio"
                url = None
                command = server_config.get("command", "unknown")
            else:
                url = server_config.get("url", "unknown")
                command = None

        # Get tools for this server
        tool_prefix = f"mcp_{name}_"
        tools = [tool.name for tool in resolver.get_all_tools() if tool.name.startswith(tool_prefix)]

        response.append(
            MCPServerResponse(
                name=name,
                transport=transport,
                url=url,
                command=command,
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
    # configuration so the API reflects exactly what is persisted on the Fiche
    # row.  This avoids stale entries after a server is removed within the same
    # request cycle (test_remove_mcp_server regression).

    return response


@router.post("/", response_model=Fiche, status_code=status.HTTP_201_CREATED)
async def add_mcp_server(
    fiche_id: int,
    request: MCPServerAddRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Add an MCP server to a fiche."""
    # Get fiche and check permissions
    fiche = crud.get_fiche(db, fiche_id=fiche_id)
    if not fiche:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fiche not found")

    if fiche.owner_id != current_user.id and current_user.role != "ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to modify this fiche")

    # Build MCP server config
    if request.preset:
        server_config: Dict[str, Any] = {
            "preset": request.preset,
        }
        # Encrypt auth token if provided
        if request.auth_token:
            server_config["auth_token"] = crypto.encrypt(request.auth_token)
    elif request.transport == "stdio":
        # Stdio transport - command-based subprocess
        server_config = {
            "type": "stdio",
            "name": request.name,
            "command": request.command,
        }
        if request.env:
            server_config["env"] = request.env
    else:
        # HTTP transport - validate HTTPS URL for security
        if not request.url.startswith("https://"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="MCP server URL must use HTTPS for security")

        server_config = {
            "type": "custom",
            "url": request.url,
            "name": request.name,
        }
        # Encrypt auth token if provided
        if request.auth_token:
            server_config["auth_token"] = crypto.encrypt(request.auth_token)

    if request.allowed_tools:
        server_config["allowed_tools"] = request.allowed_tools

    # Add to fiche config
    current_config = fiche.config or {}
    mcp_servers = _get_mcp_servers_from_config(current_config)

    # Check for duplicates
    for existing in mcp_servers:
        if request.preset and existing.get("preset") == request.preset:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Preset '{request.preset}' is already configured for this fiche",
            )
        elif request.transport == "stdio" and existing.get("command") == request.command:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Server with command '{request.command}' is already configured for this fiche",
            )
        elif request.transport == "http" and not request.preset and existing.get("url") == request.url:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Server URL '{request.url}' is already configured for this fiche",
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

    # Update fiche config
    mcp_servers.append(server_config)
    updated_config = _update_mcp_servers_in_config(current_config, mcp_servers)

    # Save to database
    set_json_field(fiche, "config", updated_config)
    db.commit()
    db.refresh(fiche)
    return fiche


@router.delete("/{server_name}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_mcp_server(
    fiche_id: int,
    server_name: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Remove an MCP server from a fiche."""
    # Get fiche and check permissions
    fiche = crud.get_fiche(db, fiche_id=fiche_id)
    if not fiche:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fiche not found")

    if fiche.owner_id != current_user.id and current_user.role != "ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to modify this fiche")

    # Get MCP servers from config
    current_config = fiche.config or {}
    mcp_servers = _get_mcp_servers_from_config(current_config)

    # Find and remove the server
    found = False
    updated_servers = []

    removed_configs: List[Dict[str, Any]] = []

    for server_config in mcp_servers:
        if "preset" in server_config and server_config["preset"] == server_name:
            found = True
            removed_configs.append(server_config)
            continue  # Skip this server
        elif server_config.get("name") == server_name:
            found = True
            removed_configs.append(server_config)
            continue  # Skip this server
        updated_servers.append(server_config)

    if not found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"MCP server '{server_name}' not found")

    # Update fiche config
    set_json_field(fiche, "config", {"mcp_servers": updated_servers})
    db.commit()

    # If any removed servers were stdio transport, shut down their processes
    for removed in removed_configs:
        transport = removed.get("transport")
        if removed.get("type") == "stdio" or transport == "stdio" or "command" in removed:
            try:
                manager = MCPManager()
                cfg = MCPServerConfig(
                    name=removed.get("name", server_name),
                    transport="stdio",
                    command=removed.get("command"),
                    env=removed.get("env"),
                    allowed_tools=removed.get("allowed_tools"),
                    timeout=removed.get("timeout", 30.0),
                    max_retries=removed.get("max_retries", 3),
                )
                manager.shutdown_stdio_process_for_config_sync(cfg)
            except Exception:
                logger.exception("Failed to shutdown removed stdio MCP server '%s'", server_name)

    return None


@router.post("/test", response_model=MCPTestConnectionResponse)
async def test_mcp_connection(
    fiche_id: int,
    request: MCPServerAddRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Test connection to an MCP server without saving it."""
    # Get fiche and check permissions (for context)
    fiche = crud.get_fiche(db, fiche_id=fiche_id)
    if not fiche:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fiche not found")

    if fiche.owner_id != current_user.id and current_user.role != "ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to test servers for this fiche")

    # Build MCP server config
    if request.preset:
        server_config: Dict[str, Any] = {
            "preset": request.preset,
            "auth_token": request.auth_token,
        }
    elif request.transport == "stdio":
        server_config = {
            "type": "stdio",
            "name": request.name,
            "command": request.command,
        }
        if request.env:
            server_config["env"] = request.env
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
    fiche_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Get all available tools for a fiche (built-in + MCP)."""
    # Get fiche and check permissions
    fiche = crud.get_fiche(db, fiche_id=fiche_id)
    if not fiche:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fiche not found")

    if fiche.owner_id != current_user.id and current_user.role != "ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to view this fiche")

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
