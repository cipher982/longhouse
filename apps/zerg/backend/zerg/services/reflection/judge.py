"""LLM analysis of session batches to produce structured reflection actions.

Takes a ProjectBatch (session summaries + existing insights) and returns
a list of actions: create_insight, merge, or skip.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from zerg.services.reflection.collector import ProjectBatch

logger = logging.getLogger(__name__)

REFLECTION_PROMPT = """\
You are an AI session analyst. You review summaries of recent AI coding sessions \
and extract reusable learnings, patterns, and failure modes.

## Sessions to Analyze

Project: {project}

{session_block}

## Existing Insights (for dedup — do NOT re-create these)

{existing_block}

## Instructions

For each meaningful insight from the sessions above, output ONE action:

1. **create_insight** — A genuinely new learning not covered by existing insights.
2. **merge** — An observation that reinforces an existing insight (reference its ID).
3. **skip** — Not worth logging (too trivial, project-specific one-off, etc).

Focus on:
- Recurring bugs or gotchas (things that tripped up multiple sessions)
- Tools or techniques that worked well
- Patterns in how problems were solved
- Infrastructure/deployment issues that keep coming up
- Mistakes that could be prevented with better context

DO NOT create insights for:
- Session-specific implementation details (e.g., "added a button to page X")
- One-off debugging that won't recur
- Things already captured in existing insights

## Action Proposals

For high-confidence insights (confidence >= 0.8) that have a clear, actionable fix,
include an `action_blurb` field — a 1-2 sentence description of what should be done.

Examples of good action blurbs:
- "Add UFW allow rule for 172.16.0.0/12 to the deploy checklist in AGENTS.md"
- "Create a pre-commit hook that checks for timezone-naive datetime usage"
- "Add retry logic to the session ingest endpoint for transient DB failures"

Only include action_blurb when the action is concrete and scoped. Do NOT suggest
vague actions like "improve error handling" or "refactor the auth module."

## Output Format

Return a JSON array of actions. Each action is an object:

```json
[
  {{
    "action": "create_insight",
    "insight_type": "pattern|failure|improvement|learning",
    "title": "Short, reusable title",
    "description": "What was learned and why it matters",
    "severity": "info|warning|critical",
    "confidence": 0.7,
    "tags": ["optional", "tags"],
    "action_blurb": "Optional: concrete 1-2 sentence action to take"
  }},
  {{
    "action": "merge",
    "insight_id": "existing-insight-uuid",
    "observation": "New evidence or context to append"
  }},
  {{
    "action": "skip",
    "reason": "Why this wasn't worth logging"
  }}
]
```

Return ONLY the JSON array, no other text.\
"""


def _build_session_block(batch: ProjectBatch) -> str:
    """Format session summaries for the prompt."""
    lines = []
    for s in batch.sessions:
        title = s.summary_title or "Untitled"
        lines.append(f"### {title}")
        lines.append(f"Provider: {s.provider} | Messages: {s.user_messages} | Tool calls: {s.tool_calls}")
        lines.append(s.summary)
        lines.append("")
    return "\n".join(lines)


def _build_existing_block(batch: ProjectBatch) -> str:
    """Format existing insights for the dedup section."""
    if not batch.existing_insights:
        return "(none)"
    lines = []
    for i in batch.existing_insights:
        tags = ", ".join(i.get("tags") or [])
        lines.append(f"- [{i['id']}] ({i['insight_type']}) {i['title']}")
        if i.get("description"):
            lines.append(f"  {i['description'][:200]}")
        if tags:
            lines.append(f"  Tags: {tags}")
    return "\n".join(lines)


async def analyze_sessions(
    batch: ProjectBatch,
    llm_client: Any = None,
    model: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Analyze a project batch with LLM and return structured actions.

    Args:
        batch: Sessions and existing insights for one project.
        llm_client: Async OpenAI-compatible client.
        model: Model ID for the completion.

    Returns:
        Tuple of (actions list, usage dict with prompt_tokens/completion_tokens).
    """
    if not batch.sessions:
        return [], {"prompt_tokens": 0, "completion_tokens": 0}

    project_name = batch.project or "(cross-project)"
    session_block = _build_session_block(batch)
    existing_block = _build_existing_block(batch)

    prompt = REFLECTION_PROMPT.format(
        project=project_name,
        session_block=session_block,
        existing_block=existing_block,
    )

    if llm_client is None:
        logger.warning("No LLM client provided for reflection, returning empty actions")
        return [], {"prompt_tokens": 0, "completion_tokens": 0}

    try:
        response = await llm_client.chat.completions.create(
            model=model or "gpt-5-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            response_format={"type": "json_object"},
            extra_body={"metadata": {"source": "longhouse:reflection"}},
        )

        usage = {
            "prompt_tokens": getattr(response.usage, "prompt_tokens", 0) if response.usage else 0,
            "completion_tokens": getattr(response.usage, "completion_tokens", 0) if response.usage else 0,
        }

        raw = response.choices[0].message.content or "[]"
        actions = _parse_actions(raw, batch.project)
        return actions, usage

    except Exception:
        logger.exception("LLM call failed for reflection on project %s", project_name)
        return [], {"prompt_tokens": 0, "completion_tokens": 0}


def _parse_actions(raw: str, project: str | None) -> list[dict[str, Any]]:
    """Parse LLM JSON response into validated action dicts.

    Handles both raw arrays and {"actions": [...]} wrapper objects.
    Gracefully handles malformed JSON by returning empty list.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Failed to parse LLM reflection JSON: %s", raw[:500])
        return []

    # Handle wrapper object
    if isinstance(parsed, dict):
        parsed = parsed.get("actions", parsed.get("insights", []))

    if not isinstance(parsed, list):
        logger.warning("Expected JSON array from LLM, got %s", type(parsed).__name__)
        return []

    actions = []
    for item in parsed:
        if not isinstance(item, dict):
            continue

        action_type = item.get("action")
        if action_type not in ("create_insight", "merge", "skip"):
            continue

        # Tag all actions with source project
        item["project"] = project

        # Validate action_blurb is a string if present
        if "action_blurb" in item:
            if not isinstance(item["action_blurb"], str) or not item["action_blurb"].strip():
                del item["action_blurb"]

        actions.append(item)

    return actions


__all__ = ["analyze_sessions"]
