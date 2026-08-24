"""Tests for the public Longhouse MCP tool surface."""

import json
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from zerg.cli.mcp_serve import mcp_server
from zerg.mcp_server.server import COORDINATION_INSTRUCTIONS
from zerg.mcp_server.server import _render_recall_search
from zerg.mcp_server.server import create_server


class _Response:
    def __init__(self, payload: dict, status_code: int = 200):
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return json.loads(self.text)


def test_recall_card_renderer_exposes_small_open_decision_facts():
    rendered = _render_recall_search(
        {
            "results": [
                {
                    "session_id": "11111111-1111-4111-8111-111111111111",
                    "project": "longhouse",
                    "provider": "codex",
                    "snippet": "the answer",
                    "total_events": 42,
                    "matched_role": "assistant",
                    "matched_by": ["dense"],
                    "ref": "rr1_" + "A" * 55,
                }
            ],
            "lanes": ["dense"],
        }
    )

    assert "assistant · 42 events" in rendered


def test_mcp_server_initializes_without_hosted_probe(monkeypatch):
    started = []

    class FakeServer:
        def run(self):
            started.append(True)

    monkeypatch.setattr("zerg.mcp_server.create_server", lambda **kwargs: FakeServer())
    monkeypatch.setattr(
        "httpx.get",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("MCP startup must not use the network")),
    )

    mcp_server(
        url="https://demo.longhouse.test",
        token="test-token",
        transport="stdio",
        port=8001,
    )

    assert started == [True]


def test_create_server_exposes_archive_and_coordination_tools_by_default():
    server = create_server("http://example.com", None)

    tool_names = set(server._tool_manager._tools.keys())

    assert tool_names == {
        "search_sessions",
        "get_session_detail",
        "recall",
        "recall_context",
        "peers",
        "tail",
        "send",
        "inbox",
        "reply",
    }


def test_managed_coordination_server_keeps_history_search(monkeypatch):
    """A coordination launch must not subtract archive discovery.

    The strip this replaces deleted search_sessions from exactly the sessions
    that need it; directed-input authority is enforced per call instead.
    """
    monkeypatch.setenv("LONGHOUSE_COORDINATION_TOKEN", "zst_coordination")

    server = create_server("http://example.com", None)

    tool_names = set(server._tool_manager._tools)
    assert "search_sessions" in tool_names
    assert tool_names == set(create_server("http://example.com", None)._tool_manager._tools)


def test_mcp_server_carries_durable_coordination_instructions():
    server = create_server("http://example.com", None)

    assert server._mcp_server.instructions == COORDINATION_INSTRUCTIONS
    assert "`peers` tool" in COORDINATION_INSTRUCTIONS
    assert "Use `send` for directed input" in COORDINATION_INSTRUCTIONS
    assert "attributed untrusted input" in COORDINATION_INSTRUCTIONS


@pytest.mark.asyncio
async def test_recall_tool_forwards_provider_filter():
    server = create_server("http://example.com", "test-token")
    tool = server._tool_manager._tools["recall"]
    response = _Response({"results": [], "total": 0, "lanes": ["lexical"], "degraded": []})

    with patch(
        "zerg.mcp_server.server.LonghouseAPIClient.get",
        new=AsyncMock(return_value=response),
    ) as mock_get:
        result = await tool.run(
            {
                "query": "auth refresh",
                "project": "zerg",
                "provider": "codex",
                "since_days": 30,
                "max_results": 3,
            }
        )

    assert result == "0 recall results · lanes: lexical"
    mock_get.assert_awaited_once_with(
        "/api/agents/recall",
        params={
            "query": "auth refresh",
            "since_days": 30,
            "max_results": 3,
            "mode": "auto",
            "project": "zerg",
            "provider": "codex",
        },
    )


@pytest.mark.asyncio
async def test_recall_tool_caps_card_count_and_never_requests_bulk_context():
    server = create_server("http://example.com", "test-token")
    tool = server._tool_manager._tools["recall"]
    response = _Response({"results": [], "total": 0, "lanes": ["lexical"], "degraded": []})

    with patch(
        "zerg.mcp_server.server.LonghouseAPIClient.get",
        new=AsyncMock(return_value=response),
    ) as mock_get:
        await tool.run({"query": "auth refresh", "max_results": 999})

    assert mock_get.await_args.kwargs["params"]["max_results"] == 10
    assert "context_turns" not in mock_get.await_args.kwargs["params"]


@pytest.mark.asyncio
async def test_recall_context_opens_one_ref_and_renders_turns():
    server = create_server("http://example.com", "test-token")
    tool = server._tool_manager._tools["recall_context"]
    result_ref = "rr1_" + "A" * 55
    response = _Response(
        {
            "ref": result_ref,
            "session_id": "11111111-1111-4111-8111-111111111111",
            "turns": [{"role": "assistant", "content_text": "the answer", "is_match": True}],
            "content_byte_budget": 6000,
            "content_bytes_returned": 10,
            "evidence_status": "complete",
        }
    )
    with patch(
        "zerg.mcp_server.server.LonghouseAPIClient.get",
        new=AsyncMock(return_value=response),
    ) as mock_get:
        result = await tool.run({"ref": result_ref, "before": 99, "after": -1})

    assert "* [assistant] the answer" in result
    mock_get.assert_awaited_once_with(
        "/api/agents/recall/context",
        params={"ref": result_ref, "before": 5, "after": 0, "max_content_bytes": 1200},
    )


@pytest.mark.asyncio
async def test_send_uses_session_scoped_authority(monkeypatch):
    server = create_server("http://example.com", "test-token")
    tool = server._tool_manager._tools["send"]
    response = type(
        "Resp",
        (),
        {
            "status_code": 201,
            "text": '{"id":1,"input_receipt":{"status":"queued"}}',
        },
    )()

    monkeypatch.setenv("LONGHOUSE_MANAGED_SESSION_ID", "11111111-1111-1111-1111-111111111111")
    monkeypatch.setenv("LONGHOUSE_COORDINATION_TOKEN", "zst_coordination")
    with patch(
        "zerg.mcp_server.server.LonghouseAPIClient.post",
        new=AsyncMock(return_value=response),
    ) as mock_post:
        result = await tool.run({"session_id": "22222222-2222-2222-2222-222222222222", "text": "hello"})

    assert result == '{"id":1,"input_receipt":{"status":"queued"}}'
    mock_post.assert_awaited_once_with(
        "/api/agents/directed-inputs",
        json={
            "target_session_id": "22222222-2222-2222-2222-222222222222",
            "text": "hello",
        },
        headers={
            "X-Longhouse-Session-Id": "11111111-1111-1111-1111-111111111111",
            "X-Agents-Token": "zst_coordination",
        },
    )


@pytest.mark.asyncio
async def test_inbox_uses_session_scoped_authority(monkeypatch):
    server = create_server("http://example.com", "test-token")
    tool = server._tool_manager._tools["inbox"]
    response = type(
        "Resp",
        (),
        {
            "status_code": 200,
            "text": '{"directed_inputs":[],"next_cursor":0}',
        },
    )()

    monkeypatch.setenv("LONGHOUSE_MANAGED_SESSION_ID", "11111111-1111-1111-1111-111111111111")
    monkeypatch.setenv("LONGHOUSE_COORDINATION_TOKEN", "zst_coordination")
    with patch(
        "zerg.mcp_server.server.LonghouseAPIClient.get",
        new=AsyncMock(return_value=response),
    ) as mock_get:
        result = await tool.run({"direction": "all", "after_cursor": 3, "limit": 5})

    assert result == '{"directed_inputs":[],"next_cursor":0}'
    mock_get.assert_awaited_once_with(
        "/api/agents/directed-inputs",
        params={
            "direction": "all",
            "after_id": 3,
            "limit": 5,
        },
        headers={
            "X-Longhouse-Session-Id": "11111111-1111-1111-1111-111111111111",
            "X-Agents-Token": "zst_coordination",
        },
    )


@pytest.mark.asyncio
async def test_reply_uses_session_scoped_authority(monkeypatch):
    server = create_server("http://example.com", "test-token")
    tool = server._tool_manager._tools["reply"]
    response = type(
        "Resp",
        (),
        {
            "status_code": 200,
            "text": '{"id":43,"reply_to_id":42}',
        },
    )()

    monkeypatch.setenv("LONGHOUSE_MANAGED_SESSION_ID", "11111111-1111-1111-1111-111111111111")
    monkeypatch.setenv("LONGHOUSE_COORDINATION_TOKEN", "zst_coordination")
    with patch(
        "zerg.mcp_server.server.LonghouseAPIClient.post",
        new=AsyncMock(return_value=response),
    ) as mock_post:
        result = await tool.run({"input_id": 42, "text": "done"})

    assert result == '{"id":43,"reply_to_id":42}'
    mock_post.assert_awaited_once_with(
        "/api/agents/directed-inputs/42/reply",
        json={"text": "done"},
        headers={
            "X-Longhouse-Session-Id": "11111111-1111-1111-1111-111111111111",
            "X-Agents-Token": "zst_coordination",
        },
    )


@pytest.mark.asyncio
async def test_peers_infers_repo_from_current_session(monkeypatch):
    server = create_server("http://example.com", "test-token")
    tool = server._tool_manager._tools["peers"]
    current_resp = type(
        "Resp",
        (),
        {
            "status_code": 200,
            "text": '{"id":"11111111-1111-1111-1111-111111111111","git_repo":"git@github.com:cipher982/longhouse.git"}',
        },
    )()
    wall_resp = type(
        "Resp",
        (),
        {
            "status_code": 200,
            "text": json.dumps(
                {
                    "sessions": [
                        {
                            "session_id": "11111111-1111-1111-1111-111111111111",
                            "has_live_presence": True,
                            "device_name": "laptop",
                            "provider": "claude",
                            "presence_state": "idle",
                            "summary_title": "Current",
                            "git_branch": "main",
                        },
                        {
                            "session_id": "22222222-2222-2222-2222-222222222222",
                            "has_live_presence": True,
                            "device_name": "demo-machine",
                            "provider": "codex",
                            "cwd": "/Users/dev/git/longhouse",
                            "git_repo": "git@github.com:cipher982/longhouse.git",
                            "presence_state": "thinking",
                            "summary_title": "Peer",
                            "git_branch": "main",
                        },
                    ],
                    "total": 2,
                }
            ),
        },
    )()

    monkeypatch.setenv("LONGHOUSE_MANAGED_SESSION_ID", "11111111-1111-1111-1111-111111111111")
    with patch(
        "zerg.mcp_server.server.LonghouseAPIClient.get",
        new=AsyncMock(side_effect=[current_resp, wall_resp]),
    ) as mock_get:
        result = await tool.run({})

    payload = json.loads(result)
    assert payload["total"] == 1
    assert payload["repo"] == "git@github.com:cipher982/longhouse.git"
    assert payload["active_only"] is True
    assert payload["peers"][0] == {
        "session_id": "22222222-2222-2222-2222-222222222222",
        "device_name": "demo-machine",
        "provider": "codex",
        "cwd": "/Users/dev/git/longhouse",
        "git_repo": "git@github.com:cipher982/longhouse.git",
        "git_branch": "main",
        "summary_title": "Peer",
        "presence_state": "thinking",
        "kernel_control_label": None,
        "kernel_live_control_available": None,
        "kernel_host_reattach_available": None,
        "kernel_observe_only": None,
        "kernel_search_only": None,
        "kernel_staleness_reason": None,
    }
    assert mock_get.await_args_list[0].args == ("/api/agents/sessions/11111111-1111-1111-1111-111111111111",)
    assert mock_get.await_args_list[1].args == ("/api/agents/sessions/wall",)
    assert mock_get.await_args_list[1].kwargs["params"] == {
        "repo": "git@github.com:cipher982/longhouse.git",
        "days": 7,
    }


@pytest.mark.asyncio
async def test_peers_falls_back_to_cwd_when_no_git_repo(monkeypatch):
    server = create_server("http://example.com", "test-token")
    tool = server._tool_manager._tools["peers"]
    current_resp = type(
        "Resp",
        (),
        {
            "status_code": 200,
            "text": '{"id":"11111111-1111-1111-1111-111111111111","git_repo":null,"cwd":"/Users/dev/git/acme/project"}',
        },
    )()
    wall_resp = type(
        "Resp",
        (),
        {
            "status_code": 200,
            "text": json.dumps({"sessions": [], "total": 0}),
        },
    )()

    monkeypatch.setenv("LONGHOUSE_MANAGED_SESSION_ID", "11111111-1111-1111-1111-111111111111")
    with patch(
        "zerg.mcp_server.server.LonghouseAPIClient.get",
        new=AsyncMock(side_effect=[current_resp, wall_resp]),
    ) as mock_get:
        result = await tool.run({})

    payload = json.loads(result)
    assert "error" not in payload
    assert mock_get.await_args_list[1].kwargs["params"] == {
        "repo": "/Users/dev/git/acme/project",
        "days": 7,
    }


@pytest.mark.asyncio
async def test_semantic_search_sessions_fails_loud_instead_of_falling_back():
    server = create_server("http://example.com", "test-token")
    tool = server._tool_manager._tools["search_sessions"]
    semantic_resp = type(
        "Resp",
        (),
        {
            "status_code": 503,
            "text": "semantic index unavailable",
        },
    )()

    with patch(
        "zerg.mcp_server.server.LonghouseAPIClient.get",
        new=AsyncMock(return_value=semantic_resp),
    ) as mock_get:
        result = await tool.run({"query": "bedrock", "semantic": True})

    payload = json.loads(result)
    assert payload["error"] == "Semantic search unavailable: API returned 503"
    assert payload["retry"] == "Call search_sessions with semantic=false for lexical search."
    assert mock_get.await_count == 1
    assert mock_get.await_args.args == ("/api/agents/sessions/semantic",)


@pytest.mark.asyncio
async def test_search_sessions_without_query_lists_recent_sessions():
    """Omitting the query is a listing call, not an error.

    The tool forwards to /api/agents/sessions without a query param, which
    returns recent sessions ordered by last activity.
    """
    server = create_server("http://example.com", "test-token")
    tool = server._tool_manager._tools["search_sessions"]
    response = type(
        "Resp",
        (),
        {
            "status_code": 200,
            "text": '{"sessions":[],"total":0,"has_real_sessions":false}',
        },
    )()

    with patch(
        "zerg.mcp_server.server.LonghouseAPIClient.get",
        new=AsyncMock(return_value=response),
    ) as mock_get:
        result = await tool.run({"project": "zerg", "limit": 5})

    assert result == '{"sessions":[],"total":0,"has_real_sessions":false}'
    mock_get.assert_awaited_once_with(
        "/api/agents/sessions",
        params={
            "days_back": 14,
            "limit": 5,
            "context_mode": "forensic",
            "project": "zerg",
        },
    )


@pytest.mark.asyncio
async def test_search_sessions_blank_query_is_treated_as_absent():
    server = create_server("http://example.com", "test-token")
    tool = server._tool_manager._tools["search_sessions"]
    response = type(
        "Resp",
        (),
        {
            "status_code": 200,
            "text": '{"sessions":[],"total":0,"has_real_sessions":false}',
        },
    )()

    with patch(
        "zerg.mcp_server.server.LonghouseAPIClient.get",
        new=AsyncMock(return_value=response),
    ) as mock_get:
        await tool.run({"query": "   "})

    assert "query" not in mock_get.await_args.kwargs["params"]


@pytest.mark.asyncio
async def test_search_sessions_semantic_without_query_errors_without_api_call():
    server = create_server("http://example.com", "test-token")
    tool = server._tool_manager._tools["search_sessions"]

    with patch(
        "zerg.mcp_server.server.LonghouseAPIClient.get",
        new=AsyncMock(),
    ) as mock_get:
        result = await tool.run({"semantic": True})

    payload = json.loads(result)
    assert "requires a query" in payload["error"]
    assert mock_get.await_count == 0


@pytest.mark.asyncio
async def test_search_sessions_preserves_structured_owner_scope_error():
    server = create_server("http://example.com", "test-token")
    tool = server._tool_manager._tools["search_sessions"]
    response = type(
        "Resp",
        (),
        {
            "status_code": 503,
            "text": json.dumps(
                {
                    "detail": {
                        "code": "canonical_owner_required",
                        "message": "Canonical owner scope is unavailable.",
                    }
                }
            ),
        },
    )()

    with patch(
        "zerg.mcp_server.server.LonghouseAPIClient.get",
        new=AsyncMock(return_value=response),
    ):
        result = await tool.run({"query": "coordination"})

    payload = json.loads(result)
    assert payload == {
        "error": "API returned 503",
        "status_code": 503,
        "detail": {
            "code": "canonical_owner_required",
            "message": "Canonical owner scope is unavailable.",
        },
        "code": "canonical_owner_required",
        "message": "Canonical owner scope is unavailable.",
    }


@pytest.mark.asyncio
async def test_search_sessions_marks_search_unavailable_as_not_absence():
    server = create_server("http://example.com", "test-token")
    tool = server._tool_manager._tools["search_sessions"]
    response = type(
        "Resp",
        (),
        {
            "status_code": 503,
            "text": json.dumps(
                {
                    "detail": {
                        "code": "search_unavailable",
                        "message": "The derived search index is unavailable.",
                    }
                }
            ),
        },
    )()

    with patch(
        "zerg.mcp_server.server.LonghouseAPIClient.get",
        new=AsyncMock(return_value=response),
    ):
        result = await tool.run({"query": "coordination"})

    payload = json.loads(result)
    assert payload["code"] == "search_unavailable"
    assert payload["outcome"] == "unavailable"
    assert payload["not_found"] is False
    assert "not evidence that no sessions exist" in payload["retry"]


@pytest.mark.asyncio
async def test_search_sessions_description_names_canonical_longhouse_database():
    server = create_server("http://example.com", "test-token")
    tool = server._tool_manager._tools["search_sessions"]

    assert "canonical Longhouse agent-session database" in tool.fn.__doc__
