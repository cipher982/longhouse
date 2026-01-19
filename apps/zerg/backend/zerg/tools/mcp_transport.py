"""MCP transport abstraction layer.

This module provides transport abstractions for MCP (Model Context Protocol) communication,
supporting both HTTP-based and stdio-based (subprocess) transports.
"""

import asyncio
import json
import logging
import os
import shlex
from abc import ABC
from abc import abstractmethod
from dataclasses import dataclass
from typing import Any
from typing import Dict
from typing import List
from typing import Literal
from typing import Optional

import httpx

from zerg.tools.mcp_exceptions import MCPAuthenticationError
from zerg.tools.mcp_exceptions import MCPConnectionError
from zerg.tools.mcp_exceptions import MCPToolExecutionError

logger = logging.getLogger(__name__)


@dataclass
class MCPServerConfig:
    """Configuration for an MCP server connection."""

    name: str
    transport: Literal["http", "stdio"] = "http"

    # HTTP transport fields
    url: Optional[str] = None
    auth_token: Optional[str] = None

    # Stdio transport fields
    command: Optional[str] = None
    env: Optional[Dict[str, str]] = None

    # Common fields
    allowed_tools: Optional[List[str]] = None
    timeout: float = 30.0
    max_retries: int = 3


class MCPTransport(ABC):
    """Abstract base class for MCP transports."""

    def __init__(self, config: MCPServerConfig):
        self.config = config

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to the MCP server."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Close connection to the MCP server."""
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if the MCP server is reachable and responsive."""
        ...

    @abstractmethod
    async def list_tools(self) -> List[Dict[str, Any]]:
        """List available tools from the MCP server."""
        ...

    @abstractmethod
    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        """Call a tool on the MCP server."""
        ...


class HTTPTransport(MCPTransport):
    """HTTP-based MCP transport using httpx."""

    def __init__(self, config: MCPServerConfig):
        super().__init__(config)
        self.client: Optional[httpx.AsyncClient] = None
        self._health_check_passed = False

    async def connect(self) -> None:
        """Initialize the HTTP client."""
        if self.client is not None:
            return

        # Enable HTTP/2 for better multiplexing. Fall back to HTTP/1.1 if h2 is missing.
        try:
            self.client = httpx.AsyncClient(
                timeout=self.config.timeout,
                limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
                http2=True,
            )
        except ImportError:
            self.client = httpx.AsyncClient(
                timeout=self.config.timeout,
                limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
                http2=False,
            )

    async def disconnect(self) -> None:
        """Close the HTTP client."""
        if self.client:
            await self.client.aclose()
            self.client = None
        self._health_check_passed = False

    def _get_headers(self) -> Dict[str, str]:
        """Get common headers including auth."""
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.config.auth_token:
            headers["Authorization"] = f"Bearer {self.config.auth_token}"
        return headers

    async def _request_with_retry(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Make HTTP request with retry logic."""
        if not self.client:
            await self.connect()

        url = f"{self.config.url}{path}"
        headers = kwargs.pop("headers", {})
        headers.update(self._get_headers())

        last_exception = None
        for attempt in range(self.config.max_retries):
            try:
                if method == "GET":
                    response = await self.client.get(url, headers=headers, **kwargs)
                elif method == "POST":
                    response = await self.client.post(url, headers=headers, **kwargs)
                else:
                    raise ValueError(f"Unsupported HTTP method: {method}")

                response.raise_for_status()
                return response

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 401:
                    raise MCPAuthenticationError(self.config.name, "Invalid authentication token")
                elif e.response.status_code < 500:
                    raise
                last_exception = e

            except (httpx.ConnectError, httpx.TimeoutException) as e:
                last_exception = e

            if attempt < self.config.max_retries - 1:
                wait_time = 2**attempt
                logger.debug(f"Retrying request to {url} in {wait_time}s (attempt {attempt + 1}/{self.config.max_retries})")
                await asyncio.sleep(wait_time)

        raise MCPConnectionError(self.config.name, self.config.url, last_exception)

    async def health_check(self) -> bool:
        """Check if the MCP server is reachable and responsive."""
        if self._health_check_passed:
            return True

        if not self.client:
            await self.connect()

        headers = self._get_headers()
        try:
            response = await self.client.get(f"{self.config.url}/health", headers=headers, timeout=5.0)
            response.raise_for_status()
            self._health_check_passed = True
            logger.info(f"Health check passed for MCP server '{self.config.name}'")
            return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise MCPAuthenticationError(self.config.name, "Invalid authentication token")
            logger.warning(f"Health check failed for MCP server '{self.config.name}': HTTP {e.response.status_code}")
            return False
        except Exception as e:
            logger.warning(f"Health check failed for MCP server '{self.config.name}': {e}")
            return False

    async def list_tools(self) -> List[Dict[str, Any]]:
        """List available tools from the MCP server."""
        try:
            response = await self._request_with_retry("GET", "/tools/list")
            data = response.json()
            return data.get("tools", [])
        except MCPConnectionError:
            raise
        except Exception as e:
            logger.error(f"Failed to list MCP tools from '{self.config.name}': {e}")
            raise MCPConnectionError(self.config.name, self.config.url, e)

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Call a tool on the MCP server."""
        try:
            response = await self._request_with_retry("POST", "/tools/call", json={"name": tool_name, "arguments": arguments})
            result = response.json()

            # Extract content from MCP response format
            if "content" in result and isinstance(result["content"], list):
                text_parts = []
                for block in result["content"]:
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                return "\n".join(text_parts)

            return result

        except MCPConnectionError:
            raise
        except Exception as e:
            raise MCPToolExecutionError(tool_name, self.config.name, e)


class StdioTransport(MCPTransport):
    """Stdio-based MCP transport - spawns server as subprocess.

    This transport implements the MCP JSON-RPC 2.0 protocol over stdio,
    communicating with an MCP server spawned as a subprocess.
    """

    def __init__(self, config: MCPServerConfig):
        super().__init__(config)
        self.process: Optional[asyncio.subprocess.Process] = None
        self._message_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._initialized = False
        self._tools_cache: Optional[List[Dict[str, Any]]] = None
        self._lock = asyncio.Lock()

    def _next_id(self) -> int:
        """Get next message ID."""
        self._message_id += 1
        return self._message_id

    async def connect(self) -> None:
        """Spawn MCP server process and initialize."""
        if self.process is not None and self.process.returncode is None:
            return  # Already connected

        if not self.config.command:
            raise MCPConnectionError(self.config.name, "stdio", ValueError("No command specified for stdio transport"))

        # Build environment
        env = os.environ.copy()
        if self.config.env:
            env.update(self.config.env)

        # Parse command - handle both string and potential list
        cmd = shlex.split(self.config.command)

        try:
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError as e:
            raise MCPConnectionError(self.config.name, self.config.command, e)
        except Exception as e:
            raise MCPConnectionError(self.config.name, self.config.command, e)

        # Start background reader task
        self._reader_task = asyncio.create_task(self._read_responses())
        if self.process.stderr:
            self._stderr_task = asyncio.create_task(self._read_stderr())

        # Perform MCP initialize handshake
        await self._initialize()

    async def _initialize(self) -> None:
        """Perform MCP protocol initialization handshake."""
        try:
            # Send initialize request
            result = await self._send_request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "zerg", "version": "1.0.0"},
                },
            )
            logger.debug(f"MCP server '{self.config.name}' initialized: {result}")

            # Send initialized notification (no response expected)
            await self._send_notification("notifications/initialized", {})
            self._initialized = True

        except Exception as e:
            await self.disconnect()
            raise MCPConnectionError(self.config.name, self.config.command, e)

    async def _send_request(self, method: str, params: Optional[Dict] = None) -> Any:
        """Send JSON-RPC 2.0 request, wait for response."""
        if not self.process or not self.process.stdin:
            raise MCPConnectionError(self.config.name, "stdio", ValueError("Not connected"))

        msg_id = self._next_id()
        request = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params:
            request["params"] = params

        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._pending[msg_id] = future

        try:
            message = json.dumps(request) + "\n"
            self.process.stdin.write(message.encode())
            await self.process.stdin.drain()

            return await asyncio.wait_for(future, timeout=self.config.timeout)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise MCPConnectionError(self.config.name, self.config.command, TimeoutError(f"Request timed out: {method}"))
        except Exception:
            self._pending.pop(msg_id, None)
            raise

    async def _send_notification(self, method: str, params: Optional[Dict] = None) -> None:
        """Send JSON-RPC 2.0 notification (no response expected)."""
        if not self.process or not self.process.stdin:
            raise MCPConnectionError(self.config.name, "stdio", ValueError("Not connected"))

        notification = {"jsonrpc": "2.0", "method": method}
        if params:
            notification["params"] = params

        message = json.dumps(notification) + "\n"
        self.process.stdin.write(message.encode())
        await self.process.stdin.drain()

    async def _read_responses(self) -> None:
        """Background reader task for stdout."""
        while self.process and self.process.returncode is None:
            try:
                if not self.process.stdout:
                    break
                line = await self.process.stdout.readline()
                if not line:
                    break

                try:
                    response = json.loads(line.decode())
                except json.JSONDecodeError:
                    # Skip non-JSON lines (debug output, logs, etc.)
                    logger.debug(f"Non-JSON output from MCP server: {line.decode().strip()}")
                    continue

                msg_id = response.get("id")
                if msg_id is not None and msg_id in self._pending:
                    if "error" in response:
                        error = response["error"]
                        error_msg = error.get("message", "Unknown error")
                        self._pending[msg_id].set_exception(MCPToolExecutionError(str(msg_id), self.config.name, Exception(error_msg)))
                    else:
                        self._pending[msg_id].set_result(response.get("result"))
                    del self._pending[msg_id]
                elif "method" in response:
                    # Handle server-initiated notifications
                    logger.debug(f"Received notification from MCP server: {response.get('method')}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Error reading from MCP server '{self.config.name}': {e}")

        # Process ended - fail any pending requests
        for future in self._pending.values():
            if not future.done():
                future.set_exception(MCPConnectionError(self.config.name, "stdio", Exception("Process terminated")))
        self._pending.clear()

    async def _read_stderr(self) -> None:
        """Background reader task for stderr to avoid blocking the process."""
        if not self.process or not self.process.stderr:
            return

        while self.process and self.process.returncode is None:
            try:
                line = await self.process.stderr.readline()
                if not line:
                    break
                logger.debug(
                    "MCP server '%s' stderr: %s",
                    self.config.name,
                    line.decode(errors="replace").strip(),
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Error reading stderr from MCP server '%s': %s", self.config.name, e)

    async def disconnect(self) -> None:
        """Terminate process gracefully."""
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None

        if self._stderr_task:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
            self._stderr_task = None

        if self.process:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
            self.process = None

        self._initialized = False
        self._tools_cache = None
        self._pending.clear()

    async def health_check(self) -> bool:
        """Check if the MCP server process is running."""
        if not self.process:
            return False
        if self.process.returncode is not None:
            return False
        return self._initialized

    async def list_tools(self) -> List[Dict[str, Any]]:
        """List available tools from the MCP server."""
        async with self._lock:
            if self._tools_cache is not None:
                return self._tools_cache

            if not self._initialized:
                await self.connect()

            try:
                result = await self._send_request("tools/list", {})
                self._tools_cache = result.get("tools", [])
                return self._tools_cache
            except Exception as e:
                logger.error(f"Failed to list MCP tools from '{self.config.name}': {e}")
                raise MCPConnectionError(self.config.name, self.config.command, e)

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Call a tool on the MCP server."""
        if not self._initialized:
            await self.connect()

        try:
            result = await self._send_request("tools/call", {"name": tool_name, "arguments": arguments})

            # Extract content from MCP response format
            if "content" in result and isinstance(result["content"], list):
                text_parts = []
                for block in result["content"]:
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                return "\n".join(text_parts)

            return result

        except MCPToolExecutionError:
            raise
        except Exception as e:
            raise MCPToolExecutionError(tool_name, self.config.name, e)


def create_transport(config: MCPServerConfig) -> MCPTransport:
    """Factory function to create appropriate transport based on config."""
    if config.transport == "http":
        return HTTPTransport(config)
    elif config.transport == "stdio":
        return StdioTransport(config)
    else:
        raise ValueError(f"Unknown transport type: {config.transport}")
