"""An in-process S3 endpoint for the CI receipt tests: PUT, GET and ListObjectsV2 with a delimiter.

It checks only that a request is SigV4-signed for access key `ak`; the signature itself is verified
against a real MinIO by hand, not here.
"""

from __future__ import annotations

import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from xml.sax.saxutils import escape

BUCKET = "artifacts"
PREFIX = "longhouse-ci/receipts"


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.signed_requests = 0
        self.reads: list[str] = []
        self.refuse: list[int] = []  # statuses the next requests are answered with, before any is served
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                pass

            def _key(self) -> tuple[str, dict[str, str]]:
                parts = urllib.parse.urlsplit(self.path)
                path = urllib.parse.unquote(parts.path)
                assert path.startswith(f"/{BUCKET}"), path
                if self.headers.get("Authorization", "").startswith("AWS4-HMAC-SHA256 Credential=ak/"):
                    outer.signed_requests += 1
                return path[len(BUCKET) + 2 :], dict(urllib.parse.parse_qsl(parts.query))

            def _refused(self) -> bool:
                if not outer.refuse:
                    return False
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(outer.refuse.pop(0))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return True

            def do_PUT(self) -> None:  # noqa: N802
                if self._refused():
                    return
                key, _ = self._key()
                outer.objects[key] = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                if self._refused():
                    return
                key, query = self._key()
                if key == "" and query.get("list-type") == "2":
                    body = outer._list(query.get("prefix", ""), query.get("delimiter", ""))
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                outer.reads.append(key)
                data = outer.objects.get(key)
                self.send_response(200 if data is not None else 404)
                self.send_header("Content-Length", str(len(data) if data is not None else 0))
                self.end_headers()
                if data is not None:
                    self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True)

    def _list(self, prefix: str, delimiter: str) -> bytes:
        contents: list[str] = []
        prefixes: list[str] = []
        for key in sorted(k for k in self.objects if k.startswith(prefix)):
            rest = key[len(prefix) :]
            if delimiter and delimiter in rest:
                common = prefix + rest.split(delimiter)[0] + delimiter
                if common not in prefixes:
                    prefixes.append(common)
            else:
                contents.append(key)
        xml = '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><IsTruncated>false</IsTruncated>'
        xml += "".join(f"<Contents><Key>{escape(k)}</Key></Contents>" for k in contents)
        xml += "".join(f"<CommonPrefixes><Prefix>{escape(p)}</Prefix></CommonPrefixes>" for p in prefixes)
        return (xml + "</ListBucketResult>").encode()

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def env(self) -> dict[str, str]:
        return {
            "CI_RECEIPTS_S3_ENDPOINT": self.endpoint,
            "CI_RECEIPTS_S3_BUCKET": BUCKET,
            "CI_RECEIPTS_S3_PREFIX": PREFIX,
            "CI_RECEIPTS_S3_ACCESS_KEY_ID": "ak",
            "CI_RECEIPTS_S3_SECRET_ACCESS_KEY": "sk",
        }

    def __enter__(self) -> "FakeS3":
        self.thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
