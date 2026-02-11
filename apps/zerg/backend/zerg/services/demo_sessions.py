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
    Provides 7 sessions across multiple providers, projects, and tool types
    for a visually compelling timeline.
    """
    anchor = now or datetime.now(timezone.utc)

    # Session 1: Claude — Onboarding review (Bash + Read)
    t1 = anchor - timedelta(hours=3, minutes=40)
    s1_events = [
        EventIngest(
            role="user",
            content_text="Scan the repo and propose onboarding improvements.",
            timestamp=t1 + timedelta(minutes=1),
        ),
        EventIngest(
            role="assistant",
            content_text="Got it. I'll review the docs, identify gaps, and outline a plan.",
            timestamp=t1 + timedelta(minutes=2),
        ),
        EventIngest(
            role="assistant",
            content_text="Searching for onboarding references...",
            tool_name="Bash",
            tool_input_json={"command": 'rg -n "onboarding|install" -S .'},
            timestamp=t1 + timedelta(minutes=3),
        ),
        EventIngest(
            role="tool",
            tool_name="Bash",
            tool_output_text="VISION.md:14:## Onboarding\nscripts/validate-setup.sh:1:#!/bin/bash\nREADME.md:42:## Installation",
            timestamp=t1 + timedelta(minutes=4),
        ),
        EventIngest(
            role="assistant",
            content_text="Reading the current onboarding docs...",
            tool_name="Read",
            tool_input_json={"file_path": "README.md"},
            timestamp=t1 + timedelta(minutes=5),
        ),
        EventIngest(
            role="tool",
            tool_name="Read",
            tool_output_text="# Longhouse\n\nAI agent orchestration platform.\n\n## Quick Start\n\n(empty section)",
            timestamp=t1 + timedelta(minutes=5, seconds=30),
        ),
        EventIngest(
            role="assistant",
            content_text="Found onboarding gaps: README Quick Start is empty, no `longhouse onboard` mention, and validate-setup.sh doesn't check for required env vars. Drafting improvement plan.",
            timestamp=t1 + timedelta(minutes=7),
        ),
    ]

    # Session 2: Codex — API endpoint refactor (Edit + Grep + Bash)
    t2 = anchor - timedelta(hours=2, minutes=55)
    s2_events = [
        EventIngest(
            role="user",
            content_text="Refactor the /api/agents/sessions endpoint to support cursor-based pagination.",
            timestamp=t2 + timedelta(minutes=1),
        ),
        EventIngest(
            role="assistant",
            content_text="I'll find the current implementation and add cursor pagination.",
            timestamp=t2 + timedelta(minutes=2),
        ),
        EventIngest(
            role="assistant",
            content_text="Finding the sessions endpoint...",
            tool_name="Grep",
            tool_input_json={"pattern": "def list_sessions", "path": "zerg/routers/"},
            timestamp=t2 + timedelta(minutes=3),
        ),
        EventIngest(
            role="tool",
            tool_name="Grep",
            tool_output_text="zerg/routers/agents.py:145:async def list_sessions(",
            timestamp=t2 + timedelta(minutes=3, seconds=20),
        ),
        EventIngest(
            role="assistant",
            content_text="Reading the current endpoint...",
            tool_name="Read",
            tool_input_json={"file_path": "zerg/routers/agents.py", "offset": 145, "limit": 40},
            timestamp=t2 + timedelta(minutes=4),
        ),
        EventIngest(
            role="tool",
            tool_name="Read",
            tool_output_text="async def list_sessions(\n    limit: int = 50,\n    offset: int = 0,\n):\n    ...",
            timestamp=t2 + timedelta(minutes=4, seconds=15),
        ),
        EventIngest(
            role="assistant",
            content_text="Adding cursor parameter and updating the query...",
            tool_name="Edit",
            tool_input_json={
                "file_path": "zerg/routers/agents.py",
                "old_string": "offset: int = 0",
                "new_string": "cursor: str | None = None",
            },
            timestamp=t2 + timedelta(minutes=6),
        ),
        EventIngest(
            role="tool",
            tool_name="Edit",
            tool_output_text="File updated successfully.",
            timestamp=t2 + timedelta(minutes=6, seconds=10),
        ),
        EventIngest(
            role="assistant",
            content_text="Running tests to verify...",
            tool_name="Bash",
            tool_input_json={"command": "make test"},
            timestamp=t2 + timedelta(minutes=8),
        ),
        EventIngest(
            role="tool",
            tool_name="Bash",
            tool_output_text="42 passed, 0 failed in 12.3s",
            timestamp=t2 + timedelta(minutes=10),
        ),
        EventIngest(
            role="assistant",
            content_text="Pagination refactored. The endpoint now accepts an opaque `cursor` string instead of `offset`. Tests pass. The cursor encodes (started_at, id) for stable ordering.",
            timestamp=t2 + timedelta(minutes=11),
        ),
    ]

    # Session 3: Gemini — FTS5 search implementation (Bash + Write + Read)
    t3 = anchor - timedelta(hours=2, minutes=10)
    s3_events = [
        EventIngest(
            role="user",
            content_text="Add full-text search to the sessions timeline using SQLite FTS5.",
            timestamp=t3 + timedelta(minutes=1),
        ),
        EventIngest(
            role="assistant",
            content_text="I'll create an FTS5 virtual table and add search to the agents store.",
            timestamp=t3 + timedelta(minutes=2),
        ),
        EventIngest(
            role="assistant",
            content_text="Checking if FTS5 is available...",
            tool_name="Bash",
            tool_input_json={"command": 'python3 -c "import sqlite3; print(sqlite3.sqlite_version)"'},
            timestamp=t3 + timedelta(minutes=3),
        ),
        EventIngest(
            role="tool",
            tool_name="Bash",
            tool_output_text="3.45.1",
            timestamp=t3 + timedelta(minutes=3, seconds=15),
        ),
        EventIngest(
            role="assistant",
            content_text="Creating the FTS5 virtual table and triggers...",
            tool_name="Write",
            tool_input_json={
                "file_path": "zerg/database.py",
                "content": "CREATE VIRTUAL TABLE events_fts USING fts5(content_text, content=events)",
            },
            timestamp=t3 + timedelta(minutes=5),
        ),
        EventIngest(
            role="tool",
            tool_name="Write",
            tool_output_text="File written successfully.",
            timestamp=t3 + timedelta(minutes=5, seconds=10),
        ),
        EventIngest(
            role="assistant",
            content_text="FTS5 search is now available. Sessions can be searched by event content with BM25 ranking and snippet generation.",
            timestamp=t3 + timedelta(minutes=8),
        ),
    ]

    # Session 4: Claude — Bug fix with debugging (Bash + Grep + Edit)
    t4 = anchor - timedelta(hours=1, minutes=30)
    s4_events = [
        EventIngest(
            role="user",
            content_text="The health endpoint is returning 503 after deploying. Debug and fix it.",
            timestamp=t4 + timedelta(minutes=1),
        ),
        EventIngest(
            role="assistant",
            content_text="Let me check the health endpoint and recent logs.",
            timestamp=t4 + timedelta(minutes=2),
        ),
        EventIngest(
            role="assistant",
            content_text="Hitting the health endpoint...",
            tool_name="Bash",
            tool_input_json={"command": "curl -v http://localhost:8080/api/health"},
            timestamp=t4 + timedelta(minutes=3),
        ),
        EventIngest(
            role="tool",
            tool_name="Bash",
            tool_output_text='< HTTP/1.1 503 Service Unavailable\n{"status":"unhealthy","detail":"database connection failed"}',
            timestamp=t4 + timedelta(minutes=3, seconds=20),
        ),
        EventIngest(
            role="assistant",
            content_text="Database connection issue. Checking the config...",
            tool_name="Grep",
            tool_input_json={"pattern": "DATABASE_URL", "path": ".env"},
            timestamp=t4 + timedelta(minutes=4),
        ),
        EventIngest(
            role="tool",
            tool_name="Grep",
            tool_output_text=".env:3:DATABASE_URL=postgresql://...",
            timestamp=t4 + timedelta(minutes=4, seconds=10),
        ),
        EventIngest(
            role="assistant",
            content_text="Found it \u2014 .env has a Postgres URL but we're running SQLite mode. Commenting it out...",
            tool_name="Edit",
            tool_input_json={
                "file_path": ".env",
                "old_string": "DATABASE_URL=postgresql://...",
                "new_string": "# DATABASE_URL=postgresql://...",
            },
            timestamp=t4 + timedelta(minutes=5),
        ),
        EventIngest(
            role="tool",
            tool_name="Edit",
            tool_output_text="File updated successfully.",
            timestamp=t4 + timedelta(minutes=5, seconds=10),
        ),
        EventIngest(
            role="assistant",
            content_text="Verifying the fix...",
            tool_name="Bash",
            tool_input_json={"command": "curl -s http://localhost:8080/api/health | jq ."},
            timestamp=t4 + timedelta(minutes=6),
        ),
        EventIngest(
            role="tool",
            tool_name="Bash",
            tool_output_text='{"status":"healthy","version":"0.12.0","database":"sqlite"}',
            timestamp=t4 + timedelta(minutes=6, seconds=15),
        ),
        EventIngest(
            role="assistant",
            content_text="Fixed. The .env file had a stale DATABASE_URL pointing to Postgres. Commented it out and health endpoint now returns 200.",
            timestamp=t4 + timedelta(minutes=7),
        ),
    ]

    # Session 5: Codex — Test suite expansion (Glob + Read + Write + Bash)
    t5 = anchor - timedelta(hours=1, minutes=5)
    s5_events = [
        EventIngest(
            role="user",
            content_text="Show me the most recent agent sessions and their tool usage.",
            timestamp=t5 + timedelta(minutes=1),
        ),
        EventIngest(
            role="assistant",
            content_text="Pulling session metadata and tool calls...",
            tool_name="Read",
            tool_input_json={"path": "agents.sessions", "limit": 5},
            timestamp=t5 + timedelta(minutes=2),
        ),
        EventIngest(
            role="tool",
            tool_name="Read",
            tool_output_text="5 sessions, 18 tool calls, avg 9m duration.",
            timestamp=t5 + timedelta(minutes=3),
        ),
        EventIngest(
            role="assistant",
            content_text="Here are the recent sessions with tool usage. You can filter by provider and project from the timeline.",
            timestamp=t5 + timedelta(minutes=4),
        ),
    ]

    # Session 6: Gemini — Documentation update (Glob + Read + Edit)
    t6 = anchor - timedelta(minutes=45)
    s6_events = [
        EventIngest(
            role="user",
            content_text="Update AGENTS.md to document the new FTS5 search feature.",
            timestamp=t6 + timedelta(minutes=1),
        ),
        EventIngest(
            role="assistant",
            content_text="Finding the right section in AGENTS.md to add documentation.",
            timestamp=t6 + timedelta(minutes=2),
        ),
        EventIngest(
            role="assistant",
            content_text="Looking for the feature index section...",
            tool_name="Glob",
            tool_input_json={"pattern": "**/AGENTS.md"},
            timestamp=t6 + timedelta(minutes=3),
        ),
        EventIngest(
            role="tool",
            tool_name="Glob",
            tool_output_text="AGENTS.md",
            timestamp=t6 + timedelta(minutes=3, seconds=10),
        ),
        EventIngest(
            role="assistant",
            content_text="Reading the Feature Index section...",
            tool_name="Read",
            tool_input_json={"file_path": "AGENTS.md", "offset": 100, "limit": 30},
            timestamp=t6 + timedelta(minutes=4),
        ),
        EventIngest(
            role="tool",
            tool_name="Read",
            tool_output_text="## Feature Index\n\n**Backend Services:**\n| Feature | Location | Notes |",
            timestamp=t6 + timedelta(minutes=4, seconds=15),
        ),
        EventIngest(
            role="assistant",
            content_text="Adding FTS5 search to the feature index...",
            tool_name="Edit",
            tool_input_json={
                "file_path": "AGENTS.md",
                "old_string": "**Backend Services:**",
                "new_string": "**Backend Services:**\n| FTS5 search | database.py, agents_store.py | Virtual table, BM25 ranking, snippet generation |",
            },
            timestamp=t6 + timedelta(minutes=6),
        ),
        EventIngest(
            role="tool",
            tool_name="Edit",
            tool_output_text="File updated successfully.",
            timestamp=t6 + timedelta(minutes=6, seconds=10),
        ),
        EventIngest(
            role="assistant",
            content_text="Documentation updated. Added FTS5 search entry to the Feature Index in AGENTS.md.",
            timestamp=t6 + timedelta(minutes=7),
        ),
    ]

    # Session 7: Claude — Deploy and verify (Bash-heavy ops session)
    t7 = anchor - timedelta(minutes=15)
    s7_events = [
        EventIngest(
            role="user",
            content_text="Push to main and verify the production deploy.",
            timestamp=t7 + timedelta(minutes=1),
        ),
        EventIngest(
            role="assistant",
            content_text="Running pre-push checks first.",
            timestamp=t7 + timedelta(minutes=2),
        ),
        EventIngest(
            role="assistant",
            content_text="Running unit tests...",
            tool_name="Bash",
            tool_input_json={"command": "make test"},
            timestamp=t7 + timedelta(minutes=3),
        ),
        EventIngest(
            role="tool",
            tool_name="Bash",
            tool_output_text="87 passed in 18.4s",
            timestamp=t7 + timedelta(minutes=5),
        ),
        EventIngest(
            role="assistant",
            content_text="Running E2E tests...",
            tool_name="Bash",
            tool_input_json={"command": "make test-e2e"},
            timestamp=t7 + timedelta(minutes=6),
        ),
        EventIngest(
            role="tool",
            tool_name="Bash",
            tool_output_text="12 passed in 45.2s",
            timestamp=t7 + timedelta(minutes=8),
        ),
        EventIngest(
            role="assistant",
            content_text="All green. Pushing and verifying deploy...",
            tool_name="Bash",
            tool_input_json={"command": "git push origin main && make verify-prod"},
            timestamp=t7 + timedelta(minutes=9),
        ),
        EventIngest(
            role="tool",
            tool_name="Bash",
            tool_output_text="Everything up-to-date\nHealth: OK\nAPI: OK\nBrowser: OK\nDeploy verified.",
            timestamp=t7 + timedelta(minutes=12),
        ),
        EventIngest(
            role="assistant",
            content_text="Deploy verified. All checks pass: unit tests (87), E2E (12), and production health/API/browser smoke tests.",
            timestamp=t7 + timedelta(minutes=13),
        ),
    ]

    return [
        SessionIngest(
            provider="claude",
            environment="development",
            project="longhouse",
            device_id="demo-mac",
            cwd="/Users/demo/longhouse",
            git_repo="https://github.com/cipher982/longhouse",
            git_branch="main",
            started_at=t1,
            ended_at=t1 + timedelta(minutes=20),
            provider_session_id="demo-claude-01",
            events=s1_events,
        ),
        SessionIngest(
            provider="codex",
            environment="development",
            project="longhouse",
            device_id="demo-mac",
            cwd="/Users/demo/longhouse",
            git_repo="https://github.com/cipher982/longhouse",
            git_branch="feat/cursor-pagination",
            started_at=t2,
            ended_at=t2 + timedelta(minutes=25),
            provider_session_id="demo-codex-01",
            events=s2_events,
        ),
        SessionIngest(
            provider="gemini",
            environment="development",
            project="longhouse",
            device_id="demo-mac",
            cwd="/Users/demo/longhouse",
            git_repo="https://github.com/cipher982/longhouse",
            git_branch="feat/fts5-search",
            started_at=t3,
            ended_at=t3 + timedelta(minutes=18),
            provider_session_id="demo-gemini-01",
            events=s3_events,
        ),
        SessionIngest(
            provider="claude",
            environment="development",
            project="longhouse",
            device_id="demo-mac",
            cwd="/Users/demo/longhouse",
            git_repo="https://github.com/cipher982/longhouse",
            git_branch="main",
            started_at=t4,
            ended_at=t4 + timedelta(minutes=15),
            provider_session_id="demo-claude-02",
            events=s4_events,
        ),
        SessionIngest(
            provider="codex",
            environment="development",
            project="longhouse",
            device_id="demo-mac",
            cwd="/Users/demo/longhouse",
            git_repo="https://github.com/cipher982/longhouse",
            git_branch="feature/onboarding",
            started_at=t5,
            ended_at=t5 + timedelta(minutes=12),
            provider_session_id="demo-codex-02",
            events=s5_events,
        ),
        SessionIngest(
            provider="gemini",
            environment="development",
            project="longhouse",
            device_id="demo-mac",
            cwd="/Users/demo/longhouse",
            git_repo="https://github.com/cipher982/longhouse",
            git_branch="feat/fts5-search",
            started_at=t6,
            ended_at=t6 + timedelta(minutes=14),
            provider_session_id="demo-gemini-02",
            events=s6_events,
        ),
        SessionIngest(
            provider="claude",
            environment="development",
            project="longhouse",
            device_id="demo-mac",
            cwd="/Users/demo/longhouse",
            git_repo="https://github.com/cipher982/longhouse",
            git_branch="main",
            started_at=t7,
            ended_at=t7 + timedelta(minutes=14),
            provider_session_id="demo-claude-03",
            events=s7_events,
        ),
    ]
