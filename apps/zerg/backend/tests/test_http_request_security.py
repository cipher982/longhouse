"""Security tests for http_request tool."""

from __future__ import annotations

from typing import Any

from zerg.tools.builtin.http_tools import http_request


class DummyResponse:
    def __init__(self, status_code: int = 200, url: str = "https://example.com", headers: dict | None = None, body: Any = None):
        self.status_code = status_code
        self._url = url
        self.headers = headers or {"content-type": "application/json"}
        self._body = body if body is not None else {"ok": True}

    @property
    def url(self) -> str:  # pragma: no cover - trivial
        return self._url

    @property
    def text(self) -> str:  # pragma: no cover - trivial
        return str(self._body)

    def json(self) -> Any:  # pragma: no cover - trivial
        return self._body


class DummyClient:
    def __init__(self, tracker: dict):
        self._tracker = tracker

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def request(self, *args, **kwargs):
        self._tracker["called"] = self._tracker.get("called", 0) + 1
        return DummyResponse(url=str(kwargs.get("url", "https://example.com")))


def test_http_request_blocks_private_ip(monkeypatch):
    tracker: dict[str, int] = {"called": 0}

    def fake_client(*args, **kwargs):
        return DummyClient(tracker)

    monkeypatch.setattr("zerg.tools.builtin.http_tools.httpx.Client", fake_client)

    result = http_request("http://127.0.0.1/private")

    assert result["status_code"] == 0
    assert "blocked" in result.get("error", "").lower()
    assert tracker["called"] == 0


def test_http_request_blocks_disallowed_scheme(monkeypatch):
    tracker: dict[str, int] = {"called": 0}

    def fake_client(*args, **kwargs):
        return DummyClient(tracker)

    monkeypatch.setattr("zerg.tools.builtin.http_tools.httpx.Client", fake_client)

    result = http_request("file:///etc/passwd")

    assert result["status_code"] == 0
    assert "scheme" in result.get("error", "").lower()
    assert tracker["called"] == 0


def test_http_request_allows_public_ip(monkeypatch):
    tracker: dict[str, int] = {"called": 0}

    def fake_client(*args, **kwargs):
        return DummyClient(tracker)

    monkeypatch.setattr("zerg.tools.builtin.http_tools.httpx.Client", fake_client)

    result = http_request("http://8.8.8.8/health")

    assert result["status_code"] == 200
    assert result.get("body") == {"ok": True}
    assert tracker["called"] == 1
