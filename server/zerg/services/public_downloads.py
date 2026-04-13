from __future__ import annotations

from dataclasses import dataclass

import httpx
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from zerg.services.runtime_artifacts import LEGACY_RELEASE_ASSET_FILENAMES
from zerg.services.runtime_artifacts import RELEASE_ASSET_FILENAMES
from zerg.services.runtime_artifacts import RELEASE_REPO
from zerg.services.runtime_artifacts import RuntimeComponent

PUBLIC_DOWNLOAD_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True)
class PublicDownloadCandidate:
    asset_name: str
    filename: str
    media_type: str


@dataclass(frozen=True)
class PublicDownload:
    slug: str
    candidates: tuple[PublicDownloadCandidate, ...]


class PublicDownloadUnavailable(RuntimeError):
    """Raised when an upstream public download cannot be fetched."""


def _latest_release_asset_url(asset_name: str) -> str:
    return f"https://github.com/{RELEASE_REPO}/releases/latest/download/{asset_name}"


def macos_desktop_download() -> PublicDownload:
    desktop_archive_asset = RELEASE_ASSET_FILENAMES[RuntimeComponent.DESKTOP_APP]["darwin-arm64"]
    legacy_archive_asset = LEGACY_RELEASE_ASSET_FILENAMES[RuntimeComponent.DESKTOP_APP]["darwin-arm64"]
    return PublicDownload(
        slug="macOS",
        candidates=(
            PublicDownloadCandidate(
                asset_name="Longhouse-macos-arm64.dmg",
                filename="Longhouse-macos-arm64.dmg",
                media_type="application/x-apple-diskimage",
            ),
            PublicDownloadCandidate(
                asset_name=desktop_archive_asset,
                filename="Longhouse-macos-arm64.zip",
                media_type="application/zip",
            ),
            PublicDownloadCandidate(
                asset_name=legacy_archive_asset,
                filename="Longhouse-macos-arm64.zip",
                media_type="application/zip",
            ),
        ),
    )


async def _close_stream(response: httpx.Response, client: httpx.AsyncClient) -> None:
    await response.aclose()
    await client.aclose()


async def _open_candidate_stream(
    client: httpx.AsyncClient,
    candidate: PublicDownloadCandidate,
) -> httpx.Response:
    request = client.build_request("GET", _latest_release_asset_url(candidate.asset_name))
    response = await client.send(request, stream=True)
    try:
        response.raise_for_status()
    except httpx.HTTPError:
        await response.aclose()
        raise
    return response


async def _resolve_download_candidate(
    client: httpx.AsyncClient,
    download: PublicDownload,
) -> tuple[PublicDownloadCandidate, httpx.Response]:
    last_error: httpx.HTTPError | None = None
    for candidate in download.candidates:
        try:
            response = await _open_candidate_stream(client, candidate)
            return candidate, response
        except httpx.HTTPError as exc:
            last_error = exc
            continue

    raise PublicDownloadUnavailable(f"{download.slug} download is temporarily unavailable") from last_error


async def download_response(download: PublicDownload) -> StreamingResponse:
    client = httpx.AsyncClient(follow_redirects=True, timeout=PUBLIC_DOWNLOAD_TIMEOUT_SECONDS)
    try:
        selected_candidate, upstream = await _resolve_download_candidate(client, download)
    except PublicDownloadUnavailable:
        await client.aclose()
        raise

    headers = {
        "Cache-Control": "no-store",
        "Content-Disposition": f'attachment; filename="{selected_candidate.filename}"',
    }
    for header_name in ("Content-Length", "ETag", "Last-Modified"):
        header_value = upstream.headers.get(header_name)
        if header_value:
            headers[header_name] = header_value

    return StreamingResponse(
        upstream.aiter_bytes(),
        media_type=selected_candidate.media_type,
        headers=headers,
        background=BackgroundTask(_close_stream, upstream, client),
    )


async def download_macos_desktop_app_response() -> StreamingResponse:
    return await download_response(macos_desktop_download())
