"""Owner-scoped URL credentials for served media.

A transcript renders an image with ``<img src=...>``. That request cannot carry
a header, and a hosted native client (the iOS app) authenticates every API call
with a bearer runtime token and holds no browser session cookie -- so the media
URL itself has to carry the authority to read one blob.

What it carries is a signature over the owner, the blob, and an expiry, not the
client's own bearer token: the URL then grants read of exactly one object, to
one owner, for a bounded time, and never the account behind it. The route still
enforces the owner's view of the session that references the blob.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from zerg.config import get_settings

# Long enough that a transcript document, its cached payload, and a tapped
# image link all survive; short enough to bound a leaked URL. The window also
# fixes which URL a reader gets, so repeated reads of one blob are cache hits
# rather than new URLs.
MEDIA_URL_TOKEN_TTL = timedelta(hours=24)
MEDIA_URL_TOKEN_VERSION = "1"
MEDIA_URL_TOKEN_PARAMETER = "mt"


class MediaUrlTokenError(ValueError):
    """Raised when a media URL token cannot be minted."""


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _signing_secret() -> bytes:
    secret = str(getattr(get_settings(), "jwt_secret", "") or "").strip()
    if not secret:
        raise MediaUrlTokenError("media URL tokens require a configured JWT secret")
    return secret.encode("utf-8")


def _signature(payload: str) -> str:
    digest = hmac.new(_signing_secret(), payload.encode("utf-8"), hashlib.sha256).digest()
    return _b64url_encode(digest)


def _expiry(now: datetime | None, ttl: timedelta) -> int:
    """Expiry bucket-aligned to the TTL window.

    Every read of the same session mints the same URL inside a window, so a
    re-rendered transcript reuses one URL -- and the client's cached bytes --
    instead of refetching each image under a fresh query string. A token
    minted anywhere in a window stays valid for one to two windows.
    """

    window = max(int(ttl.total_seconds()), 1)
    bucket = int((now or datetime.now(timezone.utc)).timestamp()) // window
    return (bucket + 2) * window


def media_url_token(
    *,
    owner_id: int,
    sha256: str,
    now: datetime | None = None,
    ttl: timedelta = MEDIA_URL_TOKEN_TTL,
) -> str:
    """Mint the query credential for one owner's one media object."""

    if owner_id <= 0:
        raise MediaUrlTokenError("media URL tokens require a positive owner id")
    digest = str(sha256 or "").strip().lower()
    if not digest:
        raise MediaUrlTokenError("media URL tokens require a media hash")
    payload = f"{MEDIA_URL_TOKEN_VERSION}.{owner_id}.{_expiry(now, ttl)}.{digest}"
    encoded = _b64url_encode(payload.encode("utf-8"))
    return f"{encoded}.{_signature(payload)}"


def parse_media_url_token(
    token: str,
    *,
    sha256: str,
    now: datetime | None = None,
) -> int | None:
    """Return the owner a token authorizes for this exact blob, or None."""

    if not token:
        return None
    payload, _, signature = token.partition(".")
    if not payload or not signature:
        return None
    try:
        expected = _signature(_b64url_decode(payload).decode("utf-8"))
    except (MediaUrlTokenError, UnicodeDecodeError, ValueError):
        return None
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        version, raw_owner_id, raw_expires, bound_hash = _b64url_decode(payload).decode("utf-8").split(".", 3)
        owner_id = int(raw_owner_id)
        expires_at = int(raw_expires)
    except (UnicodeDecodeError, ValueError):
        return None
    if version != MEDIA_URL_TOKEN_VERSION or owner_id <= 0:
        return None
    if bound_hash != str(sha256 or "").strip().lower():
        return None
    moment = now or datetime.now(timezone.utc)
    if moment.timestamp() >= expires_at:
        return None
    return owner_id


__all__ = [
    "MEDIA_URL_TOKEN_PARAMETER",
    "MEDIA_URL_TOKEN_TTL",
    "MediaUrlTokenError",
    "media_url_token",
    "parse_media_url_token",
]
