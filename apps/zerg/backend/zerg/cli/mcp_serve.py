"""CLI command to run the Longhouse MCP server.

Provides ``longhouse mcp-server`` which launches a Model Context Protocol
server over stdio (default) or HTTP transport.  CLI agents like Claude Code
connect to this server to access Longhouse session search, memory, and
notification tools.
"""

from __future__ import annotations

import typer

from zerg.services.shipper.token import get_zerg_url
from zerg.services.shipper.token import load_token


def mcp_server(
    url: str = typer.Option(
        None,
        "--url",
        "-u",
        help="Longhouse API URL (auto-detected from stored config if omitted)",
    ),
    token: str = typer.Option(
        None,
        "--token",
        "-t",
        envvar="AGENTS_API_TOKEN",
        help="API token (uses stored device token if omitted)",
    ),
    transport: str = typer.Option(
        "stdio",
        "--transport",
        help="Transport: stdio or http",
    ),
    port: int = typer.Option(
        8001,
        "--port",
        "-p",
        help="Port for HTTP transport",
    ),
) -> None:
    """Run the Longhouse MCP server for CLI agent integration.

    Exposes session search, memory read/write, and notification tools
    via the Model Context Protocol (MCP).

    Examples:
        longhouse mcp-server
        longhouse mcp-server --url https://david.longhouse.ai --transport http --port 9000
    """
    if not url:
        url = get_zerg_url() or "http://localhost:8080"
    if not token:
        token = load_token()

    from zerg.mcp_server import create_server

    server = create_server(api_url=url, api_token=token)

    if transport == "stdio":
        server.run()
    elif transport == "http":
        # FastMCP.run() doesn't accept a port argument; use uvicorn directly.
        import uvicorn

        try:
            app = server.sse_app()
        except AttributeError:
            # Newer mcp SDK versions expose streamable-http via ASGI
            app = server.streamable_http_app()
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
    else:
        typer.secho(f"Unknown transport: {transport}. Use 'stdio' or 'http'.", fg=typer.colors.RED)
        raise typer.Exit(code=1)
