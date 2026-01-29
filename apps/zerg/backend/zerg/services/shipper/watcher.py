"""Real-time file watcher for Claude Code sessions.

Watches ~/.claude/projects/ for JSONL file changes and triggers
shipping with debouncing to handle rapid writes from streaming.

From VISION.md:
> "Magic moment: user types in Claude Code -> shipper fires ->
>  session appears in Zerg before they switch tabs."
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

if TYPE_CHECKING:
    from zerg.services.shipper.shipper import SessionShipper

logger = logging.getLogger(__name__)


class SessionFileHandler(FileSystemEventHandler):
    """Handles file system events for session JSONL files.

    Debounces rapid writes (Claude streams to file) and triggers
    ship on quiet period.
    """

    def __init__(
        self,
        on_change: Callable[[Path], None],
        debounce_seconds: float = 0.5,
    ):
        """Initialize the handler.

        Args:
            on_change: Callback when a file has changed (after debounce)
            debounce_seconds: Wait this long after last write before triggering
        """
        super().__init__()
        self.on_change = on_change
        self.debounce_seconds = debounce_seconds
        self._pending: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def on_modified(self, event):
        """Handle file modification events."""
        if event.is_directory:
            return

        # Only handle .jsonl files
        path = Path(event.src_path)
        if path.suffix != ".jsonl":
            return

        self._debounce(path)

    def _debounce(self, path: Path) -> None:
        """Debounce file changes, calling on_change after quiet period."""
        path_str = str(path)

        with self._lock:
            # Cancel any pending timer for this file
            if path_str in self._pending:
                self._pending[path_str].cancel()

            # Schedule new callback
            timer = threading.Timer(
                self.debounce_seconds,
                self._fire_change,
                args=[path],
            )
            timer.daemon = True
            timer.start()
            self._pending[path_str] = timer

    def _fire_change(self, path: Path) -> None:
        """Fire the change callback."""
        path_str = str(path)

        with self._lock:
            # Remove from pending
            self._pending.pop(path_str, None)

        logger.debug(f"File change debounced: {path.name}")
        try:
            self.on_change(path)
        except Exception as e:
            logger.error(f"Error in change callback for {path.name}: {e}")

    def cancel_all(self) -> None:
        """Cancel all pending timers."""
        with self._lock:
            for timer in self._pending.values():
                timer.cancel()
            self._pending.clear()


class SessionWatcher:
    """Watches Claude session directories for changes.

    Usage:
        watcher = SessionWatcher(shipper)
        await watcher.start()
        # ... runs until stopped ...
        await watcher.stop()
    """

    def __init__(
        self,
        shipper: SessionShipper,
        debounce_ms: int = 500,
        fallback_scan_interval: int = 300,  # 5 minutes
    ):
        """Initialize the watcher.

        Args:
            shipper: SessionShipper instance to use for shipping
            debounce_ms: Debounce period in milliseconds
            fallback_scan_interval: Seconds between fallback scans (0 to disable)
        """
        self.shipper = shipper
        self.debounce_seconds = debounce_ms / 1000.0
        self.fallback_scan_interval = fallback_scan_interval
        self._observer: Observer | None = None
        self._handler: SessionFileHandler | None = None
        self._stop_event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._fallback_task: asyncio.Task | None = None
        self._pending_ships: asyncio.Queue[Path] = asyncio.Queue()
        self._ship_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start watching for file changes."""
        projects_dir = self.shipper.config.projects_dir

        if not projects_dir.exists():
            logger.warning(f"Projects directory does not exist: {projects_dir}")
            # Create it so watcher can still function
            projects_dir.mkdir(parents=True, exist_ok=True)

        self._loop = asyncio.get_event_loop()
        self._stop_event = asyncio.Event()

        # Create handler that queues paths for async shipping
        self._handler = SessionFileHandler(
            on_change=self._queue_ship,
            debounce_seconds=self.debounce_seconds,
        )

        # Set up file system observer
        self._observer = Observer()
        self._observer.schedule(
            self._handler,
            str(projects_dir),
            recursive=True,
        )
        self._observer.start()

        logger.info(f"Watching {projects_dir} for session changes")

        # Start the async ship processor
        self._ship_task = asyncio.create_task(self._ship_processor())

        # Start fallback scan if enabled
        if self.fallback_scan_interval > 0:
            self._fallback_task = asyncio.create_task(self._fallback_scanner())

        # Do an initial scan
        logger.info("Running initial scan...")
        try:
            result = await self.shipper.scan_and_ship()
            if result.events_shipped > 0:
                logger.info(f"Initial scan: shipped {result.events_shipped} events " f"from {result.sessions_shipped} sessions")
        except Exception as e:
            logger.error(f"Initial scan failed: {e}")

    async def stop(self) -> None:
        """Stop watching and clean up."""
        if self._stop_event:
            self._stop_event.set()

        # Cancel handler timers
        if self._handler:
            self._handler.cancel_all()

        # Stop observer
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5.0)

        # Cancel tasks
        if self._fallback_task:
            self._fallback_task.cancel()
            try:
                await self._fallback_task
            except asyncio.CancelledError:
                pass

        if self._ship_task:
            self._ship_task.cancel()
            try:
                await self._ship_task
            except asyncio.CancelledError:
                pass

        logger.info("Watcher stopped")

    def _queue_ship(self, path: Path) -> None:
        """Queue a path for shipping (called from watchdog thread)."""
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(
                self._pending_ships.put_nowait,
                path,
            )

    async def _ship_processor(self) -> None:
        """Process queued ship requests."""
        while True:
            try:
                path = await self._pending_ships.get()

                # Check if file still exists
                if not path.exists():
                    logger.debug(f"File no longer exists: {path}")
                    continue

                logger.info(f"Shipping session: {path.name}")

                try:
                    result = await self.shipper.ship_session(path)
                    if result["events_inserted"] > 0:
                        logger.info(f"Shipped {result['events_inserted']} events " f"(skipped {result['events_skipped']} duplicates)")
                except Exception as e:
                    logger.error(f"Failed to ship {path.name}: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Ship processor error: {e}")

    async def _fallback_scanner(self) -> None:
        """Periodic fallback scan to catch missed events."""
        while True:
            try:
                await asyncio.sleep(self.fallback_scan_interval)

                if self._stop_event and self._stop_event.is_set():
                    break

                logger.debug("Running fallback scan...")
                result = await self.shipper.scan_and_ship()

                if result.events_shipped > 0:
                    logger.info(f"Fallback scan: shipped {result.events_shipped} events " f"from {result.sessions_shipped} sessions")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Fallback scan error: {e}")
