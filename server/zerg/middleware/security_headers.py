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

from urllib.parse import urlsplit

from starlette.types import ASGIApp
from starlette.types import Message
from starlette.types import Receive
from starlette.types import Scope
from starlette.types import Send

# Third-party origins the SPA really loads. Each one is here because the page
# visibly breaks without it (verified 2026-09-16 on longhouse.ai, where this
# list had been missing since the policy landed):
#   - web fonts: web/index.html links Google Fonts and Fontshare stylesheets
#   - the landing live demo sandbox: web/src/components/landing/demo/liveDemoConfig.ts
_FONT_STYLE_ORIGINS = ("https://fonts.googleapis.com", "https://api.fontshare.com")
_FONT_FILE_ORIGINS = ("https://fonts.gstatic.com", "https://cdn.fontshare.com")
_LIVE_DEMO_ORIGIN = "https://freetype-phase1.drose-agents.workers.dev"


def _origin(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def build_csp(*, analytics_script_src: str | None = None) -> str:
    """The Content-Security-Policy for every response.

    The analytics origin comes from the configured Umami script (script tag
    plus its beacon), so a deployment without analytics allows no extra host.
    """
    analytics = _origin(analytics_script_src)
    analytics_hosts = (analytics,) if analytics else ()
    return "; ".join(
        (
            "default-src 'self'",
            " ".join(("script-src 'self' https://accounts.google.com/gsi/client", *analytics_hosts)),
            " ".join(("style-src 'self' 'unsafe-inline'", *_FONT_STYLE_ORIGINS)),
            "img-src 'self' data: blob:",
            " ".join(("font-src 'self' data:", *_FONT_FILE_ORIGINS)),
            # WebSocket streams and Google Identity Services use explicit
            # provider origins; the page still cannot submit forms cross-origin.
            " ".join(
                (
                    "connect-src 'self' https://accounts.google.com/gsi/ ws: wss:",
                    _LIVE_DEMO_ORIGIN,
                    *analytics_hosts,
                )
            ),
            "frame-src https://accounts.google.com/gsi/",
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

    def __init__(self, app: ASGIApp, *, hsts: bool = True, analytics_script_src: str | None = None) -> None:
        self.app = app
        self.hsts = hsts
        self.csp = build_csp(analytics_script_src=analytics_script_src).encode("latin-1")

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

                add(b"content-security-policy", self.csp)
                add(b"x-content-type-options", b"nosniff")
                add(b"x-frame-options", b"DENY")
                add(b"referrer-policy", b"strict-origin-when-cross-origin")
                if self.hsts and scope.get("scheme") == "https":
                    add(b"strict-transport-security", b"max-age=31536000; includeSubDomains")
            await send(message)

        await self.app(scope, receive, send_with_headers)
