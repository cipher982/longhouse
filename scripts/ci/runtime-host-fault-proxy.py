#!/usr/bin/env python3
"""Sit between a managed launcher and a real Runtime Host and break one call.

Every managed-launch test has run against a Runtime Host that only ever
succeeded, so no test could tell "the launcher tolerates a Runtime Host fault"
apart from "the fault never happened". That gap let launch confirmation stay
fatal in all five launchers: one lost HTTP response on a bookkeeping POST meant
Longhouse refused to open the user's agent. This proxy supplies the fault.

Everything is a byte-for-byte TCP relay, including WebSocket upgrades, except
the first request on a connection whose target matches --fault-path. Matching
on the first request is enough because each Rust call that this lane targets
builds its own client and therefore its own connection.

Fault modes:
  status:<code>          Refuse without forwarding. The Runtime Host never sees
                         the call, so durable state is untouched.
  forward-status:<code>  Forward, wait for the Runtime Host to answer, discard
                         its real response and send <code> instead. This is a
                         committed write whose response was lost, which is the
                         shape of the incident this lane exists for.
  forward-reset          Forward, then destroy the connection with RST.
  hang                   Read the request and answer nothing until the client
                         gives up.
"""

import argparse
import socket
import struct
import threading
import time

_REASON = {
    409: "Conflict",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
}


class Budget:
    """How many matching requests may still be faulted. A limit of 0 is unlimited."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._used = 0
        self._lock = threading.Lock()

    def take(self) -> bool:
        with self._lock:
            if self._limit and self._used >= self._limit:
                return False
            self._used += 1
            return True


def endpoint(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    if not host or not port.isdigit():
        raise argparse.ArgumentTypeError(f"expected host:port, got {value!r}")
    return host, int(port)


def read_headers(sock: socket.socket, limit: int = 262144) -> tuple[bytes, bool]:
    buffer = b""
    while b"\r\n\r\n" not in buffer:
        chunk = sock.recv(4096)
        if not chunk:
            return buffer, False
        buffer += chunk
        if len(buffer) > limit:
            return buffer, False
    return buffer, True


def content_length(head: bytes) -> int:
    for line in head.split(b"\r\n")[1:]:
        if line.lower().startswith(b"content-length:"):
            try:
                return int(line.split(b":", 1)[1].strip())
            except ValueError:
                return 0
    return 0


def request_target(head: bytes) -> str:
    parts = head.split(b"\r\n", 1)[0].decode("latin-1", "replace").split(" ")
    return parts[1] if len(parts) > 1 else ""


def pump(source: socket.socket, sink: socket.socket) -> None:
    try:
        while True:
            data = source.recv(65536)
            if not data:
                break
            sink.sendall(data)
    except OSError:
        pass
    finally:
        try:
            sink.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def respond(sock: socket.socket, code: int) -> None:
    body = b'{"detail":"injected Runtime Host fault"}'
    head = (
        f"HTTP/1.1 {code} {_REASON.get(code, 'Error')}\r\n"
        "content-type: application/json\r\n"
        f"content-length: {len(body)}\r\n"
        "connection: close\r\n\r\n"
    ).encode()
    try:
        sock.sendall(head + body)
    except OSError:
        pass


def reset(sock: socket.socket) -> None:
    """Drop the connection with RST so the client sees a transport failure."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    except OSError:
        pass


def relay(client: socket.socket, args: argparse.Namespace, buffered: bytes) -> None:
    upstream = socket.create_connection(args.target)
    try:
        upstream.sendall(buffered)
        reverse = threading.Thread(target=pump, args=(upstream, client), daemon=True)
        reverse.start()
        pump(client, upstream)
        reverse.join(timeout=args.forward_timeout)
    finally:
        upstream.close()


def apply_fault(client: socket.socket, args: argparse.Namespace, head: bytes, body: bytes) -> None:
    if args.fault_mode.startswith("forward"):
        upstream = socket.create_connection(args.target)
        try:
            upstream.settimeout(args.forward_timeout)
            upstream.sendall(head + b"\r\n\r\n" + body)
            # The response is discarded, but reading it is what proves the
            # Runtime Host processed the call before the client is told it
            # failed. Without this the mode would be indistinguishable from a
            # plain refusal.
            seen = b""
            while b"\r\n\r\n" not in seen:
                chunk = upstream.recv(4096)
                if not chunk:
                    break
                seen += chunk
        except OSError:
            pass
        finally:
            upstream.close()

    if args.fault_mode == "forward-reset":
        reset(client)
        return
    if args.fault_mode == "hang":
        time.sleep(args.hang_seconds)
        return
    respond(client, int(args.fault_mode.split(":", 1)[1]))


def serve_client(client: socket.socket, args: argparse.Namespace, budget: Budget) -> None:
    try:
        buffered, complete = read_headers(client)
        if not complete:
            return
        head, _, rest = buffered.partition(b"\r\n\r\n")
        if args.fault_path not in request_target(head) or not budget.take():
            relay(client, args, buffered)
            return
        body = rest
        declared = content_length(head)
        while len(body) < declared:
            chunk = client.recv(65536)
            if not chunk:
                break
            body += chunk
        apply_fault(client, args, head, body)
    except OSError:
        pass
    finally:
        try:
            client.close()
        except OSError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", type=endpoint, required=True)
    parser.add_argument("--target", type=endpoint, required=True)
    parser.add_argument("--fault-path", required=True, help="substring of the request target to break")
    parser.add_argument("--fault-mode", required=True)
    parser.add_argument(
        "--fault-count",
        type=int,
        default=0,
        help="fault at most this many matching requests; 0 means every one",
    )
    parser.add_argument("--hang-seconds", type=float, default=20.0)
    parser.add_argument("--forward-timeout", type=float, default=30.0)
    args = parser.parse_args()

    if not (
        args.fault_mode in {"forward-reset", "hang"}
        or (
            args.fault_mode.split(":", 1)[0] in {"status", "forward-status"}
            and args.fault_mode.split(":", 1)[-1].isdigit()
        )
    ):
        parser.error(f"unrecognized fault mode {args.fault_mode!r}")

    budget = Budget(args.fault_count)
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(args.listen)
    listener.listen(128)
    while True:
        client, _ = listener.accept()
        threading.Thread(target=serve_client, args=(client, args, budget), daemon=True).start()


if __name__ == "__main__":
    main()
