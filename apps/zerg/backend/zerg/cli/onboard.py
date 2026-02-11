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

import logging
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

logger = logging.getLogger(__name__)


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


def _get_shell_profile_path() -> Path | None:
    """Return the primary shell profile path for the current user's shell.

    Inspects ``$SHELL`` to determine which RC file a fresh interactive
    shell would source.  Returns ``None`` when the shell is unknown.
    """
    shell_name = os.path.basename(os.environ.get("SHELL", ""))
    home = Path.home()

    if shell_name == "zsh":
        return home / ".zshrc"
    elif shell_name == "bash":
        if sys.platform == "darwin":
            return home / ".bash_profile"
        return home / ".bashrc"
    elif shell_name == "fish":
        return home / ".config" / "fish" / "config.fish"
    return None


_ALLOWED_SHELLS = {"bash", "zsh", "fish"}

# Unique marker to isolate PATH from noisy profile output
_PATH_MARKER = "__LH_PATH__"


def _resolve_shell_bin() -> tuple[str, str] | None:
    """Return (absolute_shell_path, shell_basename) for the user's $SHELL.

    Uses the absolute path from ``$SHELL`` after allowlisting the basename.
    Falls back to ``/bin/zsh`` then ``/bin/bash`` when ``$SHELL`` is unknown.
    Returns ``None`` when no usable shell can be found.
    """
    shell_env = os.environ.get("SHELL", "")
    basename = os.path.basename(shell_env)

    if basename in _ALLOWED_SHELLS and os.path.isfile(shell_env):
        return (shell_env, basename)

    # Fallback: try common system shells
    for fallback in ("/bin/zsh", "/bin/bash"):
        if os.path.isfile(fallback):
            return (fallback, os.path.basename(fallback))

    return None


def _extract_path_from_profile(profile_path: Path) -> str | None:
    """Source a shell profile in a subshell and return the resulting PATH.

    This simulates what a fresh interactive terminal would see, without
    inheriting the current process' PATH modifications.

    Robustness measures:
    - Uses the absolute shell path from ``$SHELL`` (handles Homebrew shells).
    - Passes the profile path as a positional argument to avoid injection.
    - Uses ``-i`` (interactive) mode so rc files don't early-return.
    - Prints a unique marker line and parses only that (avoids stdout noise).
    - Gates the marker print on successful sourcing.

    Returns ``None`` when the subshell exits with an error or times out.
    """
    if not profile_path.exists():
        return None

    resolved = _resolve_shell_bin()
    if resolved is None:
        return None
    shell_bin, shell_name = resolved

    try:
        if shell_name == "fish":
            # Fish: pass profile as $argv[1], gate on source success,
            # use unique marker to avoid stdout contamination.
            result = subprocess.run(
                [
                    shell_bin,
                    "-c",
                    'source $argv[1] 2>/dev/null; and echo "' + _PATH_MARKER + '=$PATH"',
                    "--",
                    str(profile_path),
                ],
                capture_output=True,
                text=True,
                timeout=5,
                env={"HOME": str(Path.home()), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )
        else:
            # bash/zsh: use -i for interactive mode so .zshrc/.bashrc don't
            # early-return.  Profile path passed as $1 (positional arg).
            result = subprocess.run(
                [
                    shell_bin,
                    "-i",
                    "-c",
                    'source "$1" 2>/dev/null && echo "' + _PATH_MARKER + '=$PATH" || exit 1',
                    "_",  # $0 placeholder
                    str(profile_path),
                ],
                capture_output=True,
                text=True,
                timeout=5,
                env={"HOME": str(Path.home()), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )

        if result.returncode == 0:
            # Parse only the marker line from stdout
            for line in result.stdout.splitlines():
                if line.startswith(_PATH_MARKER + "="):
                    path_value = line[len(_PATH_MARKER) + 1 :]
                    if path_value:
                        return path_value
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("Failed to extract PATH from %s: %s", profile_path, exc)

    return None


def verify_shell_path() -> list[str]:
    """Verify that ``longhouse`` and ``claude`` are on PATH in a fresh shell.

    Simulates a fresh shell by sourcing the user's profile in a
    subprocess with a minimal base PATH, then checks whether the
    ``longhouse`` binary (and optionally ``claude``) would be
    reachable.

    Returns:
        A list of warning/fix strings (empty if everything looks good).
        Each string is a human-readable message suitable for printing.
    """
    warnings: list[str] = []

    profile = _get_shell_profile_path()
    if profile is None:
        # Unknown shell -- skip the check silently
        return warnings

    fresh_path = _extract_path_from_profile(profile)
    if fresh_path is None:
        # Could not source profile -- skip silently
        return warnings

    # Split PATH into directories
    path_dirs = fresh_path.split(":") if ":" in fresh_path else fresh_path.split(" ")

    # --- Check longhouse ---
    longhouse_bin = shutil.which("longhouse")
    if longhouse_bin:
        longhouse_dir = str(Path(longhouse_bin).parent)
        if longhouse_dir not in path_dirs:
            shell_name = os.path.basename(os.environ.get("SHELL", ""))
            warnings.append(f"'longhouse' is installed at {longhouse_bin} but won't be on PATH in a new terminal.")
            if shell_name == "fish":
                warnings.append(f"  Fix: fish_add_path {longhouse_dir}")
            else:
                warnings.append(f"  Fix: echo 'export PATH=\"{longhouse_dir}:$PATH\"' >> {profile}")
            warnings.append(f"  Then: source {profile}")

    # --- Check claude ---
    claude_bin = shutil.which("claude")
    if claude_bin:
        claude_dir = str(Path(claude_bin).parent)
        if claude_dir not in path_dirs:
            shell_name = os.path.basename(os.environ.get("SHELL", ""))
            warnings.append(f"'claude' is installed at {claude_bin} but won't be on PATH in a new terminal.")
            if shell_name == "fish":
                warnings.append(f"  Fix: fish_add_path {claude_dir}")
            else:
                warnings.append(f"  Fix: echo 'export PATH=\"{claude_dir}:$PATH\"' >> {profile}")
            warnings.append(f"  Then: source {profile}")

    return warnings


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
    no_demo: bool = typer.Option(
        False,
        "--no-demo",
        help="Skip seeding demo sessions",
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

    # Check supported AI CLIs
    has_claude = _has_command("claude")
    has_codex = _has_command("codex")
    has_gemini = _has_command("gemini")
    has_any_cli = has_claude or has_codex or has_gemini

    if has_claude:
        typer.secho("  [OK] Claude Code found", fg=typer.colors.GREEN)
    if has_codex:
        typer.secho("  [OK] Codex CLI found", fg=typer.colors.GREEN)
    if has_gemini:
        typer.secho("  [OK] Gemini CLI found", fg=typer.colors.GREEN)

    if not has_any_cli:
        typer.secho("  [--] No supported AI CLI found", fg=typer.colors.YELLOW)
        typer.echo("")
        typer.echo("  Longhouse works with any of these CLI tools:")
        typer.echo("    - Claude Code  https://docs.anthropic.com/en/docs/claude-code/overview")
        typer.echo("    - Codex CLI    https://github.com/openai/codex")
        typer.echo("    - Gemini CLI   https://github.com/google-gemini/gemini-cli")
        typer.echo("")
        typer.echo("  You can still set up the server now and connect a CLI later.")
        typer.echo("  You can also import sessions manually via JSONL upload.")

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
    elif not has_any_cli:
        typer.echo("  Skipping shipper (no supported CLI installed)")
        typer.echo("  Install a CLI first, then run: longhouse connect --install")
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

    # Step 5: Seed demo sessions
    typer.secho("Step 5: Demo data", fg=typer.colors.BLUE, bold=True)
    typer.echo("")

    if no_demo:
        typer.echo("  Skipping demo data (--no-demo)")
    elif _check_server_health(host, port):
        typer.echo("  Seeding demo sessions...")
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.post(f"{api_url}/api/agents/demo")
                if resp.status_code == 200:
                    result = resp.json()
                    if result.get("seeded"):
                        typer.secho(
                            f"  [OK] Seeded {result['sessions_created']} demo sessions",
                            fg=typer.colors.GREEN,
                        )
                        typer.echo("       Run 'longhouse ship' to sync your real sessions.")
                    else:
                        typer.secho("  [OK] Demo sessions already present", fg=typer.colors.GREEN)
                else:
                    typer.secho("  [WARN] Could not seed demo data", fg=typer.colors.YELLOW)
        except Exception as e:
            typer.secho(f"  [WARN] Demo seeding failed: {e}", fg=typer.colors.YELLOW)
    else:
        typer.echo("  Skipping demo data (server not running)")

    typer.echo("")

    # Step 6: Save config
    typer.secho("Step 6: Saving configuration", fg=typer.colors.BLUE, bold=True)
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

    # Step 7: Verify PATH in a fresh shell
    typer.secho("Step 7: PATH verification", fg=typer.colors.BLUE, bold=True)
    typer.echo("")

    path_warnings = verify_shell_path()
    if path_warnings:
        for warning in path_warnings:
            typer.secho(f"  [WARN] {warning}", fg=typer.colors.YELLOW)
    else:
        typer.secho("  [OK] PATH looks good for a fresh shell", fg=typer.colors.GREEN)

    typer.echo("")

    # Step 8: Open browser (if GUI available)
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
__all__ = ["onboard", "verify_shell_path", "_resolve_shell_bin", "_PATH_MARKER"]
