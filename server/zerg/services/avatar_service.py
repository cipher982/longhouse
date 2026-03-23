"""Avatar upload / processing helper.

This module centralises everything related to validating, resizing and storing
user avatar images so that the *routers* layer stays clean and focused on HTTP
concerns.

The only public call-site is `store_avatar_for_user`, used by
``/users/me/avatar``.
"""

from __future__ import annotations

# Standard library
import io
import uuid
from pathlib import Path

# typing order per Ruff
from typing import Any
from typing import Final
from typing import Optional

# Third-party
from fastapi import UploadFile
from fastapi import status
from fastapi.exceptions import HTTPException

# Constants ------------------------------------------------------------------

MAX_RAW_BYTES: Final[int] = 2 * 1024 * 1024  # 2 MiB raw upload limit
ALLOWED_MIME: Final[set[str]] = {"image/png", "image/jpeg", "image/webp"}

# Directory where avatars are persisted.  Computed relative to repo root so
# the function also works in tests where CWD can vary.
AVATARS_DIR = Path(__file__).resolve().parent.parent.parent / "static" / "avatars"
AVATARS_DIR.mkdir(parents=True, exist_ok=True)


# Helper functions -----------------------------------------------------------


# NOTE: runtime optional dependency – we do not import Pillow at module level.


def _ensure_pillow() -> Optional[Any]:
    """Return PIL module if available; ``None`` otherwise."""

    try:
        from PIL import Image  # type: ignore  # noqa: WPS433 – runtime import

        return Image
    except ModuleNotFoundError:  # pragma: no cover – optional dependency
        return None


def _process_image(raw: bytes, mime: str) -> tuple[bytes, str]:
    """Resize and convert the raw bytes if Pillow is installed.

    Returns a tuple of ``(processed_bytes, file_extension)`` where
    *file_extension* **does not** include the leading dot.
    """

    pillow = _ensure_pillow()
    if pillow is None:
        # Pillow not available – just persist the bytes as-is.  Derive file
        # extension from MIME type so browsers render correctly.
        return raw, {
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/webp": "webp",
        }.get(mime, "img")

    # With Pillow we downscale to fit within 256×256, strip metadata and
    # always encode to WebP because it's smaller and universally supported by
    # modern browsers.

    from PIL import Image  # type: ignore  # noqa: WPS433 – runtime import

    try:
        img = Image.open(io.BytesIO(raw))
    except Exception as exc:  # pragma: no cover – invalid image data
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid image file: {exc}",
        ) from exc

    # Ensure RGB(A)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")

    # In-place resize (thumbnail keeps aspect ✓)
    img.thumbnail((256, 256))

    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=85)
    return buf.getvalue(), "webp"


# Public API -----------------------------------------------------------------


def store_avatar_for_user(upload: UploadFile) -> str:
    """Validate *upload*, process it, store on disk and return public URL.

    The URL is relative ("/static/avatars/…") so the router can store that
    verbatim in the database.
    """

    # 1) Basic guards ---------------------------------------------------------
    if upload.content_type not in ALLOWED_MIME:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported image type")

    raw = upload.file.read()
    if len(raw) > MAX_RAW_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="File too large")

    # 2) Image processing -----------------------------------------------------
    processed_bytes, ext = _process_image(raw, upload.content_type)

    # 3) Persist to ./static/avatars -----------------------------------------
    unique_name = f"{uuid.uuid4().hex}.{ext}"
    dest_path: Path = AVATARS_DIR / unique_name
    try:
        dest_path.write_bytes(processed_bytes)
    except Exception as exc:  # pragma: no cover
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to store image: {exc}",
        ) from exc

    return f"/static/avatars/{unique_name}"
