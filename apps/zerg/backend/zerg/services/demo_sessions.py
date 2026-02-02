"""Demo agent session builders for DB seeding."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone

from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest


def build_demo_agent_sessions(now: datetime | None = None) -> list[SessionIngest]:
    """Return demo agent sessions for seeding a demo DB.

    Keeps data deterministic relative to `now` for stable demos.
    """
    anchor = now or datetime.now(timezone.utc)
    base_one = anchor - timedelta(hours=2, minutes=10)
    base_two = anchor - timedelta(hours=1, minutes=5)

    session_one_events = [
        EventIngest(
            role="user",
            content_text="Scan the repo and propose onboarding improvements.",
            timestamp=base_one + timedelta(minutes=1),
        ),
        EventIngest(
            role="assistant",
            content_text="Got it. I'll review the docs, identify gaps, and outline a plan.",
            timestamp=base_one + timedelta(minutes=2),
        ),
        EventIngest(
            role="assistant",
            content_text="Searching for onboarding references...",
            tool_name="Bash",
            tool_input_json={"command": 'rg -n "onboarding|install" -S .'},
            timestamp=base_one + timedelta(minutes=3),
        ),
        EventIngest(
            role="tool",
            tool_name="Bash",
            tool_output_text="VISION.md\nscripts/validate-setup.sh",
            timestamp=base_one + timedelta(minutes=4),
        ),
        EventIngest(
            role="assistant",
            content_text="Found onboarding gaps in the README and missing a quick-start path. Drafting a minimal task plan and finish conditions.",
            timestamp=base_one + timedelta(minutes=6),
        ),
    ]

    session_two_events = [
        EventIngest(
            role="user",
            content_text="Show me the most recent agent sessions and their tool usage.",
            timestamp=base_two + timedelta(minutes=1),
        ),
        EventIngest(
            role="assistant",
            content_text="Pulling session metadata and tool calls...",
            tool_name="Read",
            tool_input_json={"path": "agents.sessions", "limit": 5},
            timestamp=base_two + timedelta(minutes=2),
        ),
        EventIngest(
            role="tool",
            tool_name="Read",
            tool_output_text="5 sessions, 18 tool calls, avg 9m duration.",
            timestamp=base_two + timedelta(minutes=3),
        ),
        EventIngest(
            role="assistant",
            content_text="Here are the recent sessions with tool usage. You can filter by provider and project from the timeline.",
            timestamp=base_two + timedelta(minutes=4),
        ),
    ]

    return [
        SessionIngest(
            provider="claude",
            environment="development",
            project="longhouse-demo",
            device_id="demo-mac",
            cwd="/Users/demo/longhouse",
            git_repo="https://github.com/cipher982/longhouse",
            git_branch="main",
            started_at=base_one,
            ended_at=base_one + timedelta(minutes=18),
            provider_session_id="demo-claude-01",
            events=session_one_events,
        ),
        SessionIngest(
            provider="codex",
            environment="development",
            project="longhouse-demo",
            device_id="demo-mac",
            cwd="/Users/demo/longhouse",
            git_repo="https://github.com/cipher982/longhouse",
            git_branch="feature/onboarding",
            started_at=base_two,
            ended_at=base_two + timedelta(minutes=12),
            provider_session_id="demo-codex-01",
            events=session_two_events,
        ),
    ]
