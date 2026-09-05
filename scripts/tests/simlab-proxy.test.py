#!/usr/bin/env python3
"""Real-socket regression coverage for the simulator's outage gate."""

import asyncio
import importlib.util
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("simlab_proxy", Path(__file__).resolve().parents[1] / "qa/simlab_proxy.py")
assert SPEC and SPEC.loader
proxy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proxy)


class NetworkRelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_outage_drops_existing_and_new_connections_then_recovers(self):
        async def echo(reader, writer):
            try:
                while data := await reader.read(1024):
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        with tempfile.TemporaryDirectory() as scratch:
            gate = Path(scratch) / "offline"
            upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
            relay = proxy.NetworkRelay(upstream.sockets[0].getsockname()[1], gate)
            server = await asyncio.start_server(relay.accept, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            watcher = asyncio.create_task(relay.watch_gate())
            writers = []
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writers.append(writer)
                writer.write(b"before outage")
                await writer.drain()
                self.assertEqual(await asyncio.wait_for(reader.readexactly(13), 2), b"before outage")

                gate.touch()
                self.assertEqual(await asyncio.wait_for(reader.read(1), 2), b"")
                blocked, blocked_writer = await asyncio.open_connection("127.0.0.1", port)
                writers.append(blocked_writer)
                self.assertEqual(await asyncio.wait_for(blocked.read(1), 2), b"")

                gate.unlink()
                recovered, recovered_writer = await asyncio.open_connection("127.0.0.1", port)
                writers.append(recovered_writer)
                recovered_writer.write(b"after outage")
                await recovered_writer.drain()
                self.assertEqual(await asyncio.wait_for(recovered.readexactly(12), 2), b"after outage")
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
                for writer in writers:
                    writer.close()
                    await writer.wait_closed()
                server.close()
                upstream.close()
                await server.wait_closed()
                await upstream.wait_closed()


if __name__ == "__main__":
    unittest.main()
