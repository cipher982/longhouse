"""``longhouse-server sessions events`` pages the way the host does.

Storage-v2 hosts reject a nonzero ``offset`` with 400 and hand back a
``next_cursor`` instead, so a caller that walks a whole session (hatch review
reads the requester's messages this way) must be able to pass that cursor
through, and must not send an offset the host will refuse.
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
    def __init__(self):
        self.params: dict | None = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, url: str, *, headers: dict, params: dict) -> _Response:
        self.params = params
        return _Response({"events": [], "has_more": False, "next_cursor": None})


def _run(monkeypatch, *args: str) -> dict:
    client = _Client()
    monkeypatch.setattr(sessions_cli, "_load_api_credentials", lambda **_: ("https://longhouse.test", "device-token"))
    monkeypatch.setattr(sessions_cli.httpx, "Client", lambda timeout: client)
    result = CliRunner().invoke(app, ["sessions", "events", "session-1", "--json", *args])
    assert result.exit_code == 0, result.output
    assert client.params is not None
    return client.params


def test_cursor_is_passed_through_and_offset_is_not_sent(monkeypatch):
    params = _run(monkeypatch, "--cursor", "TEhDMgEB")

    assert params["cursor"] == "TEhDMgEB"
    assert "offset" not in params


def test_first_page_sends_neither_cursor_nor_offset(monkeypatch):
    params = _run(monkeypatch, "--limit", "100")

    assert params["limit"] == 100
    assert "cursor" not in params
    assert "offset" not in params


def test_explicit_offset_still_reaches_a_legacy_host(monkeypatch):
    params = _run(monkeypatch, "--offset", "9")

    assert params["offset"] == 9
