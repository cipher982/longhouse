#!/usr/bin/env python3
"""Tiny local ingest echo server for synthetic shipper bench runs."""

from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path


class Handler(BaseHTTPRequestHandler):
    server_version = "LonghouseSyntheticIngest/1.0"

    def do_POST(self) -> None:
        if self.path != "/api/agents/ingest":
            self.send_response(404)
            self.end_headers()
            return

        content_length = int(self.headers.get("Content-Length", "0") or "0")
        _ = self.rfile.read(content_length)
        trace = self.headers.get("X-Longhouse-Ship-Trace", "")
        lane = _lane_from_trace(trace)
        queue_wait_ms = 1.0 if lane == "live" else 2.0
        exec_ms = 2.0 if lane == "live" else 5.0

        time.sleep(exec_ms / 1000)
        self.send_response(204)
        self.send_header("X-Ingest-Lane", lane)
        self.send_header("X-Ingest-Queue-Wait-Ms", f"{queue_wait_ms:.1f}")
        self.send_header("X-Ingest-Exec-Ms", f"{exec_ms:.1f}")
        self.send_header("X-Ingest-Chunk-Size", str(content_length))
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


def _lane_from_trace(raw: str) -> str:
    try:
        payload = json.loads(raw)
    except ValueError:
        return "archive"
    if payload.get("work_context") == "live_transcript":
        return "live"
    return "archive"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port-file", required=True)
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Path(args.port_file).write_text(str(server.server_port), encoding="utf-8")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
