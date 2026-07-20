"""Thin HTTP client for Longhouse REST API.

All MCP tools delegate to this client rather than accessing the database
directly, keeping the MCP server a pure API consumer.
"""

from __future__ import annotations

import httpx


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

    async def get(
        self,
        path: str,
        params: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Send a GET request to the Longhouse API.

        Args:
            path: API path (e.g., ``/api/agents/sessions``).
            params: Optional query parameters.

        Returns:
            The httpx Response object.
        """
        request_headers = dict(self._headers)
        if headers:
            request_headers.update(headers)
        return await self._client.get(
            f"{self.base_url}{path}",
            headers=request_headers,
            params=params,
        )

    async def post(
        self,
        path: str,
        json: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Send a POST request to the Longhouse API.

        Args:
            path: API path.
            json: Optional JSON body.

        Returns:
            The httpx Response object.
        """
        request_headers = dict(self._headers)
        if headers:
            request_headers.update(headers)
        return await self._client.post(
            f"{self.base_url}{path}",
            headers=request_headers,
            json=json,
        )
