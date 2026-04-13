from __future__ import annotations

from dataclasses import dataclass

import httpx
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from zerg.services.runtime_artifacts import RELEASE_ASSET_FILENAMES
from zerg.services.runtime_artifacts import RELEASE_REPO
from zerg.services.runtime_artifacts import RuntimeComponent

PUBLIC_DOWNLOAD_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True)
class PublicDownload:
    slug: str
    upstream_url: str
    filename: str
    media_type: str


class PublicDownloadUnavailable(RuntimeError):
    """Raised when an upstream public download cannot be fetched."""


def macos_desktop_download() -> PublicDownload:
    upstream_asset = RELEASE_ASSET_FILENAMES[RuntimeComponent.LOCAL_HEALTH_APP]["darwin-arm64"]
    return PublicDownload(
        slug="macOS",
        upstream_url=f"https://github.com/{RELEASE_REPO}/releases/latest/download/{upstream_asset}",
        filename="Longhouse-macos-arm64.zip",
        media_type="application/zip",
    )


async def _close_stream(response: httpx.Response, client: httpx.AsyncClient) -> None:
    await response.aclose()
    await client.aclose()


async def download_response(download: PublicDownload) -> StreamingResponse:
    client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=PUBLIC_DOWNLOAD_TIMEOUT_SECONDS,
    )
    upstream: httpx.Response | None = None
    try:
        request = client.build_request("GET", download.upstream_url)
        upstream = await client.send(request, stream=True)
        upstream.raise_for_status()
    except httpx.HTTPError as exc:
        if upstream is not None:
            await upstream.aclose()
        await client.aclose()
        raise PublicDownloadUnavailable(f"{download.slug} download is temporarily unavailable") from exc

    headers = {
        "Cache-Control": "no-store",
        "Content-Disposition": f'attachment; filename="{download.filename}"',
    }
    for header_name in ("Content-Length", "ETag", "Last-Modified"):
        header_value = upstream.headers.get(header_name)
        if header_value:
            headers[header_name] = header_value

    return StreamingResponse(
        upstream.aiter_bytes(),
        media_type=download.media_type,
        headers=headers,
        background=BackgroundTask(_close_stream, upstream, client),
    )


async def download_macos_desktop_app_response() -> StreamingResponse:
    return await download_response(macos_desktop_download())
