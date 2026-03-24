from __future__ import annotations

from fastapi import Request


def get_request_public_base_url(request: Request) -> str:
    """Return the externally reachable origin for this request.

    Hosted tenants run behind a TLS-terminating proxy, so `request.base_url`
    can report `http://` unless we honor the forwarded headers ourselves.
    """

    forwarded_host = _first_forwarded_value(request.headers.get("x-forwarded-host"))
    forwarded_proto = _first_forwarded_value(request.headers.get("x-forwarded-proto"))
    host = forwarded_host or request.headers.get("host") or request.url.netloc
    scheme = forwarded_proto or request.url.scheme
    if host:
        return f"{scheme}://{host}".rstrip("/")
    return str(request.base_url).rstrip("/")


def _first_forwarded_value(value: str | None) -> str | None:
    if not value:
        return None
    first = value.split(",", 1)[0].strip()
    return first or None
