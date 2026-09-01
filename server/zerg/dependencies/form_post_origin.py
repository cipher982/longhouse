"""Cross-origin guard for cookie-authenticated multipart routes."""

from __future__ import annotations

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
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="cross-origin request rejected")


__all__ = ["reject_cross_origin_form_post"]
