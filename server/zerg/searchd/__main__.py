"""Run the isolated disposable Longhouse search daemon."""

from __future__ import annotations

import argparse
import asyncio
import signal
from contextlib import suppress
from pathlib import Path

import zerg.bootstrap_sqlite  # noqa: F401  # pin sqlite before search DB imports


async def _run(database_path: Path, socket_path: Path) -> None:
    from zerg.searchd.server import SearchDaemon

    daemon = SearchDaemon(database_path=database_path, socket_path=socket_path)
    stop_requested = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop_requested.set)
    await daemon.start()
    serve_task = asyncio.create_task(daemon.serve_forever(), name="searchd-serve")
    stop_task = asyncio.create_task(stop_requested.wait(), name="searchd-stop")
    try:
        done, _pending = await asyncio.wait({serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if serve_task in done:
            await serve_task
    finally:
        for task in (serve_task, stop_task):
            task.cancel()
        for task in (serve_task, stop_task):
            with suppress(asyncio.CancelledError):
                await task
        await daemon.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--socket", type=Path, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(_run(args.database, args.socket))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
