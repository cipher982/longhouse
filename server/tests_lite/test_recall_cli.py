from __future__ import annotations

import json

from typer.testing import CliRunner

from zerg.cli import connect
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
        self.params: dict | None = None
        self.url: str | None = None

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
    monkeypatch.setattr(connect, "get_zerg_url", lambda _config_dir: "https://longhouse.test")
    monkeypatch.setattr(connect, "load_token", lambda _config_dir: "device-token")
    monkeypatch.setattr(connect.httpx, "Client", lambda timeout: client)
    return client


def _payload() -> dict:
    return {
        "results": [
            {
                "ref": "rr1_" + "A" * 55,
                "session_id": "11111111-1111-4111-8111-111111111111",
                "project": "g55",
                "provider": "codex",
                "started_at": "2026-08-20T12:00:00Z",
                "snippet": "The IMEI was recorded in the device notes.",
                "snippet_unavailable_reason": None,
                "total_events": 42,
                "matched_role": "assistant",
                "matched_tool_name": None,
                "matched_by": ["lexical", "dense"],
            }
        ],
        "total": 1,
        "lanes": ["lexical", "dense"],
        "degraded": [],
    }


def test_recall_readable_output_is_a_compact_browseable_index(monkeypatch):
    client = _wire(monkeypatch, _payload())

    result = CliRunner().invoke(app, ["recall", "IMEI"])

    assert result.exit_code == 0, result.output
    assert "The IMEI was recorded in the device notes." in result.output
    assert "g55 · codex" in result.output
    assert "assistant · 42 events" in result.output
    assert "https://longhouse.test/timeline/11111111-1111-4111-8111-111111111111" in result.output
    assert "longhouse-server recall-context rr1_" in result.output
    assert client.params == {
        "query": "IMEI",
        "since_days": 14,
        "max_results": 5,
    }


def test_recall_json_is_compact_and_never_requests_bulk_context(monkeypatch):
    client = _wire(monkeypatch, _payload())

    result = CliRunner().invoke(app, ["recall", "IMEI", "--json"])

    assert result.exit_code == 0, result.output
    assert len(result.output.splitlines()) == 1
    assert json.loads(result.output) == _payload()
    assert client.params is not None
    assert "context_turns" not in client.params
    assert client.params["max_results"] == 5


def test_recall_context_opens_exactly_one_ref(monkeypatch):
    result_ref = "rr1_" + "A" * 55
    client = _wire(
        monkeypatch,
        {
            "ref": result_ref,
            "session_id": "11111111-1111-4111-8111-111111111111",
            "turns": [{"role": "assistant", "content_text": "The IMEI was recorded.", "is_match": True}],
            "total_events": 42,
            "content_byte_budget": 6000,
            "content_bytes_returned": 22,
            "max_content_bytes_applied": 1200,
            "evidence_status": "complete",
            "evidence_reason": None,
        },
    )

    result = CliRunner().invoke(app, ["recall-context", result_ref])

    assert result.exit_code == 0, result.output
    assert "* [assistant] The IMEI was recorded." in result.output
    assert client.url == "https://longhouse.test/api/agents/recall/context"
    assert client.params == {"ref": result_ref, "before": 2, "after": 2, "max_content_bytes": 1200}
