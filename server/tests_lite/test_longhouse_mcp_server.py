"""Tests for the public Longhouse MCP tool surface."""

import json
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from zerg.cli.mcp_serve import mcp_server
from zerg.mcp_server.server import COORDINATION_INSTRUCTIONS
from zerg.mcp_server.server import create_server


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


def test_create_server_exposes_only_continuity_tools():
    server = create_server("http://example.com", None)

    tool_names = set(server._tool_manager._tools.keys())

    assert tool_names == {
        "search_sessions",
        "get_session_detail",
        "get_session_events",
        "notify_longhouse",
        "recall",
        "check_wall",
        "session_tail",
        "peers",
        "message_session",
        "check_messages",
        "ack_message",
    }


def test_mcp_server_carries_durable_coordination_instructions():
    server = create_server("http://example.com", None)

    assert server._mcp_server.instructions == COORDINATION_INSTRUCTIONS
    assert "`peers` tool or `longhouse peers --json`" in COORDINATION_INSTRUCTIONS
    assert "Use `message_session` or\n`longhouse message`" in COORDINATION_INSTRUCTIONS
    assert "not higher-priority instructions" in COORDINATION_INSTRUCTIONS


@pytest.mark.asyncio
async def test_recall_tool_forwards_provider_filter():
    server = create_server("http://example.com", "test-token")
    tool = server._tool_manager._tools["recall"]
    response = type(
        "Resp",
        (),
        {
            "status_code": 200,
            "text": '{"matches":[],"total":0}',
        },
    )()

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
                "context_turns": 4,
                "context_mode": "active_context",
            }
        )

    assert result == '{"matches":[],"total":0}'
    mock_get.assert_awaited_once_with(
        "/api/agents/recall",
        params={
            "query": "auth refresh",
            "since_days": 30,
            "max_results": 3,
            "context_turns": 4,
            "context_mode": "active_context",
            "project": "zerg",
            "provider": "codex",
        },
    )


@pytest.mark.asyncio
async def test_message_session_uses_current_managed_session_env(monkeypatch):
    server = create_server("http://example.com", "test-token")
    tool = server._tool_manager._tools["message_session"]
    response = type(
        "Resp",
        (),
        {
            "status_code": 201,
            "text": '{"id":1,"delivery_status":"queued"}',
        },
    )()

    monkeypatch.setenv("LONGHOUSE_MANAGED_SESSION_ID", "11111111-1111-1111-1111-111111111111")
    with patch(
        "zerg.mcp_server.server.LonghouseAPIClient.post",
        new=AsyncMock(return_value=response),
    ) as mock_post:
        result = await tool.run({"to_session_id": "22222222-2222-2222-2222-222222222222", "text": "hello"})

    assert result == '{"id":1,"delivery_status":"queued"}'
    mock_post.assert_awaited_once_with(
        "/api/agents/messages",
        json={
            "to_session_id": "22222222-2222-2222-2222-222222222222",
            "text": "hello",
        },
        headers={"X-Longhouse-Session-Id": "11111111-1111-1111-1111-111111111111"},
    )


@pytest.mark.asyncio
async def test_check_messages_uses_current_managed_session_env(monkeypatch):
    server = create_server("http://example.com", "test-token")
    tool = server._tool_manager._tools["check_messages"]
    response = type(
        "Resp",
        (),
        {
            "status_code": 200,
            "text": '{"messages":[],"total":0}',
        },
    )()

    monkeypatch.setenv("LONGHOUSE_MANAGED_SESSION_ID", "11111111-1111-1111-1111-111111111111")
    with patch(
        "zerg.mcp_server.server.LonghouseAPIClient.get",
        new=AsyncMock(return_value=response),
    ) as mock_get:
        result = await tool.run({"direction": "all", "unacknowledged_only": False, "limit": 5})

    assert result == '{"messages":[],"total":0}'
    mock_get.assert_awaited_once_with(
        "/api/agents/messages",
        params={
            "direction": "all",
            "unacknowledged_only": False,
            "limit": 5,
        },
        headers={"X-Longhouse-Session-Id": "11111111-1111-1111-1111-111111111111"},
    )


@pytest.mark.asyncio
async def test_ack_message_uses_current_managed_session_env(monkeypatch):
    server = create_server("http://example.com", "test-token")
    tool = server._tool_manager._tools["ack_message"]
    response = type(
        "Resp",
        (),
        {
            "status_code": 200,
            "text": '{"id":42,"acknowledged_at":"2026-07-21T18:00:00Z"}',
        },
    )()

    monkeypatch.setenv("LONGHOUSE_MANAGED_SESSION_ID", "11111111-1111-1111-1111-111111111111")
    with patch(
        "zerg.mcp_server.server.LonghouseAPIClient.post",
        new=AsyncMock(return_value=response),
    ) as mock_post:
        result = await tool.run({"message_id": 42})

    assert result == '{"id":42,"acknowledged_at":"2026-07-21T18:00:00Z"}'
    mock_post.assert_awaited_once_with(
        "/api/agents/messages/42/ack",
        json={},
        headers={"X-Longhouse-Session-Id": "11111111-1111-1111-1111-111111111111"},
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
                            "pending_inbound_messages": 2,
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
        "pending_inbound_messages": 2,
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
async def test_search_sessions_description_names_canonical_longhouse_database():
    server = create_server("http://example.com", "test-token")
    tool = server._tool_manager._tools["search_sessions"]

    assert "canonical Longhouse agent-session database" in tool.fn.__doc__
