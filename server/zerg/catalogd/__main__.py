"""Run the isolated Longhouse catalog daemon."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from contextlib import suppress
from pathlib import Path

import zerg.bootstrap_sqlite  # noqa: F401  # pin sqlite before SQLAlchemy imports


async def _run(database_path: Path, socket_path: Path) -> None:
    from zerg.catalogd.server import CatalogDaemon

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    stop_requested = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop_requested.set)
    await daemon.start()
    serve_task = asyncio.create_task(daemon.serve_forever(), name="catalogd-serve")
    stop_task = asyncio.create_task(stop_requested.wait(), name="catalogd-stop")
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


def _serve_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--socket", type=Path, required=True)
    return parser


async def _request_backup(args: argparse.Namespace) -> dict[str, object]:
    from zerg.catalogd.client import CatalogClient

    client = CatalogClient(args.socket)
    try:
        return await client.call(
            "backup.snapshot.create.v2",
            {"output_dir": str(args.output.expanduser().resolve()), "data_root": str(args.data_root.expanduser().resolve())},
            timeout_seconds=args.timeout,
        )
    finally:
        await client.close()


def _operator_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m zerg.catalogd", description="Catalog backup and restore proof commands")
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup", help="publish an exact online restore point through catalogd")
    backup.add_argument("--socket", type=Path, required=True)
    backup.add_argument("--data-root", type=Path, required=True, help="root containing raw/ and media/ objects")
    backup.add_argument("--output", type=Path, required=True, help="empty/new restore-point directory")
    backup.add_argument("--timeout", type=float, default=3_600.0)
    verify = commands.add_parser("verify", help="verify a published catalog snapshot and object set")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--data-root", type=Path, required=True)
    restore = commands.add_parser("restore-rehearsal", help="copy critical files into a blank root and verify them")
    restore.add_argument("--manifest", type=Path, required=True)
    restore.add_argument("--source-data-root", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)
    return parser


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in {"backup", "verify", "restore-rehearsal"}:
        args = _operator_parser().parse_args()
        from zerg.catalogd.backup import restore_rehearsal
        from zerg.catalogd.backup import verify_restore_point

        if args.command == "backup":
            result = asyncio.run(_request_backup(args))
        elif args.command == "verify":
            result = verify_restore_point(manifest_path=args.manifest, data_root=args.data_root)
        else:
            result = restore_rehearsal(
                manifest_path=args.manifest,
                source_data_root=args.source_data_root,
                destination_root=args.destination,
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    args = _serve_parser().parse_args()
    try:
        asyncio.run(_run(args.database, args.socket))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
