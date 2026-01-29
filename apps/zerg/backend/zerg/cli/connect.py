"""Connect command for shipping Claude Code sessions to Zerg.

Commands:
- ship: One-shot sync of all sessions
- connect: Continuous sync (watch mode or polling)

Watch mode (default): Uses file system events for sub-second sync.
Polling mode: Falls back to periodic scanning (--poll or --interval).
"""

from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path

import typer

from zerg.services.shipper import SessionShipper
from zerg.services.shipper import SessionWatcher
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
) -> None:
    """Continuous: watch and ship sessions to Zerg.

    By default uses file watching for sub-second sync.
    Use --poll or --interval for polling mode.
    """
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    config = ShipperConfig(
        zerg_api_url=url,
        claude_config_dir=Path(claude_dir) if claude_dir else None,
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
