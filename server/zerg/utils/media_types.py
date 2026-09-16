"""Media types the runtime image's OS table is missing.

Slim runtime images ship no /etc/mime.types, so FileResponse falls back to
text/plain for these; with X-Content-Type-Options: nosniff a browser may then
refuse the landing page's images and fonts.
"""

import mimetypes

_MISSING = ((".webp", "image/webp"), (".woff2", "font/woff2"), (".woff", "font/woff"))


def register_missing_media_types() -> None:
    for suffix, media_type in _MISSING:
        mimetypes.add_type(media_type, suffix)
