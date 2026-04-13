from __future__ import annotations

import os

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FERNET_SECRET", Fernet.generate_key().decode())

from zerg.main import app
from zerg.services import public_downloads
from zerg.services.runtime_artifacts import RELEASE_ASSET_FILENAMES
from zerg.services.runtime_artifacts import RuntimeComponent


class _FakeUpstreamResponse:
    def __init__(self, body: bytes, *, status_code: int = 200, headers: dict[str, str] | None = None):
        self._body = body
        self.status_code = status_code
        self.headers = headers or {}
        self.closed = False

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://example.test/download")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("bad status", request=request, response=response)

    async def aiter_bytes(self):
        yield self._body

    async def aclose(self) -> None:
        self.closed = True


class _FakeAsyncClient:
    def __init__(self, response: _FakeUpstreamResponse):
        self._response = response
        self.closed = False
        self.requests: list[httpx.Request] = []

    def build_request(self, method: str, url: str) -> httpx.Request:
        request = httpx.Request(method, url)
        self.requests.append(request)
        return request

    async def send(self, request: httpx.Request, *, stream: bool = False) -> _FakeUpstreamResponse:
        assert stream is True
        return self._response

    async def aclose(self) -> None:
        self.closed = True


def test_download_macos_route_streams_clean_filename(monkeypatch):
    upstream = _FakeUpstreamResponse(
        b"zip-bytes",
        headers={
            "Content-Length": "9",
            "ETag": '"abc123"',
            "Last-Modified": "Sun, 13 Apr 2026 12:00:00 GMT",
        },
    )
    fake_client = _FakeAsyncClient(upstream)

    monkeypatch.setattr(public_downloads.httpx, "AsyncClient", lambda **kwargs: fake_client)

    with TestClient(app) as client:
        response = client.get("/download/macos")

    assert response.status_code == 200
    assert response.content == b"zip-bytes"
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"] == 'attachment; filename="Longhouse-macos-arm64.zip"'
    assert response.headers["content-length"] == "9"
    assert response.headers["etag"] == '"abc123"'
    assert response.headers["last-modified"] == "Sun, 13 Apr 2026 12:00:00 GMT"
    assert fake_client.requests[0].url == httpx.URL(public_downloads.macos_desktop_download().upstream_url)
    assert upstream.closed is True
    assert fake_client.closed is True


def test_macos_desktop_download_tracks_runtime_artifact_config():
    asset_name = RELEASE_ASSET_FILENAMES[RuntimeComponent.LOCAL_HEALTH_APP]["darwin-arm64"]
    assert public_downloads.macos_desktop_download().upstream_url.endswith(asset_name)


def test_download_macos_route_returns_502_when_upstream_fails(monkeypatch):
    async def _boom(*args, **kwargs):
        raise httpx.ConnectError("boom")

    class _FailingAsyncClient:
        def __init__(self):
            self.closed = False

        def build_request(self, method: str, url: str) -> httpx.Request:
            return httpx.Request(method, url)

        send = _boom

        async def aclose(self) -> None:
            self.closed = True

    fake_client = _FailingAsyncClient()

    monkeypatch.setattr(public_downloads.httpx, "AsyncClient", lambda **kwargs: fake_client)

    with TestClient(app) as client:
        response = client.get("/download/macos")

    assert response.status_code == 502
    assert response.json() == {"detail": "macOS download is temporarily unavailable"}
    assert fake_client.closed is True
