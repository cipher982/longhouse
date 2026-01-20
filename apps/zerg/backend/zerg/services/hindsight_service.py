"""Hindsight service for analyzing completed agent sessions.

This service implements the "hindsight" capability from the unified agent platform:
- Receives session-ended events from Life Hub
- Analyzes the session for patterns, failures, and improvements
- Creates insights and tasks in Life Hub's work.* schema

Trigger model:
1. Claude Code session ends
2. Life Hub receives final ingest event
3. Life Hub calls Zerg webhook: POST /api/hindsight/session-ended
4. This service analyzes the session
5. Writes insights/tasks back to Life Hub
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from zerg.config import get_settings
from zerg.events import EventType
from zerg.events import event_bus

logger = logging.getLogger(__name__)

# Analysis prompts for different insight types
ANALYSIS_SYSTEM_PROMPT = """You are an AI session analyst. Your job is to review completed coding sessions and identify:

1. **Patterns**: Recurring behaviors, approaches, or code styles that emerge across the session
2. **Failures**: What struggled or failed during the session (errors, retries, dead ends)
3. **Improvements**: Concrete, actionable things that could be improved (tooling, docs, code)
4. **Learnings**: Key insights or knowledge gained that should be remembered

For each insight, provide:
- A clear, concise title (max 80 chars)
- A detailed description
- A severity level: info, warning, or critical
- A confidence score (0-1) in your assessment

Focus on actionable insights that would help improve future sessions."""


async def analyze_session(
    session_id: str,
    project: str | None,
    provider: str,
    events_summary: dict[str, Any],
) -> list[dict[str, Any]]:
    """Analyze a completed session and return insights.

    Args:
        session_id: UUID of the session in Life Hub
        project: Project name (e.g., 'zerg', 'life-hub')
        provider: Agent provider (claude, codex, gemini, etc.)
        events_summary: Summary of session events from Life Hub

    Returns:
        List of insight dictionaries ready to be sent to Life Hub
    """
    settings = get_settings()

    # Skip analysis if no OpenAI key or in testing mode
    if settings.testing or not settings.openai_api_key:
        logger.warning("Skipping hindsight analysis: no OpenAI key or testing mode")
        return []

    # Build the analysis prompt
    user_messages = events_summary.get("user_messages", 0)
    assistant_messages = events_summary.get("assistant_messages", 0)
    tool_calls = events_summary.get("tool_calls", 0)
    duration_minutes = events_summary.get("duration_minutes", 0)
    errors = events_summary.get("errors", [])
    tools_used = events_summary.get("tools_used", [])

    analysis_prompt = f"""Analyze this {provider} coding session:

**Session Info:**
- Project: {project or 'Unknown'}
- Duration: {duration_minutes} minutes
- User messages: {user_messages}
- Assistant messages: {assistant_messages}
- Tool calls: {tool_calls}

**Tools Used:** {', '.join(tools_used) if tools_used else 'None recorded'}

**Errors/Failures:** {len(errors)} recorded
{chr(10).join(f'- {e}' for e in errors[:5]) if errors else 'None recorded'}

**Session Content Summary:**
{events_summary.get('content_summary', 'No summary available')}

Provide 1-3 actionable insights from this session. For each insight, output in this exact JSON format:
```json
{{
  "insight_type": "pattern|failure|improvement|learning",
  "title": "Brief title",
  "description": "Detailed description",
  "severity": "info|warning|critical",
  "confidence": 0.8,
  "create_task": true
}}
```

Only output JSON objects, one per insight. If the session has nothing notable, output an empty list []."""

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-5-mini",  # Fast, cheap model for analysis
                    "messages": [
                        {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
                        {"role": "user", "content": analysis_prompt},
                    ],
                    "temperature": 0.3,
                },
            )
            response.raise_for_status()
            result = response.json()

        # Parse insights from response
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        insights = _parse_insights(content, session_id, project)

        logger.info(
            "Hindsight analysis complete: %d insights for session %s",
            len(insights),
            session_id,
        )
        return insights

    except Exception as e:
        logger.error("Hindsight analysis failed for session %s: %s", session_id, e)
        return []


def _parse_insights(content: str, session_id: str, project: str | None) -> list[dict[str, Any]]:
    """Parse insight JSON objects from LLM response.

    Uses a proper JSON parser that handles nested braces correctly.
    """

    insights = []

    # Try to find JSON objects using a proper bracket-matching approach
    json_objects = _extract_json_objects(content)

    for data in json_objects:
        try:
            # Validate required fields
            if not all(k in data for k in ["insight_type", "title"]):
                continue

            insight = {
                "session_id": session_id,
                "project": project,
                "insight_type": data.get("insight_type", "learning"),
                "title": data.get("title", "Untitled insight")[:200],
                "description": data.get("description"),
                "severity": data.get("severity", "info"),
                "confidence": data.get("confidence", 0.5),
                "observations": {
                    "source": "hindsight",
                    "create_task": data.get("create_task", False),
                },
                "tags": ["hindsight", "automated"],
            }
            insights.append(insight)

        except (TypeError, AttributeError):
            continue

    return insights


def _extract_json_objects(text: str) -> list[dict[str, Any]]:
    """Extract JSON objects from text, handling nested braces correctly.

    This parser properly handles nested objects unlike simple regex patterns.
    """
    import json

    objects = []
    i = 0

    while i < len(text):
        # Find the start of a potential JSON object
        start = text.find("{", i)
        if start == -1:
            break

        # Use bracket counting to find the matching close brace
        depth = 0
        in_string = False
        escape_next = False
        end = start

        for j in range(start, len(text)):
            char = text[j]

            if escape_next:
                escape_next = False
                continue

            if char == "\\":
                escape_next = True
                continue

            if char == '"' and not escape_next:
                in_string = not in_string
                continue

            if in_string:
                continue

            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = j + 1
                    break

        if depth == 0 and end > start:
            try:
                candidate = text[start:end]
                obj = json.loads(candidate)
                if isinstance(obj, dict):
                    objects.append(obj)
                i = end
            except json.JSONDecodeError:
                i = start + 1
        else:
            i = start + 1

    return objects


async def ship_insights_to_lifehub(
    session_id: str,
    insights: list[dict[str, Any]],
) -> dict[str, Any]:
    """Ship analyzed insights to Life Hub.

    Creates insights in work.insights and optionally creates tasks.

    Args:
        session_id: UUID of the analyzed session
        insights: List of insight dictionaries

    Returns:
        Summary of what was created
    """
    settings = get_settings()

    if not settings.lifehub_url:
        logger.warning("Skipping Life Hub shipping: LIFE_HUB_URL not configured")
        return {"error": "Life Hub URL not configured"}

    results = {
        "session_id": session_id,
        "insights_created": 0,
        "tasks_created": 0,
        "errors": [],
    }

    headers = {}
    if settings.lifehub_api_key:
        headers["X-API-Key"] = settings.lifehub_api_key

    async with httpx.AsyncClient(timeout=10.0) as client:
        for insight in insights:
            try:
                # Determine if we should create a task too
                create_task = insight.get("observations", {}).get("create_task", False)

                if create_task:
                    # Use the combined endpoint that creates both
                    response = await client.post(
                        f"{settings.lifehub_url}/api/work/insights-with-task",
                        json=insight,
                        headers=headers,
                        params={"task_priority": 50},
                    )
                    response.raise_for_status()
                    results["insights_created"] += 1
                    results["tasks_created"] += 1
                else:
                    # Just create the insight
                    response = await client.post(
                        f"{settings.lifehub_url}/api/work/insights",
                        json=insight,
                        headers=headers,
                    )
                    response.raise_for_status()
                    results["insights_created"] += 1

            except Exception as e:
                error_msg = f"Failed to create insight: {e}"
                logger.error(error_msg)
                results["errors"].append(error_msg)

    logger.info(
        "Shipped hindsight results to Life Hub: %d insights, %d tasks for session %s",
        results["insights_created"],
        results["tasks_created"],
        session_id,
    )

    return results


async def run_hindsight_analysis(
    session_id: str,
    project: str | None,
    provider: str,
    events_summary: dict[str, Any],
) -> dict[str, Any]:
    """Run the full hindsight analysis pipeline.

    This is the main entry point called when a session-ended event is received.

    Args:
        session_id: UUID of the session in Life Hub
        project: Project name
        provider: Agent provider
        events_summary: Summary of session events

    Returns:
        Summary of analysis results
    """
    # Publish start event
    await event_bus.publish(
        EventType.HINDSIGHT_STARTED,
        {
            "session_id": session_id,
            "project": project,
            "provider": provider,
        },
    )

    try:
        # Analyze the session
        insights = await analyze_session(
            session_id=session_id,
            project=project,
            provider=provider,
            events_summary=events_summary,
        )

        # Ship insights to Life Hub
        if insights:
            results = await ship_insights_to_lifehub(session_id, insights)
        else:
            results = {
                "session_id": session_id,
                "insights_created": 0,
                "tasks_created": 0,
                "message": "No notable insights found",
            }

        # Publish completion event
        await event_bus.publish(
            EventType.HINDSIGHT_COMPLETE,
            {
                "session_id": session_id,
                "project": project,
                "insights_count": len(insights),
                "tasks_created": results.get("tasks_created", 0),
            },
        )

        return results

    except Exception as e:
        logger.exception("Hindsight analysis failed for session %s", session_id)
        return {
            "session_id": session_id,
            "error": str(e),
        }


def schedule_hindsight_analysis(
    session_id: str,
    project: str | None,
    provider: str,
    events_summary: dict[str, Any],
) -> None:
    """Schedule hindsight analysis without blocking the caller.

    This is the fire-and-forget entry point for the webhook handler.
    """
    settings = get_settings()
    if settings.testing:
        return

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(
            run_hindsight_analysis(
                session_id=session_id,
                project=project,
                provider=provider,
                events_summary=events_summary,
            )
        )
    except RuntimeError:
        logger.debug("No event loop; skipping hindsight analysis for session %s", session_id)


__all__ = [
    "analyze_session",
    "run_hindsight_analysis",
    "schedule_hindsight_analysis",
    "ship_insights_to_lifehub",
]
