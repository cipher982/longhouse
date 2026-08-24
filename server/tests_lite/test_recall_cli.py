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

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, _url: str, *, headers: dict, params: dict) -> _Response:
        assert headers == {"X-Agents-Token": "device-token"}
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
        "matches": [
            {
                "session_id": "11111111-1111-4111-8111-111111111111",
                "chunk_index": 0,
                "score": 0.03,
                "evidence": "The IMEI was recorded.",
                "retrieval_lanes": ["lexical", "dense"],
                "lane_ranks": {"lexical": 1, "dense": 3},
                "total_events": 42,
                "context": [
                    {
                        "search_event_id": 9,
                        "role": "assistant",
                        "content_text": "The IMEI was recorded in the device notes.",
                        "tool_name": None,
                    }
                ],
                "match_event_id": 9,
                "evidence_status": "complete",
                "evidence_reason": None,
            }
        ],
        "total": 1,
        "lanes": ["lexical", "dense"],
        "degraded": [],
        "context_byte_budget": 16384,
        "context_bytes_returned": 42,
    }


def test_recall_readable_output_uses_the_current_context_contract(monkeypatch):
    client = _wire(monkeypatch, _payload())

    result = CliRunner().invoke(app, ["recall", "IMEI", "--context-turns", "1"])

    assert result.exit_code == 0, result.output
    assert "The IMEI was recorded in the device notes." in result.output
    assert "Lexical #1 + Semantic #3" in result.output
    assert "3%" not in result.output
    assert client.params == {
        "query": "IMEI",
        "since_days": 14,
        "max_results": 5,
        "context_turns": 1,
    }


def test_recall_json_is_compact_and_defaults_to_anchor_only(monkeypatch):
    client = _wire(monkeypatch, _payload())

    result = CliRunner().invoke(app, ["recall", "IMEI", "--json"])

    assert result.exit_code == 0, result.output
    assert len(result.output.splitlines()) == 1
    assert json.loads(result.output) == _payload()
    assert client.params is not None
    assert client.params["context_turns"] == 0
    assert client.params["max_results"] == 5
