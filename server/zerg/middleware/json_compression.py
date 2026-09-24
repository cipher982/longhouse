"""Gzip complete JSON API responses for clients that ask for it.

A real account's timeline list is ~500 KB of JSON (each card carries its
session three times); gzip at level 5 makes it ~20 KB in under 2 ms. Only a
finished 200 `application/json` body is compressed. Event streams, media,
ranges, streaming bodies, and anything already encoded pass through byte for
byte, which is why this is not Starlette's GZipMiddleware: that one compresses
every content type but event streams, 206 ranges and images included.
"""

from __future__ import annotations

import gzip

from starlette.datastructures import Headers
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp
from starlette.types import Message
from starlette.types import Receive
from starlette.types import Scope
from starlette.types import Send

MIN_COMPRESS_BYTES = 1024
COMPRESS_LEVEL = 5


def accepts_gzip(scope: Scope) -> bool:
    """True when Accept-Encoding admits gzip (or `*`) with a nonzero q."""

    for key, value in scope.get("headers", []):
        if key != b"accept-encoding":
            continue
        for token in value.decode("latin-1").split(","):
            coding, *params = (part.strip() for part in token.split(";"))
            if coding.lower() not in ("gzip", "*"):
                continue
            quality = 1.0
            for param in params:
                if param.startswith("q="):
                    try:
                        quality = float(param[2:])
                    except ValueError:
                        quality = 0.0
            if quality > 0:
                return True
    return False


class JSONCompressionMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not accepts_gzip(scope):
            await self.app(scope, receive, send)
            return

        start: Message | None = None
        passthrough = False

        async def send_compressed(message: Message) -> None:
            nonlocal start, passthrough
            if message["type"] == "http.response.start":
                headers = Headers(raw=message["headers"])
                eligible = (
                    message["status"] == 200
                    and headers.get("content-type", "").startswith("application/json")
                    and "content-encoding" not in headers
                )
                if eligible:
                    start = message
                else:
                    passthrough = True
                    await send(message)
                return
            if passthrough or start is None or message["type"] != "http.response.body":
                await send(message)
                return
            body = message.get("body", b"")
            if message.get("more_body", False):
                # A streamed JSON body is left as it is rather than buffered.
                passthrough = True
                await send(start)
                await send(message)
                return
            headers = MutableHeaders(raw=start["headers"])
            headers.add_vary_header("Accept-Encoding")
            if len(body) >= MIN_COMPRESS_BYTES:
                body = gzip.compress(body, compresslevel=COMPRESS_LEVEL, mtime=0)
                headers["Content-Encoding"] = "gzip"
                headers["Content-Length"] = str(len(body))
            await send(start)
            await send({"type": "http.response.body", "body": body, "more_body": False})

        await self.app(scope, receive, send_compressed)
