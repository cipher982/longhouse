"""MCP (Model Context Protocol) adapter for the Zerg tool registry.

This module provides integration between MCP servers and our internal tool registry,
allowing fiches to use both built-in tools and MCP-provided tools seamlessly.

This module started life as a **proof-of-concept** with a few hard-coded
presets.  It has now been promoted to a *production-ready* component that
supports **dynamic** MCP server registration while still exposing the same
convenience presets (now moved to `zerg.tools.mcp_presets`).

Key changes compared to the PoC version:

1.  ❌  No more hard-coded presets in the adapter itself.  Presets live in
    `mcp_presets.py` and can be modified without touching any logic.
2.  ✅  New `MCPManager` singleton caches one adapter per (url, auth_token)
    so we never double-register tools when multiple fiches share the same MCP
    server.
3.  ✅  Public helpers `load_mcp_tools()` (async) and
    `load_mcp_tools_sync()` (sync) make it trivial to load tools from within
    both asynchronous and synchronous code paths.
4.  ✅  Dual transport support: HTTP (default) and stdio (subprocess-based).

See `docs/mcp_integration_requirements.md` for the end-to-end design.
"""

import asyncio
import logging
import threading
from typing import Any
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple

import jsonschema

from zerg.tools.mcp_config_schema import normalize_config
from zerg.tools.mcp_exceptions import MCPAuthenticationError
from zerg.tools.mcp_exceptions import MCPConfigurationError
from zerg.tools.mcp_exceptions import MCPConnectionError
from zerg.tools.mcp_exceptions import MCPToolExecutionError
from zerg.tools.mcp_exceptions import MCPValidationError
from zerg.tools.mcp_transport import MCPServerConfig
from zerg.tools.mcp_transport import MCPTransport
from zerg.tools.mcp_transport import create_transport

# ---------------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# We intentionally *do not* import the preset mapping here to avoid a circular
# dependency (`mcp_presets` imports :pyclass:`MCPServerConfig` from this very
# module).  The mapping is loaded **lazily** in :pyfunc:`MCPManager._get_presets`.

# Re-export MCPServerConfig for backwards compatibility
__all__ = ["MCPServerConfig", "MCPClient", "MCPToolAdapter", "MCPManager", "load_mcp_tools", "load_mcp_tools_sync"]


class MCPClient:
    """MCP client that delegates to transport implementations.

    Supports both HTTP and stdio transports through the transport abstraction.
    For stdio transport, clients should be pooled via MCPManager to avoid
    spawning new processes per tool call.
    """

    def __init__(self, config: MCPServerConfig):
        self.config = config
        self.transport: MCPTransport = create_transport(config)

    async def __aenter__(self):
        """Context manager entry - connect transport."""
        await self.transport.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - disconnect transport."""
        await self.transport.disconnect()

    async def health_check(self) -> bool:
        """Check if the MCP server is reachable and responsive."""
        return await self.transport.health_check()

    async def list_tools(self) -> List[Dict[str, Any]]:
        """List available tools from the MCP server."""
        return await self.transport.list_tools()

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Call a tool on the MCP server."""
        return await self.transport.call_tool(tool_name, arguments)


class MCPToolAdapter:
    """Adapts MCP tools to work with our internal tool registry."""

    def __init__(self, server_config: MCPServerConfig):
        self.config = server_config
        self.client = MCPClient(server_config)
        self.tool_prefix = f"mcp_{server_config.name}_"
        self._tool_schemas: Dict[str, Dict[str, Any]] = {}

    async def register_tools(self):
        """Discover and register all tools from the MCP server."""
        # Perform health check first
        async with self.client as client:
            if not await client.health_check():
                logger.warning(f"Skipping MCP server '{self.config.name}' - health check failed")
                return

            try:
                tools = await client.list_tools()

                for tool_spec in tools:
                    tool_name = tool_spec.get("name", "")

                    # Check if tool is in allowlist (if specified)
                    if self.config.allowed_tools and tool_name not in self.config.allowed_tools:
                        continue

                    # Store the schema for validation
                    if "inputSchema" in tool_spec:
                        self._tool_schemas[tool_name] = tool_spec["inputSchema"]

                    # Create a wrapper function for this tool
                    wrapper_fn = self._create_tool_wrapper(tool_name, tool_spec)

                    # Register with mutable runtime registry so callers can rebuild
                    # the immutable production registry to include these tools.
                    try:
                        from langchain_core.tools import StructuredTool

                        from zerg.tools.registry import ToolRegistry

                        tool = StructuredTool.from_function(
                            wrapper_fn,
                            name=f"{self.tool_prefix}{tool_name}",
                            description=tool_spec.get("description", f"MCP tool {tool_name}"),
                        )
                        ToolRegistry().register(tool)
                        logger.info("Registered MCP tool: %s", f"{self.tool_prefix}{tool_name}")
                    except Exception as reg_err:  # noqa: BLE001
                        logger.error("Failed to register MCP tool %s: %s", tool_name, reg_err)
                        # Continue with others

            except MCPConnectionError as e:
                logger.error(f"Failed to register MCP tools: {e}")
                raise

    def _validate_inputs(self, tool_name: str, arguments: Dict[str, Any]) -> None:
        """Validate tool inputs against the schema."""
        schema = self._tool_schemas.get(tool_name)
        if not schema:
            return  # No schema to validate against

        try:
            jsonschema.validate(instance=arguments, schema=schema)
        except jsonschema.ValidationError as e:
            errors = {
                "message": str(e.message),
                "path": list(e.path),
                "schema_path": list(e.schema_path),
            }
            raise MCPValidationError(tool_name, errors)

    def _create_tool_wrapper(self, tool_name: str, tool_spec: Dict[str, Any]) -> Callable:
        """Create a wrapper function that calls the MCP tool."""

        async def _async_tool_wrapper(**kwargs):  # noqa: D401 – internal helper
            # Validate inputs
            try:
                self._validate_inputs(tool_name, kwargs)
            except MCPValidationError as e:
                logger.error(f"Input validation failed for tool '{tool_name}': {e}")
                return f"Error: {e}"

            # Execute the tool
            try:
                # For stdio transport, use pooled client from MCPManager
                # For HTTP transport, create a new client per call (existing behavior)
                if self.config.transport == "stdio":
                    client = await MCPManager().get_pooled_client(self.config)
                    result = await client.call_tool(tool_name, kwargs)
                    return str(result)
                else:
                    async with MCPClient(self.config) as client:
                        result = await client.call_tool(tool_name, kwargs)
                        return str(result)
            except MCPToolExecutionError as e:
                logger.error(f"Tool execution failed: {e}")
                return f"Error: {e}"
            except Exception as e:  # noqa: BLE001
                logger.exception("Unexpected error calling MCP tool %s on %s", tool_name, self.config.name)
                return f"Error calling MCP tool {tool_name}: {e}"

        # Synchronous wrapper that uses the shared event loop
        def _sync_tool_wrapper(**kwargs):  # noqa: D401 – wrapper
            return MCPManager().run_in_loop(_async_tool_wrapper(**kwargs))

        _sync_tool_wrapper.__name__ = f"{self.tool_prefix}{tool_name}"
        _sync_tool_wrapper.__doc__ = tool_spec.get("description", "")

        # Add parameter information for better introspection
        if "inputSchema" in tool_spec:
            _sync_tool_wrapper.__annotations__ = self._extract_annotations(tool_spec["inputSchema"])

        return _sync_tool_wrapper

    def _extract_annotations(self, schema: Dict[str, Any]) -> Dict[str, type]:
        """Extract type annotations from JSON schema."""
        annotations = {}
        properties = schema.get("properties", {})

        type_mapping = {
            "string": str,
            "number": float,
            "integer": int,
            "boolean": bool,
            "array": list,
            "object": dict,
        }

        for prop_name, prop_schema in properties.items():
            json_type = prop_schema.get("type", "string")
            annotations[prop_name] = type_mapping.get(json_type, Any)

        return annotations


# ---------------------------------------------------------------------------
#  Manager – ensures tools are only registered once per MCP server
# ---------------------------------------------------------------------------


class MCPManager:
    """Singleton that tracks *one* ``MCPToolAdapter`` per unique server.

    For stdio transport, also maintains a pool of long-lived subprocess clients
    to avoid spawning a new process per tool call.
    """

    _instance: Optional["MCPManager"] = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._adapters: Dict[Tuple[str, str], MCPToolAdapter] = {}
                cls._instance._stdio_clients: Dict[str, MCPClient] = {}  # Pool for stdio clients
                cls._instance._stdio_client_locks: Dict[str, asyncio.Lock] = {}
        return cls._instance

    def run_in_loop(self, coro):
        """Run a coroutine using the shared async runner."""
        from zerg.utils.async_runner import run_in_shared_loop

        return run_in_shared_loop(coro)

    def _is_encrypted_token(self, token: str) -> bool:
        """Check if a token appears to be encrypted (Fernet format)."""
        # Fernet tokens are URL-safe base64 encoded and start with 'gAAAAA'
        # This is a simple heuristic check
        try:
            return token.startswith("gAAAAA") and len(token) > 50
        except Exception:
            return False

    def _get_adapter_key(self, cfg: MCPServerConfig) -> Tuple[str, str]:
        """Generate cache key for adapter based on transport type."""
        if cfg.transport == "stdio":
            return (f"stdio:{cfg.name}:{cfg.command}", "")
        else:
            return (cfg.url or "", cfg.auth_token or "")

    def _get_stdio_client_key(self, cfg: MCPServerConfig) -> str:
        """Generate cache key for stdio client pool."""
        # Include env vars in key since they affect process behavior
        env_hash = hash(tuple(sorted((cfg.env or {}).items())))
        return f"{cfg.name}:{cfg.command}:{env_hash}"

    def _get_stdio_client_lock(self, key: str) -> asyncio.Lock:
        """Get or create a lock for a stdio client pool entry."""
        if key not in self._stdio_client_locks:
            self._stdio_client_locks[key] = asyncio.Lock()
        return self._stdio_client_locks[key]

    # ------------------------------------------------------------------
    # Stdio client pool management
    # ------------------------------------------------------------------

    async def get_pooled_client(self, cfg: MCPServerConfig) -> MCPClient:
        """Get or create a pooled stdio client.

        For stdio transport, maintains long-lived process connections to avoid
        the overhead of spawning a new subprocess per tool call.
        """
        if cfg.transport != "stdio":
            raise ValueError("get_pooled_client only supports stdio transport")

        key = self._get_stdio_client_key(cfg)
        lock = self._get_stdio_client_lock(key)

        async with lock:
            if key not in self._stdio_clients:
                client = MCPClient(cfg)
                await client.transport.connect()
                self._stdio_clients[key] = client
                logger.info(f"Created pooled stdio client for MCP server '{cfg.name}'")

            # Check if process is still alive, reconnect if needed
            client = self._stdio_clients[key]
            if not await client.health_check():
                logger.warning(f"Stdio client for '{cfg.name}' is unhealthy, reconnecting...")
                await client.transport.disconnect()
                await client.transport.connect()

            return client

    async def shutdown_stdio_processes(self) -> None:
        """Shutdown all pooled stdio processes.

        Call this on application shutdown to cleanly terminate all MCP server
        subprocesses.
        """
        for key, client in list(self._stdio_clients.items()):
            try:
                logger.info(f"Shutting down stdio client: {key}")
                await client.transport.disconnect()
            except Exception as e:
                logger.warning(f"Error shutting down stdio client {key}: {e}")
        self._stdio_clients.clear()
        self._stdio_client_locks.clear()

    async def shutdown_stdio_process_for_config(self, cfg: MCPServerConfig) -> None:
        """Shutdown a single pooled stdio process, if present."""
        key = self._get_stdio_client_key(cfg)
        lock = self._get_stdio_client_lock(key)

        async with lock:
            client = self._stdio_clients.pop(key, None)
            if client is None:
                return
            try:
                logger.info(f"Shutting down stdio client: {key}")
                await client.transport.disconnect()
            except Exception as e:
                logger.warning(f"Error shutting down stdio client {key}: {e}")
        self._stdio_client_locks.pop(key, None)

    def shutdown_stdio_process_for_config_sync(self, cfg: MCPServerConfig) -> None:
        """Synchronous wrapper for shutdown_stdio_process_for_config."""
        self.run_in_loop(self.shutdown_stdio_process_for_config(cfg))

    def shutdown_stdio_processes_sync(self) -> None:
        """Synchronous wrapper for shutdown_stdio_processes."""
        self.run_in_loop(self.shutdown_stdio_processes())

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _init_adapter(self, cfg: MCPServerConfig):
        key = self._get_adapter_key(cfg)
        if key in self._adapters:
            return  # Already initialised

        # ------------------------------------------------------------------
        # During unit-tests we **never** want to make outbound HTTP requests –
        # they slow the suite down and would fail in CI where the hypothetical
        # MCP endpoints do not exist.  The global ``TESTING=1`` flag is set
        # at the very top of *backend/tests/conftest.py*.  We therefore short
        # -circuit initialisation when the flag is active and simply cache an
        # *empty* adapter instance.  Production behaviour is **unchanged**.
        # ------------------------------------------------------------------

        from zerg.config import get_settings  # local import to avoid cycle

        adapter = MCPToolAdapter(cfg)

        if get_settings().testing:
            # Skip remote discovery – tools will be absent which is fine for
            #   the assertions performed in the current test-suite.
            self._adapters[key] = adapter
            return

        # In non-test environments we proceed with the full registration
        # workflow which performs a health-check and dynamically registers
        # tools exposed by the MCP server.
        await adapter.register_tools()
        self._adapters[key] = adapter
        # After successful registration, refresh immutable registry view
        try:
            from zerg.tools import refresh_registry
            from zerg.tools.unified_access import reset_tool_resolver

            refresh_registry()
            reset_tool_resolver()
        except Exception:  # pragma: no cover – best-effort refresh
            pass

    async def add_server_async(self, cfg_dict: Dict[str, Any]):
        """Add a server configuration (async version)."""
        # Normalize and validate configuration
        try:
            normalized_config = normalize_config(cfg_dict)
        except ValueError as e:
            raise MCPConfigurationError(str(e))

        # Handle preset configuration
        if normalized_config["type"] == "preset":
            preset_name = normalized_config["preset"]
            presets = self._get_presets()

            if preset_name not in presets:
                raise MCPConfigurationError(f"Unknown preset: {preset_name}")

            base_cfg: MCPServerConfig = presets[preset_name]

            # Decrypt auth token if it looks encrypted (base64 with specific prefix)
            auth_token = normalized_config.get("auth_token", base_cfg.auth_token)
            if auth_token and self._is_encrypted_token(auth_token):
                try:
                    from zerg.utils import crypto

                    auth_token = crypto.decrypt(auth_token)
                except Exception as e:
                    logger.error(f"Failed to decrypt auth token: {e}")
                    raise MCPAuthenticationError(preset_name, "Failed to decrypt authentication token")

            cfg = MCPServerConfig(
                name=base_cfg.name,
                transport=getattr(base_cfg, "transport", "http"),
                url=base_cfg.url,
                auth_token=auth_token,
                command=getattr(base_cfg, "command", None),
                env=getattr(base_cfg, "env", None),
                allowed_tools=normalized_config.get("allowed_tools", base_cfg.allowed_tools),
                timeout=normalized_config.get("timeout", base_cfg.timeout),
                max_retries=normalized_config.get("max_retries", base_cfg.max_retries),
            )
        elif normalized_config["type"] == "stdio":
            # Handle stdio transport configuration
            try:
                cfg = MCPServerConfig(
                    name=normalized_config["name"],
                    transport="stdio",
                    command=normalized_config["command"],
                    env=normalized_config.get("env"),
                    allowed_tools=normalized_config.get("allowed_tools"),
                    timeout=normalized_config.get("timeout", 30.0),
                    max_retries=normalized_config.get("max_retries", 3),
                )
            except (KeyError, TypeError) as exc:
                raise MCPConfigurationError(f"Invalid stdio configuration: {exc}")
        else:  # type == "custom" (HTTP)
            try:
                # Decrypt auth token if it looks encrypted
                auth_token = normalized_config.get("auth_token")
                if auth_token and self._is_encrypted_token(auth_token):
                    try:
                        from zerg.utils import crypto

                        auth_token = crypto.decrypt(auth_token)
                    except Exception as e:
                        logger.error(f"Failed to decrypt auth token: {e}")
                        raise MCPAuthenticationError(normalized_config["name"], "Failed to decrypt authentication token")

                cfg = MCPServerConfig(
                    name=normalized_config["name"],
                    transport="http",
                    url=normalized_config["url"],
                    auth_token=auth_token,
                    allowed_tools=normalized_config.get("allowed_tools"),
                    timeout=normalized_config.get("timeout", 30.0),
                    max_retries=normalized_config.get("max_retries", 3),
                )
            except (KeyError, TypeError) as exc:
                raise MCPConfigurationError(f"Invalid configuration: {exc}")

        await self._init_adapter(cfg)

    def add_server(self, cfg_dict: Dict[str, Any]):
        """Synchronous wrapper around :pyfunc:`add_server_async`."""
        self.run_in_loop(self.add_server_async(cfg_dict))

    # ------------------------------------------------------------------
    # Internal helper – lazy preset loader to avoid circular import
    # ------------------------------------------------------------------

    @staticmethod
    def _get_presets() -> Dict[str, "MCPServerConfig"]:  # noqa: D401 – util
        """Return the preset mapping, importing the module on first use."""
        try:
            from zerg.tools.mcp_presets import PRESET_MCP_SERVERS  # type: ignore

            return PRESET_MCP_SERVERS
        except ImportError:  # pragma: no cover – missing optional file
            return {}


# ---------------------------------------------------------------------------
#  Public helpers – bulk-loaders
# ---------------------------------------------------------------------------


async def load_mcp_tools(mcp_configs: List[Dict[str, Any]]) -> None:  # noqa: D401
    """Async bulk loader.

    *mcp_configs* follows the schema documented in
    `docs/mcp_integration_requirements.md` (list of dicts where each dict is
    either a complete server config or contains a "preset" key).
    """
    if not mcp_configs:
        return

    manager = MCPManager()
    await asyncio.gather(
        *(manager.add_server_async(cfg) for cfg in mcp_configs),
        return_exceptions=True,  # Don't fail all if one fails
    )


def load_mcp_tools_sync(mcp_configs: List[Dict[str, Any]]) -> None:  # noqa: D401
    """Sync convenience wrapper for *blocking* contexts."""
    if not mcp_configs:
        return

    manager = MCPManager()
    manager.run_in_loop(load_mcp_tools(mcp_configs))
