"""Onboarding wizard for Longhouse.

Guides new users through initial setup:
- Verify/install dependencies
- Configure server
- Set up shipper for session sync
- Emit test event to verify pipeline

Usage:
    longhouse onboard          # Interactive setup
    longhouse onboard --quick  # QuickStart (defaults)
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import httpx
import typer

from zerg.cli.config_file import get_config_path
from zerg.cli.config_file import save_config
from zerg.cli.serve import _get_longhouse_home
from zerg.cli.serve import _is_server_running


def _has_command(cmd: str) -> bool:
    """Check if a command exists in PATH."""
    return shutil.which(cmd) is not None


def _has_gui() -> bool:
    """Check if GUI is available for browser opening."""
    # Check common GUI indicators
    if sys.platform == "darwin":
        # macOS always has GUI unless SSH
        return "SSH_CONNECTION" not in os.environ

    if sys.platform == "win32":
        return True

    # Linux: check for display
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _has_systemd() -> bool:
    """Check if systemd is available."""
    if sys.platform != "linux":
        return False
    return Path("/run/systemd/system").exists()


def _has_launchd() -> bool:
    """Check if launchd is available (macOS)."""
    return sys.platform == "darwin"


def _check_server_health(host: str = "127.0.0.1", port: int = 8080, timeout: float = 2.0) -> bool:
    """Check if server is responding to health checks."""
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(f"http://{host}:{port}/api/health")
            return response.status_code == 200
    except Exception:
        return False


def _wait_for_server(host: str = "127.0.0.1", port: int = 8080, timeout: float = 30.0) -> bool:
    """Wait for server to become healthy."""
    start = time.time()
    while time.time() - start < timeout:
        if _check_server_health(host, port):
            return True
        time.sleep(0.5)
    return False


def _derive_client_url(host: str, port: int) -> str:
    """Derive a client-accessible URL from bind host/port.

    Maps wildcard bind addresses to localhost for client use,
    and properly formats IPv6 addresses with brackets.

    Args:
        host: The bind host (may be 0.0.0.0, ::, etc.)
        port: The port number

    Returns:
        A URL suitable for client access
    """
    # Map wildcard binds to localhost
    if host in ("0.0.0.0", "::", ""):
        client_host = "127.0.0.1"
    elif ":" in host and not host.startswith("["):
        # IPv6 literal needs brackets
        client_host = f"[{host}]"
    else:
        client_host = host

    return f"http://{client_host}:{port}"


def _emit_test_event(api_url: str) -> bool:
    """Emit a test event to verify the pipeline."""
    try:
        # Create a minimal test session/event
        payload = {
            "id": f"test-{int(time.time())}",
            "provider": "test",
            "environment": "onboarding",
            "project": "longhouse-test",
            "device_id": f"onboard-{socket.gethostname()}",
            "cwd": str(Path.home()),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "events": [
                {
                    "type": "user",
                    "content": "Welcome to Longhouse! This is a test event from onboarding.",
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            ],
        }

        with httpx.Client(timeout=10) as client:
            response = client.post(f"{api_url}/api/agents/ingest", json=payload)
            return response.status_code in (200, 201)
    except Exception as e:
        typer.echo(f"  Error: {e}")
        return False


app = typer.Typer(help="Onboarding wizard")


@app.command()
def onboard(
    quick: bool = typer.Option(
        False,
        "--quick",
        "-q",
        help="QuickStart mode - use all defaults",
    ),
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Server host",
    ),
    port: int = typer.Option(
        8080,
        "--port",
        "-p",
        help="Server port",
    ),
    no_server: bool = typer.Option(
        False,
        "--no-server",
        help="Skip server startup",
    ),
    no_shipper: bool = typer.Option(
        False,
        "--no-shipper",
        help="Skip shipper installation",
    ),
) -> None:
    """Run the Longhouse onboarding wizard.

    Guides you through initial setup:
    1. Verify dependencies (Claude Code, etc.)
    2. Start local server
    3. Set up session shipping
    4. Verify with test event

    Use --quick for automated setup with defaults.
    """
    typer.echo("")
    typer.secho("Welcome to Longhouse", fg=typer.colors.CYAN, bold=True)
    typer.echo("")

    # Quick vs Manual mode selection
    if not quick:
        typer.echo("[1] QuickStart (recommended) - Ready in 30 seconds")
        typer.echo("[2] Manual Setup - Configure each option")
        typer.echo("")

        choice = typer.prompt("Choice", default="1")
        quick = choice == "1"
        typer.echo("")

    # Step 1: Check dependencies
    typer.secho("Step 1: Checking dependencies", fg=typer.colors.BLUE, bold=True)
    typer.echo("")

    # Check Claude Code
    has_claude = _has_command("claude")
    if has_claude:
        typer.secho("  [OK] Claude Code found", fg=typer.colors.GREEN)
    else:
        typer.secho("  [--] Claude Code not found (optional)", fg=typer.colors.YELLOW)
        typer.echo("       Install from: https://docs.anthropic.com/claude-code")

    # Check for existing config
    config_path = get_config_path()
    if config_path.exists():
        typer.secho(f"  [OK] Config found: {config_path}", fg=typer.colors.GREEN)
    else:
        typer.echo(f"  [--] No config file (will create: {config_path})")

    typer.echo("")

    # Step 2: Server setup
    typer.secho("Step 2: Server setup", fg=typer.colors.BLUE, bold=True)
    typer.echo("")

    api_url = _derive_client_url(host, port)

    if no_server:
        typer.echo("  Skipping server setup (--no-server)")
    else:
        # Check if server is already running
        is_running, pid = _is_server_running()

        if is_running:
            typer.secho(f"  [OK] Server already running (PID {pid})", fg=typer.colors.GREEN)
        elif _check_server_health(host, port):
            typer.secho(f"  [OK] Server responding at {api_url}", fg=typer.colors.GREEN)
        else:
            # Need to start server
            if quick or typer.confirm("Start Longhouse server?", default=True):
                typer.echo("  Starting server in daemon mode...")

                try:
                    # Start server as daemon
                    subprocess.run(
                        ["longhouse", "serve", "--daemon", "--host", host, "--port", str(port)],
                        check=True,
                        capture_output=True,
                    )

                    # Wait for it to be ready
                    typer.echo("  Waiting for server to be ready...")
                    if _wait_for_server(host, port, timeout=30):
                        typer.secho(f"  [OK] Server started at {api_url}", fg=typer.colors.GREEN)
                    else:
                        typer.secho("  [WARN] Server started but not responding", fg=typer.colors.YELLOW)
                        typer.echo(f"         Check logs: {_get_longhouse_home() / 'server.log'}")

                except subprocess.CalledProcessError as e:
                    typer.secho(f"  [ERROR] Failed to start server: {e}", fg=typer.colors.RED)
                    typer.echo("         Try starting manually: longhouse serve")
            else:
                typer.echo("  Skipping server startup")

    typer.echo("")

    # Step 3: Shipper setup
    typer.secho("Step 3: Session shipping", fg=typer.colors.BLUE, bold=True)
    typer.echo("")

    if no_shipper:
        typer.echo("  Skipping shipper setup (--no-shipper)")
    elif not has_claude:
        typer.echo("  Skipping shipper (Claude Code not installed)")
        typer.echo("  Install Claude Code first, then run: longhouse connect --install")
    else:
        # Check for service manager
        has_service_manager = _has_launchd() or _has_systemd()

        if has_service_manager:
            if quick or typer.confirm("Install background shipper service?", default=True):
                try:
                    result = subprocess.run(
                        ["longhouse", "connect", "--install", "--url", api_url],
                        capture_output=True,
                        text=True,
                    )

                    if result.returncode == 0:
                        typer.secho("  [OK] Shipper service installed", fg=typer.colors.GREEN)
                    else:
                        typer.secho("  [WARN] Service install failed", fg=typer.colors.YELLOW)
                        typer.echo(f"         {result.stderr.strip()}")
                        typer.echo("         Run manually: longhouse connect")

                except Exception as e:
                    typer.secho(f"  [WARN] Could not install service: {e}", fg=typer.colors.YELLOW)
        else:
            typer.secho("  [--] No service manager (WSL/SSH environment)", fg=typer.colors.YELLOW)
            typer.echo("       Run shipper in foreground: longhouse connect")
            typer.echo("       Or use: longhouse ship (one-shot sync)")

    typer.echo("")

    # Step 4: Verification
    typer.secho("Step 4: Verification", fg=typer.colors.BLUE, bold=True)
    typer.echo("")

    if _check_server_health(host, port):
        typer.echo("  Emitting test event...")
        if _emit_test_event(api_url):
            typer.secho("  [OK] Test event shipped successfully", fg=typer.colors.GREEN)
        else:
            typer.secho("  [WARN] Test event failed (server may need auth)", fg=typer.colors.YELLOW)
    else:
        typer.echo("  Skipping verification (server not running)")

    typer.echo("")

    # Step 5: Save config
    typer.secho("Step 5: Saving configuration", fg=typer.colors.BLUE, bold=True)
    typer.echo("")

    config_data = {
        "server": {
            "host": host,
            "port": port,
        },
        "shipper": {
            "mode": "watch",
            "api_url": api_url,
        },
    }

    try:
        save_config(config_data)
        typer.secho(f"  [OK] Config saved: {config_path}", fg=typer.colors.GREEN)
    except Exception as e:
        typer.secho(f"  [WARN] Could not save config: {e}", fg=typer.colors.YELLOW)

    typer.echo("")

    # Step 6: Open browser (if GUI available)
    if _has_gui() and _check_server_health(host, port):
        if quick or typer.confirm("Open Longhouse in browser?", default=True):
            typer.echo(f"  Opening {api_url}...")
            try:
                webbrowser.open(api_url)
            except Exception:
                typer.echo(f"  Could not open browser. Visit: {api_url}")

    typer.echo("")

    # Done!
    typer.echo("=" * 50)
    typer.secho("Setup complete!", fg=typer.colors.GREEN, bold=True)
    typer.echo("=" * 50)
    typer.echo("")
    typer.echo("Quick reference:")
    typer.echo(f"  Server:    {api_url}")
    typer.echo(f"  Config:    {config_path}")
    typer.echo(f"  Data:      {_get_longhouse_home()}")
    typer.echo("")
    typer.echo("Commands:")
    typer.echo("  longhouse status   Show configuration")
    typer.echo("  longhouse connect  Sync Claude sessions")
    typer.echo("  longhouse serve --stop  Stop server")
    typer.echo("")


# Export for main.py registration
__all__ = ["onboard"]
