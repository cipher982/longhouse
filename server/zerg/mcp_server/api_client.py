"""Thin HTTP client for Longhouse REST API.

All MCP tools delegate to this client rather than accessing the database
directly, keeping the MCP server a pure API consumer.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from datetime import timezone
from email.utils import parsedate_to_datetime

import httpx

_MAX_429_RETRIES = 3
_RETRY_BUDGET_SECONDS = 15.0


def _retry_after_seconds(response: httpx.Response) -> float:
    raw = response.headers.get("Retry-After", "").strip()
    if not raw:
        return 1.0
    try:
        return max(0.0, min(float(raw), _RETRY_BUDGET_SECONDS))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(raw)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, min((retry_at - datetime.now(timezone.utc)).total_seconds(), _RETRY_BUDGET_SECONDS))
    except (TypeError, ValueError, OverflowError):
        return 1.0


class LonghouseAPIClient:
    """Async HTTP client for the Longhouse REST API.

    Args:
        base_url: Longhouse API URL (e.g., ``http://localhost:8080``).
        token: Device token for ``X-Agents-Token`` header. Optional.
    """

    def __init__(self, base_url: str, token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._headers: dict[str, str] = {}
        if token:
            self._headers["X-Agents-Token"] = token
        self._client = httpx.AsyncClient(
            # The API's recall deadline is five seconds. Leave transport headroom so
            # a completed legal response does not surface as an MCP ReadTimeout.
            timeout=httpx.Timeout(10.0),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4, keepalive_expiry=30.0),
        )

    async def aclose(self) -> None:
        """Close the shared connection pool when the MCP server stops."""

        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        request_headers = dict(self._headers)
        if headers:
            request_headers.update(headers)
        request_id = json.get("client_request_id") if json else None
        retry_safe = method in {"GET", "HEAD", "OPTIONS"} or (isinstance(request_id, str) and bool(request_id.strip()))
        deadline = time.monotonic() + _RETRY_BUDGET_SECONDS
        for attempt in range(_MAX_429_RETRIES + 1):
            response = await self._client.request(
                method,
                f"{self.base_url}{path}",
                headers=request_headers,
                params=params,
                json=json,
            )
            if response.status_code != 429 or not retry_safe or attempt == _MAX_429_RETRIES:
                return response
            delay = _retry_after_seconds(response)
            if time.monotonic() + delay > deadline:
                return response
            await asyncio.sleep(delay)
        raise RuntimeError("unreachable retry loop")

    async def get(
        self,
        path: str,
        params: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Send a GET request to the Longhouse API with bounded 429 recovery."""
        return await self._request("GET", path, params=params, headers=headers)

    async def post(
        self,
        path: str,
        json: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Send a POST request to the Longhouse API with bounded 429 recovery."""
        return await self._request("POST", path, json=json, headers=headers)
