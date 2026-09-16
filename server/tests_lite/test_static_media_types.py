"""Landing assets must carry real media types (nosniff is on for every response)."""

import mimetypes

from zerg.utils.media_types import register_missing_media_types


def test_landing_asset_media_types():
    register_missing_media_types()
    assert mimetypes.guess_type("timeline-preview.webp")[0] == "image/webp"
    assert mimetypes.guess_type("jetbrains-mono-regular.woff2")[0] == "font/woff2"
