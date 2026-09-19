"""Tests for bounded MCP REST client recovery."""

import httpx
import pytest

from zerg.mcp_server.api_client import LonghouseAPIClient


@pytest.mark.asyncio
async def test_client_retries_429_with_retry_after_and_preserves_request():
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, request=request)
        return httpx.Response(201, json={"accepted": True}, request=request)

    client = LonghouseAPIClient("https://runtime.test", "device-token")
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        response = await client.post(
            "/api/agents/directed-inputs",
            json={"target_session_id": "target", "text": "hello", "client_request_id": "stable"},
        )
    finally:
        await client.aclose()

    assert response.status_code == 201
    assert len(calls) == 2
    assert calls[0].headers["X-Agents-Token"] == "device-token"
    assert calls[0].content == calls[1].content


@pytest.mark.asyncio
async def test_client_does_not_retry_non_idempotent_post():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "0"}, request=request)

    client = LonghouseAPIClient("https://runtime.test", "device-token")
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        response = await client.post("/api/agents/directed-inputs", json={"text": "hello"})
    finally:
        await client.aclose()

    assert response.status_code == 429
    assert calls == 1
