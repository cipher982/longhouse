"""Custom exceptions for MCP integration.

This module defines specific exception types for different MCP-related failures,
enabling better error handling and recovery strategies.
"""


class MCPException(Exception):
    """Base exception for all MCP-related errors."""

    pass


class MCPConnectionError(MCPException):
    """Raised when unable to connect to an MCP server."""

    def __init__(self, server_name: str, url: str, cause: Exception = None):
        self.server_name = server_name
        self.url = url
        self.cause = cause
        message = f"Failed to connect to MCP server '{server_name}' at {url}"
        if cause:
            message += f": {cause}"
        super().__init__(message)


class MCPAuthenticationError(MCPException):
    """Raised when MCP server authentication fails."""

    def __init__(self, server_name: str, message: str = "Authentication failed"):
        self.server_name = server_name
        super().__init__(f"MCP server '{server_name}': {message}")


class MCPToolExecutionError(MCPException):
    """Raised when a tool execution fails on the MCP server."""

    def __init__(self, tool_name: str, server_name: str, cause: Exception = None):
        self.tool_name = tool_name
        self.server_name = server_name
        self.cause = cause
        message = f"Tool '{tool_name}' execution failed on server '{server_name}'"
        if cause:
            message += f": {cause}"
        super().__init__(message)


class MCPValidationError(MCPException):
    """Raised when tool input validation fails."""

    def __init__(self, tool_name: str, errors: dict):
        self.tool_name = tool_name
        self.errors = errors
        message = f"Validation failed for tool '{tool_name}': {errors}"
        super().__init__(message)


class MCPConfigurationError(MCPException):
    """Raised when MCP server configuration is invalid."""

    def __init__(self, message: str):
        super().__init__(f"Invalid MCP configuration: {message}")
