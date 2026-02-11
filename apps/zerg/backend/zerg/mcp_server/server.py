"""Longhouse MCP server — exposes session search, memory, and notifications.

Uses the ``mcp`` SDK's ``FastMCP`` decorator pattern to register tools.
All tools return JSON strings.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from zerg.mcp_server.api_client import LonghouseAPIClient

# UUID v4 pattern for input validation
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

logger = logging.getLogger(__name__)

# Local file-based memory store (pragmatic shortcut; upgrade to API later)
_MEMORY_PATH = Path.home() / ".claude" / "longhouse-memory.json"


def _load_memory() -> dict[str, str]:
    """Load the local memory KV store from disk."""
    if not _MEMORY_PATH.exists():
        return {}
    try:
        text = _MEMORY_PATH.read_text()
        if not text.strip():
            return {}
        return json.loads(text)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read memory file %s: %s", _MEMORY_PATH, exc)
        return {}


def _save_memory(data: dict[str, str]) -> None:
    """Write the local memory KV store to disk atomically.

    Uses write-to-temp + rename so a crash mid-write doesn't corrupt the file.
    """
    _MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=_MEMORY_PATH.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, _MEMORY_PATH)
    except BaseException:
        # Clean up temp file on any failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def create_server(api_url: str, api_token: str | None = None) -> FastMCP:
    """Create and return a configured Longhouse MCP server.

    Args:
        api_url: Longhouse REST API URL.
        api_token: Device token for API authentication.

    Returns:
        A ``FastMCP`` server instance with all tools registered.
    """
    server = FastMCP("longhouse")
    client = LonghouseAPIClient(api_url, api_token)

    # ------------------------------------------------------------------
    # Tool: search_sessions
    # ------------------------------------------------------------------
    @server.tool()
    async def search_sessions(
        query: str,
        project: str | None = None,
        provider: str | None = None,
        days_back: int = 14,
        limit: int = 10,
    ) -> str:
        """Search past agent sessions by content.

        Queries the Longhouse API for sessions matching a text search.
        Returns session metadata including dates, providers, projects,
        message counts, and matching snippets.

        Args:
            query: Text to search for in session content.
            project: Filter by project name (optional).
            provider: Filter by provider, e.g. claude, codex, gemini (optional).
            days_back: Number of days to look back (default 14).
            limit: Maximum results to return (default 10).
        """
        params: dict = {
            "query": query,
            "days_back": days_back,
            "limit": limit,
        }
        if project:
            params["project"] = project
        if provider:
            params["provider"] = provider

        try:
            resp = await client.get("/api/agents/sessions", params=params)
            if resp.status_code != 200:
                return json.dumps({"error": f"API returned {resp.status_code}", "detail": resp.text[:500]})
            return resp.text
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    # ------------------------------------------------------------------
    # Tool: get_session_detail
    # ------------------------------------------------------------------
    @server.tool()
    async def get_session_detail(
        session_id: str,
        max_events: int = 50,
    ) -> str:
        """Get detailed events from a specific session.

        Retrieves the event log (user messages, assistant responses,
        tool calls) for a single session.

        Args:
            session_id: UUID of the session to retrieve.
            max_events: Maximum number of events to return (default 50).
        """
        # Validate session_id to prevent path injection
        if not _UUID_RE.match(session_id):
            return json.dumps({"error": "Invalid session_id format. Expected a UUID."})

        try:
            # Fetch session metadata
            meta_resp = await client.get(f"/api/agents/sessions/{session_id}")
            if meta_resp.status_code != 200:
                return json.dumps({"error": f"Session not found: {meta_resp.status_code}"})

            # Fetch session events
            events_resp = await client.get(
                f"/api/agents/sessions/{session_id}/events",
                params={"limit": max_events},
            )
            if events_resp.status_code != 200:
                return json.dumps({"error": f"Events fetch failed: {events_resp.status_code}"})

            session = meta_resp.json()
            events_data = events_resp.json()

            return json.dumps(
                {
                    "session": session,
                    "events": events_data.get("events", []),
                    "total_events": events_data.get("total", 0),
                }
            )
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    # ------------------------------------------------------------------
    # Tool: memory_read
    # ------------------------------------------------------------------
    @server.tool()
    async def memory_read(key: str) -> str:
        """Read a persistent memory value.

        Reads from a local JSON key-value store at
        ``~/.claude/longhouse-memory.json``. Returns the value if found,
        or an error message if the key does not exist.

        Args:
            key: The key to look up.
        """
        store = _load_memory()
        if key in store:
            return json.dumps({"key": key, "value": store[key]})
        return json.dumps({"key": key, "error": "Key not found"})

    # ------------------------------------------------------------------
    # Tool: memory_write
    # ------------------------------------------------------------------
    @server.tool()
    async def memory_write(key: str, value: str) -> str:
        """Write a persistent memory value.

        Stores a key-value pair in a local JSON file at
        ``~/.claude/longhouse-memory.json``. Overwrites any existing
        value for the same key.

        Args:
            key: The key to store.
            value: The value to associate with the key.
        """
        store = _load_memory()
        store[key] = value
        try:
            _save_memory(store)
            return json.dumps({"key": key, "status": "written"})
        except Exception as exc:
            return json.dumps({"key": key, "error": str(exc)})

    # ------------------------------------------------------------------
    # Tool: notify_oikos
    # ------------------------------------------------------------------
    @server.tool()
    async def notify_oikos(
        message: str,
        status: str = "info",
    ) -> str:
        """Send a notification to the Oikos coordinator.

        Currently logs the notification locally. Full WebSocket
        delivery to the Oikos UI will be added in a future release.

        Args:
            message: The notification message.
            status: Severity level: info, warning, or error (default info).
        """
        logger.info("Oikos notification [%s]: %s", status, message)
        return json.dumps(
            {
                "status": status,
                "message": message,
                "delivered": False,
                "note": "Notification logged locally. WebSocket delivery not yet implemented.",
            }
        )

    return server
