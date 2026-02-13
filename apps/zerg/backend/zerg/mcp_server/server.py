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
        semantic: bool = False,
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
            semantic: Use semantic (embedding) search instead of text search (default False).
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

        path = "/api/agents/sessions/semantic" if semantic else "/api/agents/sessions"

        try:
            resp = await client.get(path, params=params)
            if resp.status_code != 200:
                # Fall back to FTS if semantic search unavailable
                if semantic:
                    resp = await client.get("/api/agents/sessions", params=params)
                    if resp.status_code != 200:
                        return json.dumps({"error": f"API returned {resp.status_code}", "detail": resp.text[:500]})
                else:
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

    # ------------------------------------------------------------------
    # Tool: log_insight
    # ------------------------------------------------------------------
    @server.tool()
    async def log_insight(
        insight_type: str,
        title: str,
        description: str | None = None,
        project: str | None = None,
        severity: str = "info",
        confidence: float | None = None,
        session_id: str | None = None,
        tags: list[str] | None = None,
    ) -> str:
        """Log a learning, pattern, failure, or improvement insight.

        Deduplicates by title+project: if an insight with the same title and
        project exists within the last 7 days, updates confidence and appends
        to observations instead of creating a duplicate.

        Args:
            insight_type: Type of insight: pattern, failure, improvement, learning.
            title: Short summary of the insight.
            description: Detailed explanation (optional).
            project: Project name (optional).
            severity: Severity level: info, warning, critical (default info).
            confidence: Confidence score 0.0-1.0 (optional).
            session_id: UUID of the source session (optional).
            tags: List of tags for categorization (optional).
        """
        payload: dict = {
            "insight_type": insight_type,
            "title": title,
            "severity": severity,
        }
        if description is not None:
            payload["description"] = description
        if project is not None:
            payload["project"] = project
        if confidence is not None:
            payload["confidence"] = confidence
        if session_id is not None:
            payload["session_id"] = session_id
        if tags is not None:
            payload["tags"] = tags

        try:
            resp = await client.post("/api/insights", json=payload)
            if resp.status_code not in (200, 201):
                return json.dumps({"error": f"API returned {resp.status_code}", "detail": resp.text[:500]})
            return resp.text
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    # ------------------------------------------------------------------
    # Tool: query_insights
    # ------------------------------------------------------------------
    @server.tool()
    async def query_insights(
        project: str | None = None,
        insight_type: str | None = None,
        since_hours: int = 168,
        limit: int = 20,
    ) -> str:
        """Query past insights and learnings. Check before starting work for known gotchas.

        Args:
            project: Filter by project name (optional).
            insight_type: Filter by type: pattern, failure, improvement, learning (optional).
            since_hours: Hours to look back (default 168 = 7 days).
            limit: Maximum results to return (default 20).
        """
        params: dict = {
            "since_hours": since_hours,
            "limit": limit,
        }
        if project is not None:
            params["project"] = project
        if insight_type is not None:
            params["insight_type"] = insight_type

        try:
            resp = await client.get("/api/insights", params=params)
            if resp.status_code != 200:
                return json.dumps({"error": f"API returned {resp.status_code}", "detail": resp.text[:500]})
            return resp.text
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    # ------------------------------------------------------------------
    # Tool: reserve_file
    # ------------------------------------------------------------------
    @server.tool()
    async def reserve_file(
        file_path: str,
        project: str | None = None,
        agent: str = "claude",
        reason: str | None = None,
        duration_minutes: int = 60,
    ) -> str:
        """Reserve a file to prevent edit conflicts in multi-agent workflows.

        Call this before editing a file to claim exclusive access.
        Other agents will see the reservation and know to wait.

        Args:
            file_path: Path to the file to reserve.
            project: Project context (optional).
            agent: Agent name (default claude).
            reason: Why you are reserving it (optional).
            duration_minutes: How long to hold the reservation (default 60).
        """
        payload: dict = {
            "file_path": file_path,
            "project": project or "",
            "agent": agent,
            "duration_minutes": duration_minutes,
        }
        if reason is not None:
            payload["reason"] = reason

        try:
            resp = await client.post("/api/reservations", json=payload)
            if resp.status_code not in (200, 201):
                return json.dumps({"error": f"API returned {resp.status_code}", "detail": resp.text[:500]})
            return resp.text
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    # ------------------------------------------------------------------
    # Tool: check_reservation
    # ------------------------------------------------------------------
    @server.tool()
    async def check_reservation(
        file_path: str,
        project: str | None = None,
    ) -> str:
        """Check if a file is currently reserved by another agent.

        Call this before editing to see if another agent is working on a file.

        Args:
            file_path: Path to the file to check.
            project: Project context (optional).
        """
        params: dict = {"file_path": file_path}
        if project is not None:
            params["project"] = project

        try:
            resp = await client.get("/api/reservations/check", params=params)
            if resp.status_code != 200:
                return json.dumps({"error": f"API returned {resp.status_code}", "detail": resp.text[:500]})
            return resp.text
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    # ------------------------------------------------------------------
    # Tool: release_reservation
    # ------------------------------------------------------------------
    @server.tool()
    async def release_reservation(
        reservation_id: str,
    ) -> str:
        """Release a file reservation so other agents can proceed.

        Call this when you are done editing to let other agents work on the file.

        Args:
            reservation_id: UUID of the reservation to release.
        """
        if not _UUID_RE.match(reservation_id):
            return json.dumps({"error": "Invalid reservation_id format. Expected a UUID."})

        try:
            resp = await client.delete(f"/api/reservations/{reservation_id}")
            if resp.status_code != 200:
                return json.dumps({"error": f"API returned {resp.status_code}", "detail": resp.text[:500]})
            return resp.text
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    # ------------------------------------------------------------------
    # Tool: recall
    # ------------------------------------------------------------------
    @server.tool()
    async def recall(
        query: str,
        project: str | None = None,
        since_days: int = 90,
        max_results: int = 5,
        context_turns: int = 2,
    ) -> str:
        """Retrieve knowledge from past AI sessions by searching conversation content.

        Unlike search_sessions (which returns session metadata), recall returns the actual
        conversation content around the most relevant turns. Use this when you need to
        extract specific knowledge, decisions, or solutions from past work.

        Args:
            query: Natural language description of what you are looking for.
            project: Filter by project name (optional).
            since_days: Days to look back (default 90).
            max_results: Max sessions to return content from (default 5).
            context_turns: Turns before/after match to include (default 2).
        """
        params: dict = {
            "query": query,
            "since_days": since_days,
            "max_results": max_results,
            "context_turns": context_turns,
        }
        if project:
            params["project"] = project

        try:
            resp = await client.get("/api/agents/recall", params=params)
            if resp.status_code != 200:
                return json.dumps({"error": f"API returned {resp.status_code}", "detail": resp.text[:500]})
            return resp.text
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    return server
