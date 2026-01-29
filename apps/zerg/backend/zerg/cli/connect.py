"""Connect command for shipping Claude Code sessions to Zerg.

Commands:
- auth: Authenticate with Zerg and obtain a device token
- ship: One-shot sync of all sessions
- connect: Continuous sync (watch mode or polling)
- connect --install: Install as background service
- connect --uninstall: Remove background service
- connect --status: Check service status

Watch mode (default): Uses file system events for sub-second sync.
Polling mode: Falls back to periodic scanning (--poll or --interval).
"""

from __future__ import annotations

import asyncio
import logging
import signal
import socket
import webbrowser
from pathlib import Path

import httpx
import typer

from zerg.services.shipper import SessionShipper
from zerg.services.shipper import SessionWatcher
from zerg.services.shipper import ShipperConfig
from zerg.services.shipper import ShipResult
from zerg.services.shipper import clear_token
from zerg.services.shipper import get_service_info
from zerg.services.shipper import get_zerg_url
from zerg.services.shipper import install_service
from zerg.services.shipper import load_token
from zerg.services.shipper import save_token
from zerg.services.shipper import save_zerg_url
from zerg.services.shipper import uninstall_service

app = typer.Typer(help="Ship Claude Code sessions to Zerg")

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


@app.command()
def auth(
    url: str = typer.Option(
        None,
        "--url",
        "-u",
        help="Zerg API URL (e.g., https://api.swarmlet.com)",
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
    """Authenticate with Zerg and store a device token.

    The token is stored locally and used for all subsequent ship/connect commands.

    Authentication methods:
    1. Browser-based: Opens Zerg web UI to create a token interactively
    2. Direct token: Use --token to provide an existing device token

    Examples:
        zerg auth --url https://api.swarmlet.com
        zerg auth --token zdt_your_token_here
        zerg auth --clear
    """
    config_dir = Path(claude_dir) if claude_dir else None

    # Handle --clear
    if clear:
        cleared_token = clear_token(config_dir)
        if cleared_token:
            typer.secho("Cleared stored token", fg=typer.colors.GREEN)
        else:
            typer.echo("No token to clear")
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
            url = typer.prompt("Zerg API URL", default="https://api.swarmlet.com")

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

    # Interactive auth flow
    typer.echo("")
    typer.echo("To create a device token:")
    typer.echo(f"1. Open: {url.rstrip('/')}/dashboard/settings/devices")
    typer.echo(f"2. Create a new token for device: {device_name}")
    typer.echo("3. Copy the token and paste it below")
    typer.echo("")

    # Try to open browser
    dashboard_url = f"{url.rstrip('/')}/dashboard/settings/devices"
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
        help="Zerg API URL (uses stored URL if not specified)",
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
        "-d",
        help="Claude config directory (default: ~/.claude)",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable verbose output",
    ),
) -> None:
    """One-shot: ship all new Claude Code sessions to Zerg."""
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    config_dir = Path(claude_dir) if claude_dir else None

    # Load stored credentials if not provided
    if not url:
        url = get_zerg_url(config_dir) or "http://localhost:47300"
    if not token:
        token = load_token(config_dir)

    config = ShipperConfig(
        zerg_api_url=url,
        claude_config_dir=config_dir,
        api_token=token,
    )

    typer.echo(f"Shipping sessions to {url}...")
    typer.echo(f"Claude config: {config.claude_config_dir}")

    result = asyncio.run(_ship_once(config))

    typer.echo("")
    typer.echo(f"Sessions scanned: {result.sessions_scanned}")
    typer.echo(f"Sessions shipped: {result.sessions_shipped}")
    typer.echo(f"Events shipped: {result.events_shipped}")
    typer.echo(f"Events skipped (duplicates): {result.events_skipped}")

    if result.errors:
        typer.echo("")
        typer.secho(f"Errors ({len(result.errors)}):", fg=typer.colors.RED)
        for error in result.errors:
            typer.echo(f"  - {error}")
        raise typer.Exit(code=1)
    else:
        typer.secho("✓ Done", fg=typer.colors.GREEN)


@app.command()
def connect(
    url: str = typer.Option(
        None,
        "--url",
        "-u",
        help="Zerg API URL (uses stored URL if not specified)",
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
        help="Install shipper as a background service (auto-starts on boot)",
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
    """Continuous: watch and ship sessions to Zerg.

    By default uses file watching for sub-second sync.
    Use --poll or --interval for polling mode.

    Service management:
        --install   Install as background service (auto-starts on boot)
        --uninstall Stop and remove the background service
        --status    Check the status of the background service

    Run 'zerg auth' first to authenticate with Zerg.
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
        url = get_zerg_url(config_dir) or "http://localhost:47300"
    if not token:
        token = load_token(config_dir)

    if install:
        # --interval implies --poll for install mode
        use_poll = poll or interval != 30
        _handle_install(url=url, token=token, claude_dir=claude_dir, poll=use_poll, interval=interval)
        return

    # Normal connect mode - run in foreground
    config = ShipperConfig(
        zerg_api_url=url,
        claude_config_dir=config_dir,
        scan_interval_seconds=interval,
        api_token=token,
    )

    # Determine mode: if interval was explicitly set (not default), use polling
    use_polling = poll or interval != 30

    typer.echo(f"Connecting to {url}...")
    typer.echo(f"Claude config: {config.claude_config_dir}")

    if use_polling:
        typer.echo(f"Mode: polling every {interval}s")
    else:
        typer.echo(f"Mode: file watching (debounce: {debounce}ms)")

    typer.echo("Press Ctrl+C to stop")
    typer.echo("")

    # Run the appropriate loop
    try:
        if use_polling:
            asyncio.run(_polling_loop(config))
        else:
            asyncio.run(_watch_loop(config, debounce_ms=debounce))
    except KeyboardInterrupt:
        typer.echo("")
        typer.secho("Stopped", fg=typer.colors.YELLOW)


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


def _handle_install(
    url: str,
    token: str | None,
    claude_dir: str | None,
    poll: bool,
    interval: int,
) -> None:
    """Handle --install flag."""
    typer.echo("Installing shipper service...")
    typer.echo(f"  URL: {url}")
    typer.echo(f"  Mode: {'polling' if poll else 'watch'}")

    try:
        result = install_service(
            url=url,
            token=token,
            claude_dir=claude_dir,
            poll_mode=poll,
            interval=interval,
        )
        typer.echo("")
        typer.secho(f"[OK] {result['message']}", fg=typer.colors.GREEN)
        typer.echo(f"  Service: {result.get('service', 'N/A')}")
        typer.echo(f"  Config: {result.get('plist_path') or result.get('unit_path', 'N/A')}")
        typer.echo("")
        typer.echo("To check status: zerg connect --status")
        typer.echo("To stop service: zerg connect --uninstall")
    except RuntimeError as e:
        typer.secho(f"[ERROR] {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1)


async def _ship_once(config: ShipperConfig) -> ShipResult:
    """Ship sessions once."""
    shipper = SessionShipper(config=config)
    return await shipper.scan_and_ship()


async def _watch_loop(config: ShipperConfig, debounce_ms: int = 500) -> None:
    """Run the file watching loop."""
    shipper = SessionShipper(config=config)
    watcher = SessionWatcher(
        shipper,
        debounce_ms=debounce_ms,
        fallback_scan_interval=300,  # 5 minute fallback scan
    )

    # Handle graceful shutdown
    stop_event = asyncio.Event()

    def handle_signal(sig):
        logger.info(f"Received signal {sig}, shutting down...")
        stop_event.set()

    # Register signal handlers
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda s=sig: handle_signal(s))
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    # Start the watcher
    await watcher.start()

    # Check for pending spool items
    pending = shipper.spool.pending_count()
    if pending > 0:
        logger.info(f"Found {pending} spooled events, attempting replay...")
        replay_result = await shipper.replay_spool()
        if replay_result["replayed"] > 0:
            logger.info(f"Replayed {replay_result['replayed']} spooled events")
        if replay_result["remaining"] > 0:
            logger.warning(f"{replay_result['remaining']} events still pending in spool")

    # Start background spool replay task
    replay_task = asyncio.create_task(_spool_replay_loop(shipper, stop_event))

    try:
        # Wait for stop signal
        await stop_event.wait()
    finally:
        # Stop the watcher
        await watcher.stop()
        replay_task.cancel()
        try:
            await replay_task
        except asyncio.CancelledError:
            pass


async def _spool_replay_loop(shipper: SessionShipper, stop_event: asyncio.Event) -> None:
    """Background task to periodically replay spooled events."""
    while not stop_event.is_set():
        try:
            await asyncio.sleep(30)  # Check every 30 seconds

            if stop_event.is_set():
                break

            pending = shipper.spool.pending_count()
            if pending > 0:
                logger.debug(f"Replaying {pending} spooled events...")
                result = await shipper.replay_spool()
                if result["replayed"] > 0:
                    logger.info(f"Replayed {result['replayed']} events from spool")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Spool replay error: {e}")


async def _polling_loop(config: ShipperConfig) -> None:
    """Run the polling loop."""
    shipper = SessionShipper(config=config)

    # Handle graceful shutdown
    stop_event = asyncio.Event()

    def handle_signal(sig):
        logger.info(f"Received signal {sig}, shutting down...")
        stop_event.set()

    # Register signal handlers
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda s=sig: handle_signal(s))
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    # Check for pending spool items
    pending = shipper.spool.pending_count()
    if pending > 0:
        logger.info(f"Found {pending} spooled events, attempting replay...")
        replay_result = await shipper.replay_spool()
        if replay_result["replayed"] > 0:
            logger.info(f"Replayed {replay_result['replayed']} spooled events")

    iteration = 0
    while not stop_event.is_set():
        iteration += 1
        logger.info(f"[{iteration}] Scanning for new sessions...")

        try:
            result = await shipper.scan_and_ship()

            if result.events_shipped > 0:
                logger.info(f"[{iteration}] Shipped {result.events_shipped} events " f"from {result.sessions_shipped} sessions")
            elif result.events_spooled > 0:
                logger.warning(f"[{iteration}] Spooled {result.events_spooled} events " f"(API unreachable)")
            else:
                logger.debug(f"[{iteration}] No new events")

            if result.errors:
                for error in result.errors:
                    logger.error(f"[{iteration}] {error}")

            # Try to replay spool every few iterations
            if iteration % 3 == 0:
                pending = shipper.spool.pending_count()
                if pending > 0:
                    replay_result = await shipper.replay_spool()
                    if replay_result["replayed"] > 0:
                        logger.info(f"[{iteration}] Replayed {replay_result['replayed']} " f"events from spool")

        except Exception as e:
            logger.error(f"[{iteration}] Error during scan: {e}")

        # Wait for next interval or stop signal
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=config.scan_interval_seconds)
        except asyncio.TimeoutError:
            pass  # Continue to next iteration
