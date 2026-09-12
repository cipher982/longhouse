"""Cross-origin guard for cookie-authenticated multipart routes."""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import HTTPException
from fastapi import Request
from fastapi import status

from zerg.config import get_settings
from zerg.config import resolve_cors_origins


def reject_cross_origin_form_post(request: Request) -> None:
    """Reject a cross-origin browser submit of a multipart route.

    ``multipart/form-data`` is a CORS-simple content type, so a form on another
    origin can POST here with the session cookie attached and CORS never gets to
    preflight it. Cookies are ``SameSite=Lax``, but tenants are subdomains of one
    site, so SameSite cannot separate them either. Non-browser clients (iOS, CLI)
    send neither header and are unaffected.
    """
    fetch_site = request.headers.get("Sec-Fetch-Site")
    if fetch_site is not None and fetch_site not in ("same-origin", "none"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="cross-origin request rejected")
    origin = request.headers.get("Origin")
    if origin is None:
        return
    allowed = {f"{request.url.scheme}://{request.url.netloc}"}
    allowed.update(o for o in resolve_cors_origins(get_settings()) if o != "*")
    if origin not in allowed:
        # TLS termination can leave the ASGI scope on http while the browser
        # correctly reports the public https origin. Same-origin fetch metadata
        # still proves this is the browser's own origin; compare the host as
        # the proxy-independent fallback.
        same_host_proxy_origin = fetch_site == "same-origin" and urlparse(origin).netloc.lower() == request.url.netloc.lower()
        if not same_host_proxy_origin:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="cross-origin request rejected")


def require_browser_auth_header(request: Request) -> None:
    """Require a preflighted marker for browser cookie-auth mutations.

    Native clients do not send browser fetch metadata or an Origin header and
    remain compatible. A browser fetch must send this non-simple header, so a
    sibling tenant cannot trigger refresh or logout with an HTML form.
    """
    reject_cross_origin_form_post(request)
    browser_request = any(request.headers.get(name) for name in ("Origin", "Sec-Fetch-Site", "Sec-Fetch-Mode", "Sec-Fetch-Dest"))
    if browser_request and request.headers.get("X-Longhouse-Auth") != "1":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="browser auth marker required",
        )


__all__ = ["reject_cross_origin_form_post", "require_browser_auth_header"]
