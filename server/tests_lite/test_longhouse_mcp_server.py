"""Tests for the public Longhouse MCP tool surface."""

import json
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from zerg.mcp_server.server import create_server


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
    }


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
    assert payload["peers"][0]["session_id"] == "22222222-2222-2222-2222-222222222222"
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
