"""Helpers for seeding agent sessions in tests."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from uuid import UUID
from uuid import uuid4

from zerg.services.agents_store import AgentsStore
from zerg.services.agents_store import EventIngest
from zerg.services.agents_store import SessionIngest

DEFAULT_TIMESTAMP = datetime(2026, 2, 5, tzinfo=timezone.utc)


def seed_agent_session(
    db_session,
    *,
    session_id: UUID | None = None,
    provider: str = "claude",
    environment: str = "test",
    project: str = "session-tools",
    device_id: str = "dev-machine",
    cwd: str = "/tmp",
    git_repo: str | None = None,
    git_branch: str | None = None,
    user_text: str = "alpha beta",
    assistant_text: str = "gamma delta",
    tool_output_text: str = "grep needle output",
    include_assistant: bool = True,
    include_tool: bool = True,
    timestamp: datetime | None = None,
    source_path: str = "/tmp/session.jsonl",
) -> str:
    """Seed a single agent session with a minimal event trail."""

    session_uuid = session_id or uuid4()
    event_ts = timestamp or DEFAULT_TIMESTAMP

    events: list[EventIngest] = []
    offset = 0
    events.append(
        EventIngest(
            role="user",
            content_text=user_text,
            timestamp=event_ts,
            source_path=source_path,
            source_offset=offset,
        )
    )
    offset += 1

    if include_assistant:
        events.append(
            EventIngest(
                role="assistant",
                content_text=assistant_text,
                timestamp=event_ts,
                source_path=source_path,
                source_offset=offset,
            )
        )
        offset += 1

    if include_tool:
        events.append(
            EventIngest(
                role="tool",
                tool_name="Bash",
                tool_output_text=tool_output_text,
                timestamp=event_ts,
                source_path=source_path,
                source_offset=offset,
            )
        )

    store = AgentsStore(db_session)
    store.ingest_session(
        SessionIngest(
            id=session_uuid,
            provider=provider,
            environment=environment,
            project=project,
            device_id=device_id,
            cwd=cwd,
            git_repo=git_repo,
            git_branch=git_branch,
            started_at=event_ts,
            events=events,
        )
    )
    return str(session_uuid)
