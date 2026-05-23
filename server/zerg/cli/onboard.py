"""Default local quickstart for Longhouse.

Gets a developer machine into the launch-path state:
- local Runtime Host up
- Machine Agent installed when supported
- existing sessions imported when a supported CLI is present
- browser handoff to Longhouse
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
from click import Choice

from zerg.cli.config_file import get_config_path
from zerg.cli.config_file import load_config
from zerg.cli.config_file import save_loaded_config
from zerg.cli.serve import _get_longhouse_home
from zerg.cli.serve import _is_server_running
from zerg.services.local_runtime_installer import install_local_runtime
from zerg.services.runtime_artifacts import desktop_app_canonical_bundle_path
from zerg.services.shipper import load_token

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


def _allow_service_install_in_ci() -> bool:
    """Allow explicit service-manager install in CI when requested."""
    raw = os.getenv("LONGHOUSE_INSTALL_SERVICES_IN_CI")
    if not raw:
        return False
    return raw.strip().lower() not in {"0", "false", "no"}


def _open_longhouse_surface(api_url: str) -> None:
    """Open the human-facing Longhouse surface, preferring the macOS app."""
    if sys.platform == "darwin":
        app_bundle = desktop_app_canonical_bundle_path()
        if app_bundle.exists():
            typer.echo(f"  Opening Longhouse.app ({app_bundle})...")
            try:
                subprocess.run(
                    ["open", str(app_bundle)],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return
            except Exception:
                typer.echo(f"  Could not open Longhouse.app. Opening {api_url} instead...")

    typer.echo(f"  Opening {api_url}...")
    try:
        webbrowser.open(api_url)
    except Exception:
        typer.echo(f"  Could not open browser. Visit: {api_url}")


def _check_server_health(host: str = "127.0.0.1", port: int = 8080, timeout: float = 2.0) -> bool:
    """Check if server is responding to health checks."""
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(f"http://{host}:{port}/api/health")
            return response.status_code == 200
    except Exception:
        return False


def _check_server_health_at_url(url: str, timeout: float = 2.0) -> bool:
    """Check if server is responding at a given URL."""
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(f"{url.rstrip('/')}/api/health")
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


def _run_initial_import(api_url: str) -> tuple[bool, str]:
    """Run a one-shot import so existing sessions become visible immediately."""
    try:
        result = subprocess.run(
            ["longhouse", "ship", "--url", api_url],
            capture_output=True,
            text=True,
        )
    except Exception as e:
        return False, str(e)

    if result.returncode == 0:
        return True, ""

    detail = (result.stderr or "").strip() or (result.stdout or "").strip() or "longhouse ship exited non-zero"
    return False, detail


app = typer.Typer(help="Default local quickstart")


@app.command()
def onboard(
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Local runtime host",
    ),
    port: int = typer.Option(
        8080,
        "--port",
        "-p",
        help="Local runtime port",
    ),
    no_server: bool = typer.Option(
        False,
        "--no-server",
        help="Skip local runtime startup",
    ),
    no_shipper: bool = typer.Option(
        False,
        "--no-shipper",
        help="Skip machine-agent installation",
    ),
    no_browser: bool = typer.Option(
        False,
        "--no-browser",
        help="Skip auto-opening Longhouse at the end of onboarding.",
    ),
    remote_url: str = typer.Option(
        None,
        "--remote-url",
        help="Skip local runtime and connect to an existing Longhouse at this URL",
    ),
    topology: str | None = typer.Option(
        None,
        "--topology",
        help="Skip the interactive topology prompt: local or remote.",
        click_type=Choice(["local", "remote"], case_sensitive=False),
    ),
) -> None:
    """Run the default local quickstart or connect to existing Longhouse."""
    normalized_topology = topology.lower() if topology else None

    if remote_url and normalized_topology == "local":
        raise typer.BadParameter("--topology local cannot be combined with --remote-url.")

    if normalized_topology == "remote" and not remote_url and not sys.stdin.isatty():
        raise typer.BadParameter("--topology remote requires --remote-url in noninteractive mode.")

    typer.echo("")
    typer.secho("Welcome to Longhouse", fg=typer.colors.CYAN, bold=True)
    typer.echo("")

    # Topology choice: decide where the server runs (unless --remote-url specified)
    if remote_url or normalized_topology == "remote":
        # User specified --remote-url flag, use remote path directly
        if remote_url is None:
            remote_url = typer.prompt("URL of your Longhouse server", default="https://longhouse.example.com")
        api_url = remote_url
        skip_local_server = True
    elif normalized_topology == "local":
        api_url = _derive_client_url(host, port)
        skip_local_server = False
    else:
        # Interactive topology choice
        typer.echo("Where should your Longhouse server run?")
        typer.echo("")
        typer.echo("  1. Try on this Mac (localhost, trial-mode, works but stops when laptop sleeps)")
        typer.echo("  2. Connect to existing Longhouse (agent on this Mac, server on VPS/homelab/Mac mini)")
        typer.echo("")

        choice = typer.prompt("Choose", type=Choice(["1", "2"], case_sensitive=False), default="1")

        if choice == "2":
            # Remote server path
            remote_url = typer.prompt("URL of your Longhouse server", default="https://longhouse.example.com")
            api_url = remote_url
            skip_local_server = True
        else:
            # Local trial path (default)
            api_url = _derive_client_url(host, port)
            skip_local_server = False

    typer.echo("")
    typer.echo("Install Longhouse, open it, and find one prior session. " "Start Longhouse sessions later when you want control.")
    typer.echo("")

    # Step 1: Check dependencies
    typer.secho("Step 1: Checking dependencies", fg=typer.colors.BLUE, bold=True)
    typer.echo("")

    # Check supported AI CLIs
    has_claude = _has_command("claude")
    has_codex = _has_command("codex")
    has_antigravity = _has_command("agy")
    has_gemini = _has_command("gemini")
    has_any_cli = has_claude or has_codex or has_antigravity or has_gemini

    if has_claude:
        typer.secho("  [OK] Claude Code found", fg=typer.colors.GREEN)
    if has_codex:
        typer.secho("  [OK] Codex CLI found", fg=typer.colors.GREEN)
    if has_antigravity:
        typer.secho("  [OK] Antigravity CLI found", fg=typer.colors.GREEN)
    if has_gemini:
        typer.secho("  [OK] Legacy Gemini CLI found", fg=typer.colors.GREEN)

    if not has_any_cli:
        typer.secho("  [--] No supported AI CLI found", fg=typer.colors.YELLOW)
        typer.echo("")
        typer.echo("  Longhouse works with any of these CLI tools:")
        typer.echo("    - Claude Code  https://docs.anthropic.com/en/docs/claude-code/overview")
        typer.echo("    - Codex CLI    https://github.com/openai/codex")
        typer.echo("    - Antigravity  https://antigravity.google/product/antigravity-cli")
        typer.echo("")
        typer.echo("  You can still set up the local runtime now and connect a CLI later.")
        typer.echo("  You can also import sessions manually via JSONL upload.")

    # Check for existing config
    config_path = get_config_path()
    if config_path.exists():
        typer.secho(f"  [OK] Config found: {config_path}", fg=typer.colors.GREEN)
    else:
        typer.echo(f"  [--] No config file (will create: {config_path})")

    typer.echo("")

    # Step 2: Start the local runtime (or skip if remote)
    if skip_local_server:
        typer.secho("Step 2: Connect to existing Longhouse", fg=typer.colors.BLUE, bold=True)
        typer.echo("")
        typer.echo(f"  Connecting to {api_url}...")
        server_healthy = _check_server_health_at_url(api_url)
        if server_healthy:
            typer.secho(f"  [OK] Longhouse is responding at {api_url}", fg=typer.colors.GREEN)
        else:
            typer.secho(f"  [WARN] Could not reach {api_url}", fg=typer.colors.YELLOW)
            typer.echo("         Make sure your Longhouse server is running and accessible.")
    else:
        typer.secho("Step 2: Start the local runtime", fg=typer.colors.BLUE, bold=True)
        typer.echo("")

        if no_server:
            typer.echo("  Skipping local runtime startup (--no-server)")
        else:
            # Check if server is already running
            is_running, pid = _is_server_running()

            if is_running:
                typer.secho(f"  [OK] Local runtime already running (PID {pid})", fg=typer.colors.GREEN)
            elif _check_server_health(host, port):
                typer.secho(f"  [OK] Local runtime responding at {api_url}", fg=typer.colors.GREEN)
            else:
                typer.echo("  Starting local runtime...")

                try:
                    subprocess.run(
                        ["longhouse", "serve", "--daemon", "--host", host, "--port", str(port)],
                        check=True,
                        capture_output=True,
                    )

                    typer.echo("  Waiting for local runtime...")
                    if _wait_for_server(host, port, timeout=30):
                        typer.secho(f"  [OK] Local runtime ready at {api_url}", fg=typer.colors.GREEN)
                    else:
                        typer.secho("  [WARN] Local runtime started but not responding", fg=typer.colors.YELLOW)
                        typer.echo(f"         Check logs: {_get_longhouse_home() / 'server.log'}")
                except subprocess.CalledProcessError as e:
                    typer.secho(f"  [ERROR] Failed to start local runtime: {e}", fg=typer.colors.RED)
                    typer.echo("         Try starting manually: longhouse serve")

        server_healthy = no_server or _check_server_health(host, port)

    typer.echo("")

    # Step 3: Import existing sessions
    typer.secho("Step 3: Bring in your existing sessions", fg=typer.colors.BLUE, bold=True)
    typer.echo("")
    installed_desktop_app = False

    if no_shipper:
        typer.echo("  Skipping machine-agent setup (--no-shipper)")
    else:
        ci_service_install = _allow_service_install_in_ci()
        has_service_manager = (_has_launchd() or _has_systemd()) and (not os.getenv("CI") or ci_service_install)

        if has_service_manager:
            install_menubar = sys.platform == "darwin" and _has_gui() and (not os.getenv("CI") or ci_service_install)
            try:
                install_result = install_local_runtime(
                    url=api_url,
                    token=load_token(),
                    claude_dir=None,
                    machine_name=socket.gethostname(),
                    menubar=install_menubar,
                    written_by="onboard",
                )
                installed_desktop_app = install_menubar and bool(getattr(install_result, "desktop_app_result", None))
                typer.secho("  [OK] Machine agent installed for automatic imports", fg=typer.colors.GREEN)
                if install_result.hooks.warning:
                    typer.secho(
                        f"  [WARN] CLI hook install had issues: {install_result.hooks.warning}",
                        fg=typer.colors.YELLOW,
                    )
            except Exception as e:
                typer.secho(f"  [WARN] Could not install machine agent: {e}", fg=typer.colors.YELLOW)
                typer.echo("         Run manually: longhouse connect --install")
        else:
            typer.secho(
                "  [--] Background machine-agent install is not available in this environment",
                fg=typer.colors.YELLOW,
            )
            typer.echo("       Use: longhouse connect")
            typer.echo("       Or import once with: longhouse ship")

        if has_any_cli and server_healthy:
            typer.echo("  Importing your existing sessions now...")
            imported, detail = _run_initial_import(api_url)
            if imported:
                typer.secho("  [OK] Existing sessions are ready to look for in Longhouse", fg=typer.colors.GREEN)
            else:
                typer.secho("  [WARN] Initial import failed", fg=typer.colors.YELLOW)
                if detail:
                    typer.echo(f"         {detail}")
                typer.echo("         Retry with: longhouse ship")
        elif not has_any_cli:
            typer.echo("  No supported CLI found yet, so Longhouse skipped the initial import.")
            typer.echo("  Install Claude Code, Codex CLI, or Antigravity CLI later, then run: longhouse ship")
        else:
            typer.echo("  Skipping initial import (local runtime not running)")

    typer.echo("")

    # Step 4: Save config
    typer.secho("Step 4: Saving configuration", fg=typer.colors.BLUE, bold=True)
    typer.echo("")

    try:
        config = load_config(config_path=config_path)
        config.server.host = host
        config.server.port = port
        save_loaded_config(config, config_path=config_path)
        typer.secho(f"  [OK] Config saved: {config_path}", fg=typer.colors.GREEN)
    except Exception as e:
        typer.secho(f"  [WARN] Could not save config: {e}", fg=typer.colors.YELLOW)

    typer.echo("")

    # Step 5: Verify PATH in a fresh shell
    typer.secho("Step 5: PATH verification", fg=typer.colors.BLUE, bold=True)
    typer.echo("")

    path_warnings = verify_shell_path()
    if path_warnings:
        for warning in path_warnings:
            typer.secho(f"  [WARN] {warning}", fg=typer.colors.YELLOW)
    else:
        typer.secho("  [OK] PATH looks good for a fresh shell", fg=typer.colors.GREEN)

    typer.echo("")

    if not no_browser and _has_gui() and server_healthy:
        _open_longhouse_surface(api_url)

    typer.echo("")

    typer.echo("=" * 50)
    typer.secho("Setup complete!", fg=typer.colors.GREEN, bold=True)
    typer.echo("=" * 50)
    typer.echo("")
    typer.echo("First run:")
    if has_any_cli:
        typer.echo("  1. Open Longhouse")
        typer.echo("  2. Find one prior session in the timeline")
    else:
        typer.echo("  1. Open Longhouse")
        typer.echo("  2. Install Claude Code, Codex CLI, or Antigravity CLI when you want real imports")
    if installed_desktop_app:
        typer.echo("  3. Look for Longhouse.app in /Applications and your menu bar")
    typer.echo("")
    typer.echo("Next, when you want Longhouse-managed launch:")
    if has_claude:
        typer.echo("  longhouse claude   Start a Longhouse Claude session")
    if has_codex:
        typer.echo("  longhouse codex    Start a Longhouse Codex session")
    if has_antigravity:
        typer.echo("  longhouse antigravity Start a Longhouse Antigravity session")
    if not (has_claude or has_codex or has_antigravity):
        typer.echo("  Install Claude Code, Codex CLI, or Antigravity CLI, then start a Longhouse session")
    typer.echo("")
    typer.echo("Repair tools (only if you need them later):")
    typer.echo("  longhouse doctor            Diagnose local setup issues")
    typer.echo("  longhouse machine repair    Repair a configured machine agent, desktop app, and automatic imports")
    typer.echo("  longhouse connect --install First install or force reinstall the local runtime")
    typer.echo("")
    typer.echo("Advanced:")
    typer.echo("  longhouse ship              Import existing sessions once")
    typer.echo("  longhouse serve --demo      Start a safe preview instead of importing real work")
    typer.echo("  longhouse status            Show local health")
    typer.echo("  longhouse serve --stop      Stop local runtime")
    typer.echo("")


# Export for main.py registration
__all__ = ["onboard", "verify_shell_path", "_resolve_shell_bin", "_PATH_MARKER"]
