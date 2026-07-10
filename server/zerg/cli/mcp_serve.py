"""Run the Longhouse MCP server."""

from __future__ import annotations

import logging

import typer

from zerg.services.shipper.token import get_zerg_url
from zerg.services.shipper.token import load_token

logger = logging.getLogger(__name__)


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
        help="Device token (uses stored device token if omitted)",
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
    """Run the Longhouse MCP server for CLI agent integration."""
    if not url:
        url = get_zerg_url() or "http://localhost:8080"
    if not token:
        token = load_token()

    from zerg.mcp_server import create_server

    server = create_server(api_url=url, api_token=token)

    if transport == "stdio":
        server.run()
    elif transport == "http":
        # FastMCP.run() doesn't accept a port argument; use uvicorn directly
        # with the streamable-http ASGI app.
        import uvicorn

        http_app = server.streamable_http_app()
        uvicorn.run(http_app, host="127.0.0.1", port=port, log_level="info")
    else:
        typer.secho(f"Unknown transport: {transport}. Use 'stdio' or 'http'.", fg=typer.colors.RED)
        raise typer.Exit(code=1)
