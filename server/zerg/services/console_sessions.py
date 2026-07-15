"""Idle Console session identity creation, separate from provider launch."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from uuid import UUID
from uuid import uuid4

from sqlalchemy.orm import Session

import zerg.database as database_module
from zerg.models.agents import AgentSession
from zerg.services.agents.kernel_writes import ensure_primary_thread
from zerg.services.agents.kernel_writes import set_thread_execution_target
from zerg.services.catalogd_supervisor import get_catalogd_client


@dataclass(frozen=True)
class CreatedConsoleSession:
    session_id: UUID
    thread_id: UUID
    created: bool


async def create_empty_console_session(
    db: Session | None,
    *,
    owner_id: int,
    provider: str,
    device_id: str,
    cwd: str,
    project: str | None = None,
    display_name: str | None = None,
    provider_config: dict[str, object] | None = None,
    launch_surface: str = "console",
    session_id: UUID | None = None,
    thread_id: UUID | None = None,
) -> CreatedConsoleSession:
    """Persist an empty thread and its execution target without starting a run."""

    provider = str(provider or "").strip().lower()
    device_id = str(device_id or "").strip()
    cwd = str(cwd or "").strip()
    if not provider or not device_id or not cwd.startswith("/"):
        raise ValueError("provider, device_id, and absolute cwd are required")
    session_id = session_id or uuid4()
    thread_id = thread_id or uuid4()
    now = datetime.now(timezone.utc)
    data = {
        "session_id": str(session_id),
        "thread_id": str(thread_id),
        "owner_id": owner_id,
        "provider": provider,
        "device_id": device_id,
        "cwd": cwd,
        "project": str(project or "").strip() or cwd.rstrip("/").rsplit("/", 1)[-1] or "console",
        "display_name": str(display_name or "").strip() or None,
        "provider_config": dict(provider_config or {"permission_mode": "bypass"}),
        "launch_surface": launch_surface,
        "started_at": now.isoformat(),
    }
    if database_module.live_catalog_enabled():
        client = get_catalogd_client()
        if client is None:
            raise RuntimeError("Console session catalog is unavailable")
        result = await client.call("session.console.create.v2", {"session": data})
        if result.get("idempotency_conflict") is True:
            raise ValueError("Console session identity was reused with different attributes")
        return CreatedConsoleSession(
            session_id=session_id,
            thread_id=thread_id,
            created=bool(result.get("created")),
        )

    if db is None:
        raise RuntimeError("Console session database is unavailable")
    existing = db.get(AgentSession, session_id)
    if existing is not None:
        thread = ensure_primary_thread(db, existing)
        exact = (
            thread.id == thread_id
            and existing.provider == provider
            and str(thread.device_id or "") == device_id
            and str(thread.cwd or "") == cwd
        )
        if not exact:
            raise ValueError("Console session identity was reused with different attributes")
        return CreatedConsoleSession(session_id=session_id, thread_id=thread_id, created=False)
    session = AgentSession(
        id=session_id,
        provider=provider,
        environment="development",
        project=data["project"],
        device_id=device_id,
        device_name=device_id,
        cwd=cwd,
        started_at=now,
        ended_at=None,
        user_messages=0,
        assistant_messages=0,
        tool_calls=0,
        loop_mode="assist",
        launch_actor="user",
        launch_surface=launch_surface,
        origin_kind="console",
    )
    db.add(session)
    db.flush()
    thread = ensure_primary_thread(db, session)
    thread.id = thread_id
    session.primary_thread_id = thread_id
    set_thread_execution_target(
        thread,
        device_id=device_id,
        cwd=cwd,
        provider_config=data["provider_config"],
    )
    db.commit()
    return CreatedConsoleSession(session_id=session_id, thread_id=thread_id, created=True)
