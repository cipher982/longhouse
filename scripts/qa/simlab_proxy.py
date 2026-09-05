#!/usr/bin/env python3
"""Loopback-only TCP relay for simulator network-loss experiments.

An existing --offline-file drops active and new client connections. Removing it
restores connectivity without restarting the app or the upstream Runtime Host.
This models transport loss, not cellular hardware or iOS background suspension.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path


class NetworkRelay:
    def __init__(self, upstream_port: int, offline_file: Path) -> None:
        self.upstream_port = upstream_port
        self.offline_file = offline_file
        self.connections: set[asyncio.StreamWriter] = set()

    async def pump(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()

    async def accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream = None
        tasks = []
        self.connections.add(writer)
        try:
            if self.offline_file.exists():
                return
            upstream_reader, upstream = await asyncio.open_connection("127.0.0.1", self.upstream_port)
            self.connections.add(upstream)
            # The gate may have changed while the upstream connection opened.
            if self.offline_file.exists():
                return
            tasks = [
                asyncio.create_task(self.pump(reader, upstream)),
                asyncio.create_task(self.pump(upstream_reader, writer)),
            ]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (ConnectionError, OSError):
            pass  # A dropped transport is the experiment, not a relay crash.
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for stream in (writer, upstream):
                if stream is not None:
                    self.connections.discard(stream)
                    stream.close()
                    try:
                        await stream.wait_closed()
                    except (ConnectionError, OSError):
                        pass

    async def watch_gate(self) -> None:
        while True:
            if self.offline_file.exists():
                for writer in tuple(self.connections):
                    writer.close()
            await asyncio.sleep(0.05)

    async def serve(self, listen_port: int) -> None:
        server = await asyncio.start_server(self.accept, "127.0.0.1", listen_port)
        watcher = asyncio.create_task(self.watch_gate())
        try:
            async with server:
                print(f"simlab network relay ready on 127.0.0.1:{listen_port}", flush=True)
                await server.serve_forever()
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            for writer in tuple(self.connections):
                writer.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--upstream-port", type=int, required=True)
    parser.add_argument("--offline-file", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(NetworkRelay(args.upstream_port, args.offline_file).serve(args.listen_port))


if __name__ == "__main__":
    main()
