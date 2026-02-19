"""Connect command for shipping Claude Code sessions to Longhouse.

Commands:
- auth: Authenticate with Longhouse and obtain a device token
- ship: One-shot sync of all sessions
- connect: Continuous sync (watch mode or polling)
- connect --install: Install as background service
- connect --uninstall: Remove background service
- connect --status: Check service status
- recall: Search past sessions from the terminal

Watch mode (default): Uses file system events for sub-second sync.
Polling mode: Falls back to periodic scanning (--poll or --interval).
"""

from __future__ import annotations

import json as json_lib
import logging
import os
import socket
import subprocess
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
import typer

from zerg.services.shipper import clear_token
from zerg.services.shipper import clear_zerg_url
from zerg.services.shipper import get_service_info
from zerg.services.shipper import get_zerg_url
from zerg.services.shipper import install_codex_mcp_server
from zerg.services.shipper import install_hooks
from zerg.services.shipper import install_mcp_server
from zerg.services.shipper import install_service
from zerg.services.shipper import load_token
from zerg.services.shipper import save_token
from zerg.services.shipper import save_zerg_url
from zerg.services.shipper import uninstall_service
from zerg.services.shipper.service import get_engine_executable

app = typer.Typer(help="Ship Claude Code sessions to Longhouse")


def _verify_and_warn_path() -> None:
    """Run fresh-shell PATH verification and print warnings if any."""
    try:
        from zerg.cli.onboard import verify_shell_path

        warnings = verify_shell_path()
        if warnings:
            typer.echo("")
            typer.secho("  PATH check:", fg=typer.colors.YELLOW, bold=True)
            for warning in warnings:
                typer.secho(f"  {warning}", fg=typer.colors.YELLOW)
    except Exception:
        # Non-critical — don't fail the install if verification errors
        pass


# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auto-token helpers
# ---------------------------------------------------------------------------


def _is_localhost(url: str) -> bool:
    """Return True if the URL points to a loopback address."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def _auto_create_token(url: str, device_name: str | None = None) -> str | None:
    """Try to auto-create a device token. Returns plain token or None.

    Strategy (works for both localhost and remote):
    1. Try unauthenticated token creation (works when AUTH_DISABLED=1)
    2. If that fails, prompt for password and use cli-login flow
    3. If both fail, return None (caller falls back to manual flow)
    """
    device_name = device_name or socket.gethostname()

    # Step 1: Try unauthenticated token creation (AUTH_DISABLED mode)
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                f"{url.rstrip('/')}/api/devices/tokens",
                json={"device_id": device_name},
            )
            if resp.status_code in (200, 201):
                return resp.json().get("token")
    except Exception:
        pass

    # Step 2: Try password login
    if url.startswith("http://"):
        typer.echo("\u26a0 Warning: Sending password over unencrypted HTTP. Use HTTPS for production.")

    typer.echo("")
    password = typer.prompt("Password", hide_input=True)

    try:
        with httpx.Client(timeout=10) as client:
            login_resp = client.post(
                f"{url.rstrip('/')}/api/auth/cli-login",
                json={"password": password},
            )
    except httpx.ConnectError:
        typer.secho(f"Could not connect to {url}", fg=typer.colors.RED)
        return None
    except Exception as e:
        typer.secho(f"Login error: {e}", fg=typer.colors.RED)
        return None

    if login_resp.status_code == 401:
        typer.secho("Invalid password.", fg=typer.colors.RED)
        return None
    if login_resp.status_code == 400:
        typer.secho("Password auth not configured on this instance.", fg=typer.colors.RED)
        return None
    if login_resp.status_code == 429:
        typer.secho("Too many attempts. Try again later.", fg=typer.colors.RED)
        return None
    if login_resp.status_code != 200:
        typer.secho(f"Login failed (HTTP {login_resp.status_code}).", fg=typer.colors.RED)
        return None

    jwt_token = login_resp.json().get("token")
    if not jwt_token:
        typer.secho("Login response missing token.", fg=typer.colors.RED)
        return None

    # Step 3: Create device token using short-lived JWT
    try:
        with httpx.Client(timeout=10) as client:
            token_resp = client.post(
                f"{url.rstrip('/')}/api/devices/tokens",
                json={"device_id": device_name},
                headers={"Authorization": f"Bearer {jwt_token}"},
            )
    except Exception as e:
        typer.secho(f"Token creation error: {e}", fg=typer.colors.RED)
        return None

    if token_resp.status_code in (200, 201):
        return token_resp.json().get("token")

    typer.secho(f"Failed to create device token (HTTP {token_resp.status_code}).", fg=typer.colors.RED)
    return None


@app.command()
def auth(
    url: str = typer.Option(
        None,
        "--url",
        "-u",
        help="Longhouse API URL (e.g., https://api.longhouse.ai)",
    ),
    device_name: str = typer.Option(
        None,
        "--device",
        "-d",
        help="Device name (defaults to hostname)",
    ),
    token: str = typer.Option(
        None,
        "--token",
        "-t",
        help="Existing device token (skips interactive auth)",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Force re-authentication even if token exists",
    ),
    clear: bool = typer.Option(
        False,
        "--clear",
        help="Remove stored token and URL",
    ),
    claude_dir: str = typer.Option(
        None,
        "--claude-dir",
        help="Claude config directory (default: ~/.claude)",
    ),
) -> None:
    """Authenticate with Longhouse and store a device token.

    The token is stored locally and used for all subsequent ship/connect commands.

    Authentication methods (tried in order):
    1. Direct token: Use --token to provide an existing device token
    2. Auto-login: Prompts for password and creates token automatically
    3. Browser fallback: Opens web UI if auto-login is unavailable

    Examples:
        longhouse auth --url https://api.longhouse.ai
        longhouse auth --token zdt_your_token_here
        longhouse auth --clear
    """
    config_dir = Path(claude_dir) if claude_dir else None

    # Handle --clear
    if clear:
        cleared_token = clear_token(config_dir)
        cleared_url = clear_zerg_url(config_dir)
        if cleared_token and cleared_url:
            typer.secho("Cleared stored token and URL", fg=typer.colors.GREEN)
        elif cleared_token:
            typer.secho("Cleared stored token", fg=typer.colors.GREEN)
        elif cleared_url:
            typer.secho("Cleared stored URL", fg=typer.colors.GREEN)
        else:
            typer.echo("No token or URL to clear")
        return

    # Check for existing token
    existing_token = load_token(config_dir)
    existing_url = get_zerg_url(config_dir)

    if existing_token and not force:
        typer.echo(f"Already authenticated with {existing_url or 'unknown URL'}")
        typer.echo("Use --force to re-authenticate or --clear to remove")
        return

    # Get URL (required for auth)
    if not url:
        if existing_url:
            url = existing_url
            typer.echo(f"Using stored URL: {url}")
        else:
            url = typer.prompt("Longhouse API URL", default="https://api.longhouse.ai")

    # Get device name
    if not device_name:
        device_name = socket.gethostname()

    # If token provided directly, validate and store
    if token:
        if _validate_token(url, token):
            save_token(token, config_dir)
            save_zerg_url(url, config_dir)
            typer.secho(f"Token validated and stored for {device_name}", fg=typer.colors.GREEN)
        else:
            typer.secho("Invalid token", fg=typer.colors.RED)
            raise typer.Exit(code=1)
        return

    # Try auto-authenticate first (password-based, no browser needed)
    typer.echo(f"Authenticating with {url}...")
    auto_token = _auto_create_token(url, device_name)

    if auto_token:
        token = auto_token
        typer.secho(f"Device token created for {device_name}", fg=typer.colors.GREEN)
    else:
        # Fall back to manual browser-based flow
        typer.echo("")
        typer.echo("Auto-login not available. Falling back to manual token creation:")
        typer.echo(f"1. Open: {url.rstrip('/')}/settings/devices")
        typer.echo(f"2. Create a new token for device: {device_name}")
        typer.echo("3. Copy the token and paste it below")
        typer.echo("")

        # Try to open browser
        dashboard_url = f"{url.rstrip('/')}/settings/devices"
        try:
            if typer.confirm("Open browser to create token?", default=True):
                webbrowser.open(dashboard_url)
        except Exception:
            pass

        # Prompt for token
        token = typer.prompt("Device token")

        if not token:
            typer.secho("No token provided", fg=typer.colors.RED)
            raise typer.Exit(code=1)

    # Validate and store
    if _validate_token(url, token):
        save_token(token, config_dir)
        save_zerg_url(url, config_dir)
        typer.secho(f"Authenticated successfully as {device_name}", fg=typer.colors.GREEN)
    else:
        typer.secho("Invalid or expired token", fg=typer.colors.RED)
        raise typer.Exit(code=1)


def _validate_token(url: str, token: str) -> bool:
    """Validate a device token by making a test API call."""
    try:
        # Use a simple endpoint to validate
        with httpx.Client(timeout=10) as client:
            response = client.get(
                f"{url}/api/agents/sessions",
                headers={"X-Agents-Token": token},
                params={"limit": 1},
            )
            return response.status_code in (200, 501)  # 501 = Postgres not available but auth passed
    except Exception:
        return False


@app.command()
def ship(
    url: str = typer.Option(
        None,
        "--url",
        "-u",
        help="Longhouse API URL (uses stored URL if not specified)",
    ),
    token: str = typer.Option(
        None,
        "--token",
        "-t",
        envvar="AGENTS_API_TOKEN",
        help="API token (uses stored token if not specified)",
    ),
    file: str = typer.Option(
        None,
        "--file",
        "-f",
        help="Ship a single session JSONL file (used by hooks)",
    ),
    claude_dir: str = typer.Option(
        None,
        "--claude-dir",
        "-d",
        help="Claude config directory (default: ~/.claude)",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose output",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Suppress output (for hook usage)",
    ),
) -> None:
    """One-shot: ship all new Claude Code sessions to Longhouse.

    Use --file to ship a single session file (designed for Claude Code hook integration).
    """
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    config_dir = Path(claude_dir) if claude_dir else None

    # Load stored credentials if not provided
    if not url:
        url = get_zerg_url(config_dir) or "http://localhost:8080"
    if not token:
        token = load_token(config_dir)

    try:
        engine = get_engine_executable()
    except RuntimeError as e:
        if not quiet:
            typer.secho(str(e), fg=typer.colors.RED)
        raise typer.Exit(code=1)

    env = os.environ.copy()
    if verbose:
        env["RUST_LOG"] = "longhouse_engine=debug"
    if claude_dir:
        env["CLAUDE_CONFIG_DIR"] = claude_dir

    # Build base engine args
    engine_args = [engine, "ship"]
    if url:
        engine_args += ["--url", url]
    if token:
        engine_args += ["--token", token]

    # Single-file mode (for Claude Stop hook integration)
    if file:
        file_path = Path(file)
        if not file_path.exists():
            if not quiet:
                typer.secho(f"File not found: {file}", fg=typer.colors.RED)
            raise typer.Exit(code=1)
        engine_args += ["--file", str(file_path)]
        stdout = subprocess.DEVNULL if quiet else None
        stderr = subprocess.DEVNULL if quiet else None
        result = subprocess.run(engine_args, env=env, stdout=stdout, stderr=stderr)
        raise typer.Exit(code=result.returncode)

    # Full scan mode
    if not quiet:
        typer.echo(f"Shipping sessions to {url}...")
    stdout = subprocess.DEVNULL if quiet else None
    stderr = subprocess.DEVNULL if quiet else None
    result = subprocess.run(engine_args, env=env, stdout=stdout, stderr=stderr)
    raise typer.Exit(code=result.returncode)


@app.command()
def connect(
    url: str = typer.Option(
        None,
        "--url",
        "-u",
        help="Longhouse API URL (uses stored URL if not specified)",
    ),
    token: str = typer.Option(
        None,
        "--token",
        "-t",
        envvar="AGENTS_API_TOKEN",
        help="API token (uses stored token if not specified)",
    ),
    poll: bool = typer.Option(
        False,
        "--poll",
        "-p",
        help="Use polling mode instead of file watching",
    ),
    interval: int = typer.Option(
        30,
        "--interval",
        "-i",
        help="Polling interval in seconds (implies --poll)",
    ),
    debounce: int = typer.Option(
        500,
        "--debounce",
        help="Debounce delay in ms for watch mode",
    ),
    claude_dir: str = typer.Option(
        None,
        "--claude-dir",
        "-d",
        help="Claude config directory (default: ~/.claude)",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose output",
    ),
    install: bool = typer.Option(
        False,
        "--install",
        help="Install shipper as a background service + Claude Code hooks",
    ),
    hooks_only: bool = typer.Option(
        False,
        "--hooks-only",
        help="Install only Claude Code hooks (no background service)",
    ),
    uninstall: bool = typer.Option(
        False,
        "--uninstall",
        help="Stop and remove the background service",
    ),
    status: bool = typer.Option(
        False,
        "--status",
        help="Check the status of the background service",
    ),
) -> None:
    """Continuous: watch and ship sessions to Longhouse.

    By default uses file watching for sub-second sync.
    Use --poll or --interval for polling mode.

    Service management:
        --install    Install background service + Claude Code hooks
        --hooks-only Install only Claude Code hooks (no daemon)
        --uninstall  Stop and remove the background service
        --status     Check the status of the background service

    Run 'longhouse auth' first to authenticate with Longhouse.
    """
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    config_dir = Path(claude_dir) if claude_dir else None

    # Handle service management commands
    if status:
        _handle_status()
        return

    if uninstall:
        _handle_uninstall()
        return

    # Load stored credentials if not provided
    if not url:
        url = get_zerg_url(config_dir) or "http://localhost:8080"
    if not token:
        token = load_token(config_dir)

    # Auto-authenticate if no token exists
    if not token:
        typer.echo("No device token found. Attempting auto-authentication...")
        auto_token = _auto_create_token(url)
        if auto_token:
            save_token(auto_token, config_dir)
            save_zerg_url(url, config_dir)
            token = auto_token
            typer.secho("Authenticated successfully.", fg=typer.colors.GREEN)
        else:
            typer.secho(
                "Could not auto-authenticate. Run 'longhouse auth --token <token>' to authenticate manually.",
                fg=typer.colors.YELLOW,
            )
            raise typer.Exit(code=1)

    if hooks_only:
        _handle_hooks_only(url=url, token=token, claude_dir=claude_dir)
        return

    if install:
        _handle_install(url=url, token=token, claude_dir=claude_dir, poll=poll, interval=interval)
        return

    # Normal connect mode — exec longhouse-engine (replaces this process)
    try:
        engine = get_engine_executable()
    except RuntimeError as e:
        typer.secho(str(e), fg=typer.colors.RED)
        raise typer.Exit(code=1)

    if poll or interval != 30:
        typer.secho(
            "Warning: --poll / --interval are not supported by the Rust engine. "
            "The engine uses file watching with a fallback scan instead. Ignoring.",
            fg=typer.colors.YELLOW,
        )

    engine_args = [engine, "connect", "--flush-ms", str(debounce)]
    if interval != 30:
        engine_args += ["--fallback-scan-secs", str(interval)]

    env = os.environ.copy()
    if verbose:
        env["RUST_LOG"] = "longhouse_engine=debug"
    if claude_dir:
        env["CLAUDE_CONFIG_DIR"] = claude_dir

    typer.echo(f"Connecting to {url} (engine: {engine})...")
    typer.echo("Press Ctrl+C to stop")

    # exec replaces this process — signals pass through naturally
    os.execvpe(engine, engine_args, env)


@app.command()
def recall(
    query: str = typer.Argument(..., help="Search query for session content"),
    project: str = typer.Option(
        None,
        "--project",
        "-p",
        help="Filter by project name",
    ),
    provider: str = typer.Option(
        None,
        "--provider",
        help="Filter by provider (claude, codex, gemini)",
    ),
    days_back: int = typer.Option(
        14,
        "--days-back",
        "-d",
        help="Days to look back (1-90)",
    ),
    limit: int = typer.Option(
        10,
        "--limit",
        "-n",
        help="Max results to return (1-100)",
    ),
    output_json: bool = typer.Option(
        False,
        "--json",
        "-j",
        help="Output raw JSON response",
    ),
    url: str = typer.Option(
        None,
        "--url",
        "-u",
        help="Longhouse API URL (uses stored URL if not specified)",
    ),
    token: str = typer.Option(
        None,
        "--token",
        "-t",
        envvar="AGENTS_API_TOKEN",
        help="API token (uses stored token if not specified)",
    ),
    claude_dir: str = typer.Option(
        None,
        "--claude-dir",
        help="Claude config directory (default: ~/.claude)",
    ),
) -> None:
    """Search past sessions from the terminal.

    Queries the Longhouse API for sessions matching a text search,
    and displays results in a readable terminal format.

    Examples:
        longhouse recall "auth token refresh"
        longhouse recall "database migration" --project zerg --days-back 30
        longhouse recall "deploy fix" --json
    """
    config_dir = Path(claude_dir) if claude_dir else None

    # Load stored credentials if not provided
    if not url:
        url = get_zerg_url(config_dir)
        if not url:
            typer.secho("No Longhouse URL configured. Run 'longhouse auth' first.", fg=typer.colors.RED)
            raise typer.Exit(code=1)
    if not token:
        token = load_token(config_dir)
        if not token:
            typer.secho("No device token found. Run 'longhouse auth' first.", fg=typer.colors.RED)
            raise typer.Exit(code=1)

    # Build query params
    params: dict = {
        "query": query,
        "days_back": days_back,
        "limit": limit,
    }
    if project:
        params["project"] = project
    if provider:
        params["provider"] = provider

    # Make API request
    try:
        with httpx.Client(timeout=15) as client:
            response = client.get(
                f"{url.rstrip('/')}/api/agents/sessions",
                headers={"X-Agents-Token": token},
                params=params,
            )
    except httpx.ConnectError:
        typer.secho(f"Could not connect to {url}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    except httpx.TimeoutException:
        typer.secho(f"Request timed out connecting to {url}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    if response.status_code == 401:
        typer.secho("Authentication failed. Run 'longhouse auth' to re-authenticate.", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    if response.status_code != 200:
        typer.secho(f"API error: {response.status_code} {response.text[:200]}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    data = response.json()

    # Raw JSON output mode
    if output_json:
        typer.echo(json_lib.dumps(data, indent=2))
        return

    # Pretty-print results
    sessions = data.get("sessions", [])
    total = data.get("total", 0)

    if not sessions:
        typer.echo(f'No sessions found for "{query}"')
        return

    typer.echo(f'Found {total} session{"s" if total != 1 else ""} matching "{query}"')
    typer.echo("")

    for i, s in enumerate(sessions):
        # Format date
        started = s.get("started_at", "")
        try:
            dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
            date_str = dt.strftime("%Y-%m-%d %H:%M")
        except (ValueError, AttributeError):
            date_str = started[:16] if started else "unknown"

        # Header line: index, date, provider, project
        proj = s.get("project") or "-"
        prov = s.get("provider", "?")
        header = f"  [{i + 1}] {date_str}  {prov}"
        if proj != "-":
            header += f"  ({proj})"
        typer.secho(header, fg=typer.colors.CYAN, bold=True)

        # Stats line
        user_msgs = s.get("user_messages", 0)
        asst_msgs = s.get("assistant_messages", 0)
        tools = s.get("tool_calls", 0)
        git_branch = s.get("git_branch")
        stats = f"      {user_msgs}u/{asst_msgs}a msgs, {tools} tool calls"
        if git_branch:
            stats += f"  branch:{git_branch}"
        typer.echo(stats)

        # Snippet line (if search returned a match)
        snippet = s.get("match_snippet")
        if snippet:
            role = s.get("match_role", "")
            role_prefix = f"[{role}] " if role else ""
            # Truncate long snippets and normalize whitespace
            clean = " ".join(snippet.split())
            if len(clean) > 120:
                clean = clean[:117] + "..."
            typer.echo(f"      {role_prefix}{clean}")

        # CWD line
        cwd = s.get("cwd")
        if cwd:
            typer.echo(f"      cwd: {cwd}")

        typer.echo("")


def _handle_status() -> None:
    """Handle --status flag."""
    info = get_service_info()
    status = info["status"]

    typer.echo(f"Platform: {info['platform']}")
    typer.echo(f"Service: {info.get('service_name', 'N/A')}")

    if status == "running":
        typer.secho("Status: running", fg=typer.colors.GREEN)
    elif status == "stopped":
        typer.secho("Status: stopped", fg=typer.colors.YELLOW)
    else:
        typer.secho("Status: not installed", fg=typer.colors.RED)

    if status != "not-installed":
        typer.echo(f"Config: {info.get('service_file', 'N/A')}")
        typer.echo(f"Logs: {info['log_path']}")


def _handle_uninstall() -> None:
    """Handle --uninstall flag."""
    try:
        result = uninstall_service()
        typer.secho(f"[OK] {result['message']}", fg=typer.colors.GREEN)
    except RuntimeError as e:
        typer.secho(f"[ERROR] {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1)


def _handle_hooks_only(
    url: str,
    token: str | None,
    claude_dir: str | None,
) -> None:
    """Handle --hooks-only flag: install Claude Code hooks without the daemon."""
    typer.echo("Installing Claude Code hooks...")
    typer.echo(f"  URL: {url}")

    try:
        actions = install_hooks(url=url, token=token, claude_dir=claude_dir)
        typer.echo("")
        for action in actions:
            typer.secho(f"  [OK] {action}", fg=typer.colors.GREEN)
    except Exception as e:
        typer.secho(f"[ERROR] Failed to install hooks: {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    # Also register the MCP server for Claude Code
    try:
        mcp_actions = install_mcp_server(claude_dir=claude_dir)
        for action in mcp_actions:
            typer.secho(f"  [OK] {action}", fg=typer.colors.GREEN)
    except Exception as e:
        typer.secho(f"  [WARN] Claude MCP server registration failed: {e}", fg=typer.colors.YELLOW)

    # Also register the MCP server for Codex CLI
    try:
        codex_actions = install_codex_mcp_server()
        for action in codex_actions:
            typer.secho(f"  [OK] {action}", fg=typer.colors.GREEN)
    except Exception as e:
        typer.secho(f"  [WARN] Codex MCP server registration failed: {e}", fg=typer.colors.YELLOW)

    # Verify PATH in a fresh shell
    _verify_and_warn_path()

    typer.echo("")
    typer.echo("Hooks installed. Claude Code will ship sessions on each Stop event")
    typer.echo("and show recent sessions on SessionStart.")


def _handle_install(
    url: str,
    token: str | None,
    claude_dir: str | None,
    poll: bool,
    interval: int,
) -> None:
    """Handle --install flag."""
    typer.echo("Installing engine service...")
    typer.echo(f"  URL: {url}")

    try:
        result = install_service(
            url=url,
            token=token,
            claude_dir=claude_dir,
        )
        typer.echo("")
        typer.secho(f"[OK] {result['message']}", fg=typer.colors.GREEN)
        typer.echo(f"  Service: {result.get('service', 'N/A')}")
        typer.echo(f"  Config: {result.get('plist_path') or result.get('unit_path', 'N/A')}")
    except RuntimeError as e:
        typer.secho(f"[ERROR] {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    # Also install Claude Code hooks
    typer.echo("")
    typer.echo("Installing Claude Code hooks...")
    try:
        actions = install_hooks(url=url, token=token, claude_dir=claude_dir)
        for action in actions:
            typer.secho(f"  [OK] {action}", fg=typer.colors.GREEN)
    except Exception as e:
        typer.secho(f"  [WARN] Hook installation failed: {e}", fg=typer.colors.YELLOW)

    # Also register the MCP server for Claude Code
    try:
        mcp_actions = install_mcp_server(claude_dir=claude_dir)
        for action in mcp_actions:
            typer.secho(f"  [OK] {action}", fg=typer.colors.GREEN)
    except Exception as e:
        typer.secho(f"  [WARN] Claude MCP server registration failed: {e}", fg=typer.colors.YELLOW)

    # Also register the MCP server for Codex CLI
    try:
        codex_actions = install_codex_mcp_server()
        for action in codex_actions:
            typer.secho(f"  [OK] {action}", fg=typer.colors.GREEN)
    except Exception as e:
        typer.secho(f"  [WARN] Codex MCP server registration failed: {e}", fg=typer.colors.YELLOW)

    # Verify PATH in a fresh shell
    _verify_and_warn_path()

    typer.echo("")
    typer.echo("To check status: longhouse connect --status")
    typer.echo("To stop service: longhouse connect --uninstall")
