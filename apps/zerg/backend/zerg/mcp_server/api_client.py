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

    async def get(
        self,
        path: str,
        params: dict | None = None,
    ) -> httpx.Response:
        """Send a GET request to the Longhouse API.

        Args:
            path: API path (e.g., ``/api/agents/sessions``).
            params: Optional query parameters.

        Returns:
            The httpx Response object.
        """
        async with httpx.AsyncClient(timeout=15) as client:
            return await client.get(
                f"{self.base_url}{path}",
                headers=self._headers,
                params=params,
            )

    async def post(
        self,
        path: str,
        json: dict | None = None,
    ) -> httpx.Response:
        """Send a POST request to the Longhouse API.

        Args:
            path: API path.
            json: Optional JSON body.

        Returns:
            The httpx Response object.
        """
        async with httpx.AsyncClient(timeout=15) as client:
            return await client.post(
                f"{self.base_url}{path}",
                headers=self._headers,
                json=json,
            )
