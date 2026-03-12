"""Longhouse MCP server — exposes continuity/search tools for CLI agents.

Uses the ``mcp`` SDK's ``FastMCP`` decorator pattern to register tools.
All tools return JSON strings.
"""

from __future__ import annotations

import json
import logging
import re

from mcp.server.fastmcp import FastMCP

from zerg.mcp_server.api_client import LonghouseAPIClient

# UUID v4 pattern for input validation
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

logger = logging.getLogger(__name__)


def _truncate_event(event: dict, max_chars: int, include_tool_output: bool) -> dict:
    """Truncate large content fields in an event dict.

    Fields longer than max_chars are truncated and annotated with
    _<field>_truncated=True and _<field>_full_chars=N so callers
    know content was cut and can re-request with a larger limit.
    """
    result = dict(event)
    if not include_tool_output:
        result.pop("tool_output_text", None)
    for field in ("content_text", "tool_output_text"):
        val = result.get(field)
        if val and isinstance(val, str) and len(val) > max_chars:
            result[field] = val[:max_chars]
            result[f"_{field}_truncated"] = True
            result[f"_{field}_full_chars"] = len(val)
    return result


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
        context_mode: str = "forensic",
    ) -> str:
        """Search past agent sessions by content.

        Returns session metadata (dates, provider, message counts, snippet) — not event content.
        Use for session discovery: "which sessions touched project X?" or "did anyone work on Y?"
        NOT for reading event content → use recall for that.

        Args:
            query: Text to search for in session content.
            project: Filter by project name (optional).
            provider: Filter by provider, e.g. claude, codex, gemini (optional).
            days_back: Number of days to look back (default 14).
            limit: Maximum results to return (default 10).
            semantic: Use semantic (embedding) search instead of text search (default False).
            context_mode: Context projection mode: forensic|active_context (default forensic).
        """
        if context_mode not in {"forensic", "active_context"}:
            return json.dumps({"error": "context_mode must be one of: forensic, active_context"})

        params: dict = {
            "query": query,
            "days_back": days_back,
            "limit": limit,
            "context_mode": context_mode,
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
        max_events: int = 20,
        roles: str | None = None,
        include_tool_output: bool = True,
        max_content_chars: int = 400,
        context_mode: str = "forensic",
        branch_mode: str = "head",
    ) -> str:
        """Full ordered replay of a session. Loads complete event stream in sequence.

        EXPENSIVE — each event can be hundreds to thousands of chars.
        - NOT for content search → use recall (fuzzy) or query_agents (exact SQL)
        - NOT for finding events by tool name → use get_session_events

        Use this only to understand session flow or debug tool-call sequences.

        Args:
            session_id: UUID of the session to retrieve.
            max_events: Max events to load (default 20). Keep low — each event can be large.
            roles: Comma-separated role filter, e.g. "assistant,tool" (optional).
            include_tool_output: Set False to omit tool_output_text entirely (saves tokens).
            max_content_chars: Truncate content_text and tool_output_text at this length.
                Truncated fields get _<field>_truncated=True and _<field>_full_chars=N added.
            context_mode: Context projection mode: forensic|active_context (default forensic).
            branch_mode: Branch projection mode: head|all (default head).
        """
        # Validate session_id to prevent path injection
        if not _UUID_RE.match(session_id):
            return json.dumps({"error": "Invalid session_id format. Expected a UUID."})
        if context_mode not in {"forensic", "active_context"}:
            return json.dumps({"error": "context_mode must be one of: forensic, active_context"})
        if branch_mode not in {"head", "all"}:
            return json.dumps({"error": "branch_mode must be one of: head, all"})

        try:
            # Fetch session metadata
            meta_resp = await client.get(f"/api/agents/sessions/{session_id}")
            if meta_resp.status_code != 200:
                return json.dumps({"error": f"Session not found: {meta_resp.status_code}"})

            # Fetch session events
            params: dict = {"limit": max_events, "context_mode": context_mode, "branch_mode": branch_mode}
            if roles:
                params["roles"] = roles
            events_resp = await client.get(
                f"/api/agents/sessions/{session_id}/events",
                params=params,
            )
            if events_resp.status_code != 200:
                return json.dumps({"error": f"Events fetch failed: {events_resp.status_code}"})

            session = meta_resp.json()
            events_data = events_resp.json()
            events = [_truncate_event(e, max_content_chars, include_tool_output) for e in events_data.get("events", [])]

            return json.dumps(
                {
                    "session": session,
                    "events": events,
                    "total_events": events_data.get("total", 0),
                }
            )
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    # ------------------------------------------------------------------
    # Tool: get_session_events
    # ------------------------------------------------------------------
    @server.tool()
    async def get_session_events(
        session_id: str,
        query: str | None = None,
        tool_name: str | None = None,
        roles: str | None = None,
        limit: int = 20,
        offset: int = 0,
        max_content_chars: int = 400,
        context_mode: str = "forensic",
        branch_mode: str = "head",
    ) -> str:
        """Surgical event search within a known session.

        Use when you have a session ID and need specific events.
        - Filter by tool name: tool_name="Bash"
        - Search content: query="sk_live"
        - Combine filters: tool_name="Bash", query="grep"
        - Paginate: offset=20 to get next page

        NOT for cross-session search → use recall or search_sessions instead.
        NOT for full session replay → use get_session_detail instead.

        Args:
            session_id: UUID of the session to search within.
            query: Content search string (searches content_text and tool_output_text).
            tool_name: Filter by exact tool name, e.g. "Bash", "Edit", "Read".
            roles: Comma-separated role filter, e.g. "tool" for tool results only.
            limit: Max events to return (default 20).
            offset: Pagination offset (default 0).
            max_content_chars: Truncate content fields at this length (default 400).
            context_mode: Context projection mode: forensic|active_context (default forensic).
            branch_mode: Branch projection mode: head|all (default head).
        """
        if not _UUID_RE.match(session_id):
            return json.dumps({"error": "Invalid session_id format. Expected a UUID."})
        if context_mode not in {"forensic", "active_context"}:
            return json.dumps({"error": "context_mode must be one of: forensic, active_context"})
        if branch_mode not in {"head", "all"}:
            return json.dumps({"error": "branch_mode must be one of: head, all"})

        try:
            params: dict = {"limit": limit, "offset": offset, "context_mode": context_mode, "branch_mode": branch_mode}
            if query:
                params["query"] = query
            if tool_name:
                params["tool_name"] = tool_name
            if roles:
                params["roles"] = roles

            events_resp = await client.get(
                f"/api/agents/sessions/{session_id}/events",
                params=params,
            )
            if events_resp.status_code != 200:
                return json.dumps({"error": f"API returned {events_resp.status_code}", "detail": events_resp.text[:500]})

            data = events_resp.json()
            events = [_truncate_event(e, max_content_chars, True) for e in data.get("events", [])]
            return json.dumps(
                {
                    "events": events,
                    "total": data.get("total", 0),
                    "returned": len(events),
                    "offset": offset,
                }
            )
        except Exception as exc:
            return json.dumps({"error": str(exc)})

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
    # Tool: recall
    # ------------------------------------------------------------------
    @server.tool()
    async def recall(
        query: str,
        project: str | None = None,
        since_days: int = 90,
        max_results: int = 5,
        context_turns: int = 2,
        context_mode: str = "forensic",
    ) -> str:
        """Retrieve knowledge from past AI sessions by searching conversation content.

        Semantic/fuzzy search — returns actual conversation content around relevant turns.
        Use when you don't know the exact phrase but know the concept: "what was decided about auth?"
        NOT for exact string match → use query_agents SQL with ILIKE for that.
        NOT for session discovery → use search_sessions for that.

        Args:
            query: Natural language description of what you are looking for.
            project: Filter by project name (optional).
            since_days: Days to look back (default 90).
            max_results: Max sessions to return content from (default 5).
            context_turns: Turns before/after match to include (default 2).
            context_mode: Context projection mode: forensic|active_context (default forensic).
        """
        if context_mode not in {"forensic", "active_context"}:
            return json.dumps({"error": "context_mode must be one of: forensic, active_context"})

        params: dict = {
            "query": query,
            "since_days": since_days,
            "max_results": max_results,
            "context_turns": context_turns,
            "context_mode": context_mode,
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
