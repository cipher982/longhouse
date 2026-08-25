"""Response security headers.

The runtime serves the SPA and the API from one origin, and that origin holds
the ``longhouse_session`` cookie. Nothing else in the stack sets these — the
only CSP in the repo belongs to a standalone nginx image the hosted runtime
does not use.

The policy is deliberately narrow rather than exhaustive: ``frame-ancestors``
is what stops a permission-gate approval being clickjacked, and ``script-src``
is what stops an injected tag from reaching an attacker's host. The SPA build
inlines its own styles, so ``style-src`` keeps ``unsafe-inline``.
"""

from starlette.types import ASGIApp
from starlette.types import Message
from starlette.types import Receive
from starlette.types import Scope
from starlette.types import Send

_CSP = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self' data:",
        # ws:/wss: covers the timeline stream on both self-host and hosted.
        "connect-src 'self' ws: wss:",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)


class SecurityHeadersMiddleware:
    """Attach security headers to every response.

    Pure ASGI so it can sit outside the routing tree and still cover static
    files, the SPA catch-all, and error responses raised before routing.
    """

    def __init__(self, app: ASGIApp, *, hsts: bool = True) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                present = {name.lower() for name, _ in headers}

                def add(name: bytes, value: bytes) -> None:
                    if name not in present:
                        headers.append((name, value))

                add(b"content-security-policy", _CSP.encode("latin-1"))
                add(b"x-content-type-options", b"nosniff")
                add(b"x-frame-options", b"DENY")
                add(b"referrer-policy", b"strict-origin-when-cross-origin")
                if self.hsts and scope.get("scheme") == "https":
                    add(b"strict-transport-security", b"max-age=31536000; includeSubDomains")
            await send(message)

        await self.app(scope, receive, send_with_headers)
