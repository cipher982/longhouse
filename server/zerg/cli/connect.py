"""CLI helpers for auth, shipping, connect, and recall."""

from __future__ import annotations

import json as json_lib
import logging
import os
import socket
import subprocess
import webbrowser
from datetime import datetime
from pathlib import Path

import httpx
import typer

from zerg.services.desktop_app import default_install_desktop_app
from zerg.services.desktop_app import get_desktop_app_service_info
from zerg.services.desktop_app import uninstall_desktop_app_service
from zerg.services.local_runtime_installer import apply_machine_state_update
from zerg.services.local_runtime_installer import reconcile_local_runtime
from zerg.services.longhouse_paths import resolve_longhouse_home_from_provider_home
from zerg.services.machine_repair import replay_machine_backlog
from zerg.services.machine_state import clear_machine_runtime_url
from zerg.services.shipper import clear_token
from zerg.services.shipper import get_service_info
from zerg.services.shipper import get_zerg_url
from zerg.services.shipper import load_token
from zerg.services.shipper import save_token
from zerg.services.shipper import uninstall_service
from zerg.services.shipper.service import Platform
from zerg.services.shipper.service import detect_platform
from zerg.services.shipper.service import get_engine_executable
from zerg.services.shipper.token import normalize_zerg_url


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
# ---------------------------------------------------------------------------
# Auto-token helpers
# ---------------------------------------------------------------------------


def _persist_selected_url(url: str, claude_dir: str | None, *, written_by: str) -> None:
    """Persist an explicit Runtime Host choice to canonical machine state."""
    normalized_url = normalize_zerg_url(url)
    if not normalized_url:
        raise ValueError(f"Invalid Longhouse URL: {url!r}")

    apply_machine_state_update(
        claude_dir=claude_dir,
        written_by=written_by,
        runtime_url=normalized_url,
    )


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
    config_dir = resolve_longhouse_home_from_provider_home(claude_dir) if claude_dir else None

    # Handle --clear
    if clear:
        cleared_token = clear_token(config_dir)
        try:
            cleared_url = clear_machine_runtime_url(config_dir, written_by="auth-clear")
        except RuntimeError as exc:
            typer.secho(f"Failed to clear stored URL: {exc}", fg=typer.colors.RED)
            raise typer.Exit(code=1) from exc
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
            _finalize_auth_success(
                url=url,
                token=token,
                config_dir=config_dir,
                claude_dir=claude_dir,
                success_message=f"Token validated and stored for {device_name}",
            )
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
        _finalize_auth_success(
            url=url,
            token=token,
            config_dir=config_dir,
            claude_dir=claude_dir,
            success_message=f"Authenticated successfully as {device_name}",
        )
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


def _attempt_post_auth_spool_replay(*, url: str, token: str, claude_dir: str | None) -> None:
    """Best-effort replay of queued local backlog after auth repair succeeds."""
    replay = replay_machine_backlog(
        url=url,
        token=token,
        claude_dir=claude_dir,
    )
    if replay.warning:
        typer.secho(
            f"Authenticated, but {replay.warning[0].lower()}{replay.warning[1:]}",
            fg=typer.colors.YELLOW,
        )


def _finalize_auth_success(
    *,
    url: str,
    token: str,
    config_dir: Path | None,
    claude_dir: str | None,
    success_message: str,
) -> None:
    save_token(token, config_dir)
    _persist_selected_url(url, claude_dir, written_by="auth")
    typer.secho(success_message, fg=typer.colors.GREEN)
    _attempt_post_auth_spool_replay(url=url, token=token, claude_dir=claude_dir)


def _resolve_configured_url(url: object | None, config_dir: Path | None) -> str:
    explicit_url = normalize_zerg_url(url)
    if explicit_url:
        return explicit_url

    stored_url = normalize_zerg_url(get_zerg_url(config_dir))
    if stored_url:
        return stored_url

    typer.secho("No Longhouse URL configured.", fg=typer.colors.RED)
    typer.echo(
        "Run `longhouse onboard` for a local setup, " "`longhouse auth --url <url>` for a remote instance, or pass `--url` explicitly."
    )
    raise typer.Exit(code=1)


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
        help="Device token (uses stored token if not specified)",
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

    config_dir = resolve_longhouse_home_from_provider_home(claude_dir) if claude_dir else None

    url = _resolve_configured_url(url, config_dir)
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


def connect(
    url: str = typer.Option(
        None,
        "--url",
        "-u",
        help="Longhouse API URL (uses stored URL if not specified)",
    ),
    domain: str | None = typer.Option(
        None,
        "--domain",
        help="Shorthand for --url: connect to https://<domain> (e.g. longhouse.example.com)",
    ),
    token: str = typer.Option(
        None,
        "--token",
        "-t",
        help="Device token (uses stored token if not specified)",
    ),
    interval: int = typer.Option(
        300,
        "--interval",
        "-i",
        help="Fallback scan interval in seconds (engine --fallback-scan-secs)",
    ),
    debounce: int | None = typer.Option(
        None,
        "--debounce",
        hidden=True,
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
        help="Install the background machine agent + Claude Code hooks",
    ),
    hooks_only: bool = typer.Option(
        False,
        hidden=True,
    ),
    uninstall: bool = typer.Option(
        False,
        "--uninstall",
        help="Stop and remove the background machine agent",
    ),
    status: bool = typer.Option(
        False,
        "--status",
        help="Check the status of the background machine agent",
    ),
    machine_name: str = typer.Option(
        None,
        "--machine-name",
        "-m",
        help="Name for this machine in session labels (skips interactive prompt when using --install)",
    ),
    menubar: bool = typer.Option(
        default_install_desktop_app(),
        "--menubar/--no-menubar",
        help="Install Longhouse.app in the macOS menu bar when available.",
    ),
) -> None:
    """Continuous: run the machine agent to watch and ship sessions via the Rust engine.

    Runtime behavior:
    - Uses OS file watching for near-real-time sync.
    - Runs a periodic reconciliation scan (default: 300s, configurable via --interval).

    Service management:
        --install    Install the background machine agent + Claude Code hooks
        --uninstall  Stop and remove the background machine agent
        --status     Check the status of the background machine agent

    Run 'longhouse auth' first to authenticate with Longhouse.
    """
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    del debounce  # accepted for backwards-compat but no longer wired

    config_dir = resolve_longhouse_home_from_provider_home(claude_dir) if claude_dir else None

    # --domain is shorthand for --url https://<domain>
    if isinstance(domain, str) and not url:
        url = f"https://{domain}"

    # Handle service management commands
    if status:
        _handle_status()
        return

    if uninstall:
        _handle_uninstall()
        return

    url = _resolve_configured_url(url, config_dir)
    if not token:
        token = load_token(config_dir)

    if hooks_only:
        typer.secho("--hooks-only is no longer supported. Use `longhouse connect --install`.", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    if install:
        if not token:
            typer.secho(
                "No device token found. Installing the local runtime without auth; "
                "run 'longhouse auth' later to enable remote shipping.",
                fg=typer.colors.YELLOW,
            )
        _handle_install(
            url=url,
            token=token,
            claude_dir=claude_dir,
            interval=interval,
            machine_name=machine_name,
            menubar=menubar,
        )
        return

    # Auto-authenticate if no token exists for active shipping.
    if not token:
        typer.echo("No device token found. Attempting auto-authentication...")
        auto_token = _auto_create_token(url)
        if auto_token:
            save_token(auto_token, config_dir)
            _persist_selected_url(url, claude_dir, written_by="connect")
            token = auto_token
            typer.secho("Authenticated successfully.", fg=typer.colors.GREEN)
        else:
            typer.secho(
                "Could not auto-authenticate. Run 'longhouse auth --token <token>' to authenticate manually.",
                fg=typer.colors.YELLOW,
            )
            raise typer.Exit(code=1)

    # Normal connect mode — exec longhouse-engine (replaces this process)

    # Persist canonical machine target + token before handing off to the engine.
    # This handles the case where explicit --url/--token were passed but
    # not yet written (auto-auth already persists on that path).
    save_token(token, config_dir)
    _persist_selected_url(url, claude_dir, written_by="connect")

    try:
        engine = get_engine_executable()
    except RuntimeError as e:
        typer.secho(str(e), fg=typer.colors.RED)
        raise typer.Exit(code=1)

    engine_args = [engine, "connect"]
    if interval != 300:
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
        help="Filter by provider (claude, codex, antigravity, gemini)",
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
        help="Device token (uses stored token if not specified)",
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
    config_dir = resolve_longhouse_home_from_provider_home(claude_dir) if claude_dir else None

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
    typer.echo(f"Machine Agent: {info.get('service_name', 'N/A')}")

    if status == "running":
        typer.echo("Status: running")
    elif status == "stopped":
        typer.echo("Status: stopped")
    else:
        typer.echo("Status: not installed")

    if status != "not-installed":
        typer.echo(f"Config: {info.get('service_file', 'N/A')}")
        typer.echo(f"Logs: {info['log_path']}")

    if detect_platform() == Platform.MACOS:
        menubar = get_desktop_app_service_info()
        typer.echo("")
        typer.echo(f"Desktop App: {menubar.get('service_name', 'N/A')}")
        menubar_status = menubar["status"]
        if menubar_status == "running":
            typer.echo("Status: running")
        elif menubar_status == "stopped":
            typer.echo("Status: stopped")
        else:
            typer.echo("Status: not installed")
        if menubar_status != "not-installed":
            typer.echo(f"Config: {menubar.get('service_file', 'N/A')}")
            typer.echo(f"Logs: {menubar['log_path']}")
            runtime_mode = menubar.get("runtime_mode")
            artifact_path = menubar.get("artifact_path")
            launch_path = menubar.get("launch_path")
            if artifact_path:
                typer.echo(f"App: {artifact_path}")
            if launch_path:
                typer.echo(f"Launch: {launch_path}")
            if runtime_mode == "source-build":
                version = menubar.get("bundle_version")
                detail = "Desktop App runtime: local source build"
                if version:
                    detail = f"{detail} ({version})"
                typer.echo(detail)
            elif runtime_mode == "broken-install":
                typer.echo("Desktop App runtime: install is missing, broken, or unsupported " "(run: longhouse machine repair)")


def _handle_uninstall() -> None:
    """Handle --uninstall flag."""
    try:
        result = uninstall_service()
        typer.secho(f"[OK] {result['message']}", fg=typer.colors.GREEN)
    except RuntimeError as e:
        typer.secho(f"[ERROR] {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    if detect_platform() == Platform.MACOS:
        menubar_result = uninstall_desktop_app_service()
        typer.secho(f"[OK] {menubar_result['message']}", fg=typer.colors.GREEN)


def _handle_install(
    url: str,
    token: str | None,
    claude_dir: str | None,
    interval: int,
    machine_name: str | None = None,
    menubar: bool = False,
) -> None:
    """Handle --install flag."""
    # Determine machine name — prompt interactively unless already provided.
    default_name = socket.gethostname()
    if machine_name:
        resolved_name = machine_name
    else:
        typer.echo("")
        resolved_name = (
            typer.prompt(
                "Name this machine (used to label your sessions in Longhouse)",
                default=default_name,
            ).strip()
            or default_name
        )
    typer.echo(f"  URL: {url}")
    if menubar:
        typer.echo("  Desktop App: enabled")

    try:
        reconcile_result = reconcile_local_runtime(
            token=token,
            claude_dir=claude_dir,
            written_by="connect-install",
            runtime_url=url,
            machine_name=resolved_name,
            menubar=menubar,
        )
    except RuntimeError as e:
        typer.secho(f"[ERROR] {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    except ValueError as e:
        typer.secho(f"[ERROR] {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    install_result = reconcile_result.install_result

    typer.echo("")
    typer.secho(f"  Machine: {install_result.machine_name}", fg=typer.colors.CYAN)
    if install_result.engine_runtime.installed_now:
        typer.secho(f"  [OK] Engine binary installed at {install_result.engine_runtime.path}", fg=typer.colors.GREEN)
    else:
        typer.secho(f"  [OK] Engine binary ready at {install_result.engine_runtime.path}", fg=typer.colors.GREEN)
    service_skipped = str(install_result.service_result.get("service") or "").strip().lower() == "skipped"
    typer.secho(
        f"[{'WARN' if service_skipped else 'OK'}] {install_result.service_result['message']}",
        fg=typer.colors.YELLOW if service_skipped else typer.colors.GREEN,
    )
    typer.echo(f"  Machine Agent: {install_result.service_result.get('service', 'N/A')}")
    service_config_path = install_result.service_result.get("plist_path") or install_result.service_result.get(
        "unit_path",
        "N/A",
    )
    typer.echo(f"  Config: {service_config_path}")

    typer.echo("")
    typer.echo("Installing CLI hooks (Claude Code + Codex)...")
    for action in install_result.hooks.actions:
        skipped = "skipped" in action.lower()
        typer.secho(
            f"  [{'WARN' if skipped else 'OK'}] {action}",
            fg=typer.colors.YELLOW if skipped else typer.colors.GREEN,
        )
    if install_result.hooks.warning:
        typer.secho(f"  [WARN] Hook installation failed: {install_result.hooks.warning}", fg=typer.colors.YELLOW)

    if install_result.desktop_app_result:
        desktop_skipped = bool(install_result.desktop_app_result.get("skipped"))
        typer.echo("")
        typer.echo("Longhouse.app:")
        typer.secho(
            f"  [{'WARN' if desktop_skipped else 'OK'}] {install_result.desktop_app_result['message']}",
            fg=typer.colors.YELLOW if desktop_skipped else typer.colors.GREEN,
        )
        typer.echo(f"  Config: {install_result.desktop_app_result.get('plist_path', 'N/A')}")
        if install_result.desktop_app_result.get("app_path"):
            typer.echo(f"  App: {install_result.desktop_app_result['app_path']}")
        launch_path = install_result.desktop_app_result.get("launch_path") or install_result.desktop_app_result.get(
            "binary_path",
            "N/A",
        )
        typer.echo("  Launch: " f"{launch_path}")

    # Verify PATH in a fresh shell
    _verify_and_warn_path()

    try:
        from zerg.cli.acquisition import emit_acquisition_event_once

        emit_acquisition_event_once(
            "machine_agent_installed",
            "machine_agent_first_install",
            command="connect_install",
            topology="machine_agent",
            props={
                "has_token": bool(token),
                "menubar": bool(menubar),
                "service": install_result.service_result.get("service"),
            },
        )
    except Exception:
        pass

    typer.echo("")
    typer.echo("To check status: longhouse connect --status")
    typer.echo("To stop machine agent: longhouse connect --uninstall")
