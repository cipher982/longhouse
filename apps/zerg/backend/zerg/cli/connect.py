"""Connect command for shipping Claude Code sessions to Zerg.

Commands:
- ship: One-shot sync of all sessions
- connect: Continuous polling daemon
"""

from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path

import typer

from zerg.services.shipper import SessionShipper
from zerg.services.shipper import ShipperConfig
from zerg.services.shipper import ShipResult

app = typer.Typer(help="Ship Claude Code sessions to Zerg")

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


@app.command()
def ship(
    url: str = typer.Option(
        "http://localhost:47300",
        "--url",
        "-u",
        help="Zerg API URL",
    ),
    token: str = typer.Option(
        None,
        "--token",
        "-t",
        envvar="AGENTS_API_TOKEN",
        help="API token for authentication (or set AGENTS_API_TOKEN env var)",
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

    config = ShipperConfig(
        zerg_api_url=url,
        claude_config_dir=Path(claude_dir) if claude_dir else None,
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
        "http://localhost:47300",
        "--url",
        "-u",
        help="Zerg API URL",
    ),
    token: str = typer.Option(
        None,
        "--token",
        "-t",
        envvar="AGENTS_API_TOKEN",
        help="API token for authentication (or set AGENTS_API_TOKEN env var)",
    ),
    interval: int = typer.Option(
        30,
        "--interval",
        "-i",
        help="Polling interval in seconds",
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
    """Continuous: poll and ship sessions to Zerg."""
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    config = ShipperConfig(
        zerg_api_url=url,
        claude_config_dir=Path(claude_dir) if claude_dir else None,
        scan_interval_seconds=interval,
        api_token=token,
    )

    typer.echo(f"Connecting to {url}...")
    typer.echo(f"Claude config: {config.claude_config_dir}")
    typer.echo(f"Polling every {interval}s")
    typer.echo("Press Ctrl+C to stop")
    typer.echo("")

    # Run the polling loop
    try:
        asyncio.run(_polling_loop(config))
    except KeyboardInterrupt:
        typer.echo("")
        typer.secho("Stopped", fg=typer.colors.YELLOW)


async def _ship_once(config: ShipperConfig) -> ShipResult:
    """Ship sessions once."""

    shipper = SessionShipper(config=config)
    return await shipper.scan_and_ship()


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

    iteration = 0
    while not stop_event.is_set():
        iteration += 1
        logger.info(f"[{iteration}] Scanning for new sessions...")

        try:
            result = await shipper.scan_and_ship()

            if result.events_shipped > 0:
                logger.info(f"[{iteration}] Shipped {result.events_shipped} events " f"from {result.sessions_shipped} sessions")
            else:
                logger.debug(f"[{iteration}] No new events")

            if result.errors:
                for error in result.errors:
                    logger.error(f"[{iteration}] {error}")

        except Exception as e:
            logger.error(f"[{iteration}] Error during scan: {e}")

        # Wait for next interval or stop signal
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=config.scan_interval_seconds)
        except asyncio.TimeoutError:
            pass  # Continue to next iteration
