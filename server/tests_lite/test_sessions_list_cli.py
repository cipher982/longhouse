"""``longhouse-server sessions list`` is the entry point when no id is known.

The command exists so that "what ran lately on this machine" is answerable
without already holding a session id or a text query. What it must get right is
the filter mapping (a renamed parameter silently returns unfiltered sessions
instead of failing) and printing ids that a following command can accept.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from zerg.cli import sessions as sessions_cli
from zerg.cli.main import app


class _Response:
    status_code = 200

    def __init__(self, payload: dict):
        self.payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self.payload


class _Client:
    def __init__(self, response: _Response):
        self.response = response
        self.url: str | None = None
        self.params: dict | None = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, url: str, *, headers: dict, params: dict) -> _Response:
        assert headers == {"X-Agents-Token": "device-token"}
        self.url = url
        self.params = params
        return self.response


def _wire(monkeypatch, payload: dict) -> _Client:
    client = _Client(_Response(payload))
    monkeypatch.setattr(
        sessions_cli,
        "_load_api_credentials",
        lambda **_: ("https://longhouse.test", "device-token"),
    )
    monkeypatch.setattr(sessions_cli.httpx, "Client", lambda timeout: client)
    return client


def _payload() -> dict:
    return {
        "total": 2,
        "sessions": [
            {
                "id": "11111111-1111-4111-8111-111111111111",
                "provider": "omp",
                "project": "zerg",
                "device_id": "cinder",
                "last_activity_at": "2026-09-20T03:47:33.991000Z",
                "title": "Investigate and Fix Session Spam",
            },
            {
                "id": "22222222-2222-4222-8222-222222222222",
                "provider": "omp",
                "project": "zeta",
                "device_id": "cinder",
                "last_activity_at": "2026-09-20T03:47:38.348000Z",
                "title": None,
            },
        ],
    }


def test_list_maps_filters_to_the_api_parameters(monkeypatch):
    client = _wire(monkeypatch, _payload())

    result = CliRunner().invoke(app, ["sessions", "list", "--device", "cinder", "--days-back", "2"])

    assert result.exit_code == 0
    assert client.url == "https://longhouse.test/api/agents/sessions"
    assert client.params["device_id"] == "cinder"
    assert client.params["days_back"] == 2


def test_list_prints_copyable_ids_and_hides_automation_by_default(monkeypatch):
    client = _wire(monkeypatch, _payload())

    result = CliRunner().invoke(app, ["sessions", "list"])

    assert result.exit_code == 0
    assert client.params["include_automation"] is False
    assert client.params["include_test"] is False
    assert "11111111-1111-4111-8111-111111111111" in result.stdout
    assert "22222222-2222-4222-8222-222222222222" in result.stdout
    assert "Investigate and Fix Session Spam" in result.stdout
