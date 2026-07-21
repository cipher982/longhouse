#!/usr/bin/env python3
"""Tiny local storage-v2 echo server for synthetic shipper bench runs."""

from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path


class Handler(BaseHTTPRequestHandler):
    server_version = "LonghouseSyntheticStorageV2/1.0"

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] != "/api/agents/storage/v2/capabilities":
            self.send_response(404)
            self.end_headers()
            return

        machine_id = self.headers.get("X-Longhouse-Machine-Id") or "synthetic-machine"
        body = json.dumps(
            {
                "protocol_version": 2,
                "cutover": True,
                "tenant_id": "synthetic-tenant",
                "machine_id": machine_id,
                "ingest_path": "/api/agents/storage/v2/envelopes",
                "max_wire_body_bytes": 12 * 1024 * 1024,
                "max_raw_record_bytes": 4 * 1024 * 1024,
                "max_records": 10_000,
                "media_claim_path": "/api/agents/storage/v2/media/claims",
                "media_upload_path_template": "/api/agents/storage/v2/media/{sha256}",
                "max_media_bytes": 32 * 1024 * 1024,
                "max_media_claims": 512,
                "range_kinds": ["byte_offset", "record_ordinal"],
                "lanes": ["live", "repair"],
                "lane_header": "X-Longhouse-Storage-Lane",
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path != "/api/agents/storage/v2/envelopes":
            self.send_response(404)
            self.end_headers()
            return

        content_length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(content_length)
        lane = (self.headers.get("X-Longhouse-Storage-Lane") or "repair").strip().lower()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self.send_response(400)
            self.end_headers()
            return

        envelope_id = str(payload.get("expected_envelope_id") or "")
        if not envelope_id:
            self.send_response(422)
            self.end_headers()
            return

        exec_ms = 2.0 if lane == "live" else 5.0
        time.sleep(exec_ms / 1000)
        body = json.dumps(
            {
                "v": 2,
                "envelope_id": envelope_id,
                "object_hash": "b" * 64,
                "commit_seq": "1",
                "raw_state": "durable",
                "render_state": "ready",
                "media_state": "complete",
                "missing_media_hashes": [],
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


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
