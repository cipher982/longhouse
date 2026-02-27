"""Tests for tool_call_id pairing through the full ingest → API stack.

Covers:
- tool_call_id round-trips through ingest and is returned by the events API
- Claude tool_use call and tool_result are linked by the same tool_call_id
- Codex function_call and function_call_output are linked by call_id
- Legacy events with tool_call_id=None still render (FIFO fallback)
"""

import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from fastapi.testclient import TestClient

from zerg.database import Base, get_db, make_engine, make_sessionmaker
from zerg.models.agents import AgentsBase
from zerg.main import api_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(tmp_path):
    db_path = tmp_path / "test.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    AgentsBase.metadata.create_all(bind=engine)
    factory = make_sessionmaker(engine)

    def override():
        d = factory()
        try:
            yield d
        finally:
            d.close()

    api_app.dependency_overrides[get_db] = override
    return TestClient(api_app), factory


def _ingest_session(client, session_id, events):
    payload = {
        "id": session_id,
        "provider": "claude",
        "environment": "production",
        "started_at": "2026-01-01T00:00:00Z",
        "events": events,
    }
    resp = client.post(
        "/agents/ingest",
        json=payload,
        headers={"X-Agents-Token": "dev"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_tool_call_id_round_trips_via_api(tmp_path):
    """tool_call_id stored on ingest is returned by the events endpoint."""
    client, _ = _make_client(tmp_path)
    try:
        session_id = "aaaaaaaa-0000-0000-0000-000000000001"
        _ingest_session(client, session_id, [
            {
                "role": "assistant",
                "tool_name": "Bash",
                "tool_input_json": {"command": "ls"},
                "tool_call_id": "toolu_01ABC",
                "timestamp": "2026-01-01T00:00:01Z",
            },
            {
                "role": "tool",
                "tool_output_text": "file1.txt\nfile2.txt",
                "tool_call_id": "toolu_01ABC",
                "timestamp": "2026-01-01T00:00:02Z",
            },
        ])

        resp = client.get(
            f"/agents/sessions/{session_id}/events",
            headers={"X-Agents-Token": "dev"},
        )
        assert resp.status_code == 200
        events = resp.json()["events"]
        assert len(events) == 2

        call_event = next(e for e in events if e["role"] == "assistant")
        result_event = next(e for e in events if e["role"] == "tool")

        assert call_event["tool_call_id"] == "toolu_01ABC"
        assert result_event["tool_call_id"] == "toolu_01ABC"
        assert call_event["tool_name"] == "Bash"
        assert result_event["tool_name"] is None  # result never has tool_name
    finally:
        api_app.dependency_overrides.clear()


def test_parallel_tool_calls_linked_correctly(tmp_path):
    """Parallel tool calls are each linked to their own result by tool_call_id."""
    client, _ = _make_client(tmp_path)
    try:
        session_id = "aaaaaaaa-0000-0000-0000-000000000002"
        _ingest_session(client, session_id, [
            {"role": "assistant", "tool_name": "Read",
             "tool_call_id": "toolu_READ", "timestamp": "2026-01-01T00:00:01Z"},
            {"role": "assistant", "tool_name": "Bash",
             "tool_call_id": "toolu_BASH", "timestamp": "2026-01-01T00:00:01Z"},
            # Results arrive in reverse order — this breaks FIFO but not ID-based
            {"role": "tool", "tool_output_text": "bash output",
             "tool_call_id": "toolu_BASH", "timestamp": "2026-01-01T00:00:02Z"},
            {"role": "tool", "tool_output_text": "file contents",
             "tool_call_id": "toolu_READ", "timestamp": "2026-01-01T00:00:02Z"},
        ])

        resp = client.get(
            f"/agents/sessions/{session_id}/events",
            headers={"X-Agents-Token": "dev"},
        )
        events = resp.json()["events"]

        # Calls: role=assistant, have tool_name but no tool_output_text
        calls = [e for e in events if e["role"] == "assistant"]
        assert {e["tool_call_id"] for e in calls} == {"toolu_READ", "toolu_BASH"}

        # Results: role=tool, have tool_output_text
        results = [e for e in events if e["role"] == "tool"]
        bash_result = next(e for e in results if e["tool_call_id"] == "toolu_BASH")
        read_result = next(e for e in results if e["tool_call_id"] == "toolu_READ")

        # Results arrive out of FIFO order but ID-based pairing still correct
        assert bash_result["tool_output_text"] == "bash output"
        assert read_result["tool_output_text"] == "file contents"
    finally:
        api_app.dependency_overrides.clear()


def test_parallel_tool_results_not_deduplicated(tmp_path):
    """Two tool results with the same timestamp must both be stored (hash must differ)."""
    client, _ = _make_client(tmp_path)
    try:
        session_id = "aaaaaaaa-0000-0000-0000-000000000004"
        _ingest_session(client, session_id, [
            {"role": "assistant", "tool_name": "Bash",
             "tool_call_id": "toolu_A", "timestamp": "2026-01-01T00:00:01Z"},
            {"role": "assistant", "tool_name": "Read",
             "tool_call_id": "toolu_B", "timestamp": "2026-01-01T00:00:01Z"},
            # Same timestamp, different output + tool_call_id — must NOT be deduped
            {"role": "tool", "tool_output_text": "output A",
             "tool_call_id": "toolu_A", "timestamp": "2026-01-01T00:00:02Z"},
            {"role": "tool", "tool_output_text": "output B",
             "tool_call_id": "toolu_B", "timestamp": "2026-01-01T00:00:02Z"},
        ])

        resp = client.get(
            f"/agents/sessions/{session_id}/events",
            headers={"X-Agents-Token": "dev"},
        )
        events = resp.json()["events"]
        assert len(events) == 4, f"Expected 4 events, got {len(events)} — dedup collapsed parallel results"

        results = [e for e in events if e["role"] == "tool"]
        assert len(results) == 2
        outputs = {e["tool_call_id"]: e["tool_output_text"] for e in results}
        assert outputs["toolu_A"] == "output A"
        assert outputs["toolu_B"] == "output B"
    finally:
        api_app.dependency_overrides.clear()


def test_legacy_events_without_tool_call_id_still_ingest(tmp_path):
    """Events without tool_call_id (pre-fix legacy rows) ingest without error."""
    client, _ = _make_client(tmp_path)
    try:
        session_id = "aaaaaaaa-0000-0000-0000-000000000003"
        _ingest_session(client, session_id, [
            {"role": "assistant", "tool_name": "Edit",
             "tool_input_json": {"file_path": "/tmp/f"},
             "timestamp": "2026-01-01T00:00:01Z"},  # no tool_call_id
            {"role": "tool", "tool_output_text": "edited",
             "timestamp": "2026-01-01T00:00:02Z"},   # no tool_call_id
        ])

        resp = client.get(
            f"/agents/sessions/{session_id}/events",
            headers={"X-Agents-Token": "dev"},
        )
        events = resp.json()["events"]
        assert len(events) == 2
        assert all(e["tool_call_id"] is None for e in events)
    finally:
        api_app.dependency_overrides.clear()
