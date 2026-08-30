"""Idle Console session identity creation, separate from provider launch."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from uuid import UUID
from uuid import uuid4

from sqlalchemy.orm import Session

from zerg.services.catalogd_supervisor import get_catalogd_client

CONSOLE_CREATE_CATALOG_TIMEOUT_SECONDS = 5.0


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
    parent_thread_id: UUID | None = None,
    parent_session_id: UUID | None = None,
    branch_kind: str = "root",
) -> CreatedConsoleSession:
    """Persist an empty thread and its execution target without starting a run."""

    provider = str(provider or "").strip().lower()
    device_id = str(device_id or "").strip()
    cwd = str(cwd or "").strip()
    launch_surface = str(launch_surface or "").strip().lower() or "console"
    launch_actor = "automation" if launch_surface == "test" else "user"
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
        "launch_actor": launch_actor,
        "launch_surface": launch_surface,
        "started_at": now.isoformat(),
        "parent_thread_id": str(parent_thread_id) if parent_thread_id else None,
        "parent_session_id": str(parent_session_id) if parent_session_id else None,
        "branch_kind": branch_kind,
    }
    client = get_catalogd_client()
    if client is None:
        raise RuntimeError("Console session catalog is unavailable")
    # Console creation is a human-facing, idempotent write. Under a burst,
    # the hosted catalog has measured a successful commit at 2.4 seconds;
    # the generic one-second RPC budget reports that committed outcome as a
    # 500 and forces the caller to discover it by replaying the request.
    result = await client.call(
        "session.console.create.v2",
        {"session": data},
        timeout_seconds=CONSOLE_CREATE_CATALOG_TIMEOUT_SECONDS,
    )
    if result.get("idempotency_conflict") is True:
        raise ValueError("Console session identity was reused with different attributes")
    return CreatedConsoleSession(
        session_id=session_id,
        thread_id=thread_id,
        created=bool(result.get("created")),
    )
