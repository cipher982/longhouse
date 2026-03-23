from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

from zerg.cli import onboard


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _Client:
    def __init__(self, responses: list[_Response], calls: list[dict]) -> None:
        self._responses = responses
        self.calls = calls

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def post(self, url: str, json: dict, headers: dict | None = None) -> _Response:
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self._responses.pop(0)


def test_emit_test_event_retries_with_fresh_device_token(monkeypatch):
    calls: list[dict] = []
    saved_tokens: list[str] = []
    saved_urls: list[str] = []
    client = _Client([_Response(401), _Response(201)], calls)

    monkeypatch.setattr(onboard.httpx, "Client", lambda timeout=10: client)
    monkeypatch.setattr(onboard, "load_token", lambda: "stale-token")
    monkeypatch.setattr(onboard, "save_token", lambda token: saved_tokens.append(token))
    monkeypatch.setattr(onboard, "save_zerg_url", lambda url: saved_urls.append(url))
    monkeypatch.setattr(onboard.socket, "gethostname", lambda: "test-box")

    def _fake_auto_create_token(api_url: str, device_name: str) -> str:
        assert api_url == "http://127.0.0.1:8080"
        assert device_name == "onboard-test-box"
        return "fresh-token"

    monkeypatch.setattr("zerg.cli.connect._auto_create_token", _fake_auto_create_token)

    assert onboard._emit_test_event("http://127.0.0.1:8080") is True
    assert [call["headers"] for call in calls] == [
        {"X-Agents-Token": "stale-token"},
        {"X-Agents-Token": "fresh-token"},
    ]
    UUID(calls[0]["json"]["id"])
    assert calls[0]["json"]["events"] == [
        {
            "role": "user",
            "content_text": "Welcome to Longhouse! This is a test event from onboarding.",
            "timestamp": calls[0]["json"]["events"][0]["timestamp"],
            "source_path": "onboard://verification",
            "source_offset": 0,
        }
    ]
    assert saved_tokens == ["fresh-token"]
    assert saved_urls == ["http://127.0.0.1:8080"]


def test_emit_test_event_returns_false_when_token_cannot_be_created(monkeypatch):
    calls: list[dict] = []
    client = _Client([_Response(401)], calls)

    monkeypatch.setattr(onboard.httpx, "Client", lambda timeout=10: client)
    monkeypatch.setattr(onboard, "load_token", lambda: None)
    monkeypatch.setattr(onboard.socket, "gethostname", lambda: "test-box")
    monkeypatch.setattr("zerg.cli.connect._auto_create_token", lambda api_url, device_name: None)

    assert onboard._emit_test_event("http://127.0.0.1:8080") is False
    assert calls == []
