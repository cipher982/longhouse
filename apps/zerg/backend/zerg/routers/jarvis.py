"""Jarvis Integration API Router.

This module serves as the main orchestrator for Jarvis endpoints, importing
focused sub-routers and providing remaining endpoints (events, history, config, session).

Sub-routers:
- jarvis_auth: Authentication helpers
- jarvis_agents: Agent listing
- jarvis_runs: Run history
- jarvis_dispatch: Manual agent dispatch
- jarvis_supervisor: Supervisor dispatch, events, cancel
- jarvis_chat: Text chat with streaming

Endpoints in this file:
- /events: General SSE events stream
- /history: Conversation history (GET, DELETE)
- /bootstrap: Configuration and preferences (GET)
- /preferences: Update preferences (PATCH)
- /session: OpenAI Realtime session tokens (GET, POST)
- /conversation/title: Generate conversation titles (POST)
- /auth: Deprecated authentication endpoint (returns 410)
"""

import asyncio
import json
import logging
from datetime import datetime
from datetime import timezone
from typing import Dict
from typing import List
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from zerg.database import get_db
from zerg.events import EventType
from zerg.events.event_bus import event_bus
from zerg.models.models import ThreadMessage

# Import sub-routers
from zerg.routers import jarvis_agents
from zerg.routers import jarvis_chat
from zerg.routers import jarvis_dispatch
from zerg.routers import jarvis_runs
from zerg.routers import jarvis_supervisor
from zerg.routers.jarvis_auth import _is_tool_enabled
from zerg.routers.jarvis_auth import get_current_jarvis_user

logger = logging.getLogger(__name__)

# Main router
router = APIRouter(prefix="/api/jarvis", tags=["jarvis"])

# Include sub-routers (they all have /api/jarvis prefix already, so we strip it here)
router.include_router(jarvis_agents.router, prefix="", tags=["jarvis"])
router.include_router(jarvis_runs.router, prefix="", tags=["jarvis"])
router.include_router(jarvis_dispatch.router, prefix="", tags=["jarvis"])
router.include_router(jarvis_supervisor.router, prefix="", tags=["jarvis"])
router.include_router(jarvis_chat.router, prefix="", tags=["jarvis"])

# ---------------------------------------------------------------------------
# Deprecated Authentication Endpoint
# ---------------------------------------------------------------------------


class JarvisAuthRequest(BaseModel):
    """Jarvis authentication request with device secret."""

    device_secret: str = Field(..., description="Device secret for Jarvis authentication")


class JarvisAuthResponse(BaseModel):
    """Jarvis authentication response metadata."""

    session_expires_in: int = Field(..., description="Session expiry window in seconds")
    session_cookie_name: str = Field(..., description="Name of session cookie storing Jarvis session")


@router.post("/auth", response_model=JarvisAuthResponse)
def jarvis_auth(
    request: JarvisAuthRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> JarvisAuthResponse:
    """Deprecated: Jarvis now uses standard SaaS user authentication.

    Jarvis is treated as a normal client (like the dashboard). It authenticates
    using the same JWT bearer token as other frontend clients.
    """
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="Deprecated: Jarvis uses standard user login (JWT bearer token).",
    )


# ---------------------------------------------------------------------------
# General SSE Events Endpoint
# ---------------------------------------------------------------------------


async def _jarvis_event_generator(_current_user):
    """Generate SSE events for Jarvis.

    Subscribes to the event bus and yields agent/run update events.
    Runs until the client disconnects.
    """
    # Create asyncio queue for this connection
    queue = asyncio.Queue()

    # Subscribe to relevant events
    async def event_handler(event):
        """Handle event and put into queue."""
        await queue.put(event)

    # Subscribe to agent and run events
    event_bus.subscribe(EventType.AGENT_UPDATED, event_handler)
    event_bus.subscribe(EventType.RUN_CREATED, event_handler)
    event_bus.subscribe(EventType.RUN_UPDATED, event_handler)

    try:
        # Send initial connection event
        yield {
            "event": "connected",
            "data": json.dumps({"message": "Jarvis SSE stream connected"}),
        }

        # Stream events
        while True:
            try:
                # Wait for event with timeout to allow periodic heartbeats
                event = await asyncio.wait_for(queue.get(), timeout=30.0)

                # Format event for SSE
                event_type = event.get("event_type") or event.get("type") or "event"
                payload = {k: v for k, v in event.items() if k not in {"event_type", "type"}}
                event_data = {
                    "type": event_type,
                    "payload": payload,
                    "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                }

                yield {
                    "event": event_type,
                    "data": json.dumps(event_data),
                }

            except asyncio.TimeoutError:
                # Send heartbeat to keep connection alive
                heartbeat_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                yield {
                    "event": "heartbeat",
                    "data": json.dumps({"timestamp": heartbeat_ts}),
                }

    except asyncio.CancelledError:
        # Client disconnected
        logger.info("Jarvis SSE stream disconnected")
    finally:
        # Unsubscribe from events
        event_bus.unsubscribe(EventType.AGENT_UPDATED, event_handler)
        event_bus.unsubscribe(EventType.RUN_CREATED, event_handler)
        event_bus.unsubscribe(EventType.RUN_UPDATED, event_handler)


@router.get("/events")
async def jarvis_events(
    current_user=Depends(get_current_jarvis_user),
) -> EventSourceResponse:
    """Server-Sent Events stream for Jarvis.

    Provides real-time updates for agent and run events. Jarvis listens to this
    stream to update the Task Inbox UI without polling.

    Authentication:
    - Standard SaaS auth: `Authorization: Bearer <jwt>`
    - SSE fallback: `?token=<jwt>` query parameter (EventSource cannot send headers)
    - Development override: when `AUTH_DISABLED=1`, standard dev auth applies

    Event types:
    - connected: Initial connection confirmation
    - heartbeat: Keep-alive ping every 30 seconds
    - agent_updated: Agent status or configuration changed
    - run_created: New agent run started
    - run_updated: Agent run status changed (running → success/failed)

    Args:
        current_user: Authenticated user (Jarvis service account)

    Returns:
        EventSourceResponse streaming SSE events
    """
    return EventSourceResponse(_jarvis_event_generator(current_user))


# ---------------------------------------------------------------------------
# History Endpoint
# ---------------------------------------------------------------------------


class WorkerToolInfo(BaseModel):
    """Tool executed by a worker."""

    tool_name: str
    status: str  # completed, failed
    duration_ms: Optional[int] = None
    result_preview: Optional[str] = None
    error: Optional[str] = None


class WorkerInfo(BaseModel):
    """Worker spawned by spawn_worker tool."""

    job_id: int
    task: str
    status: str  # spawned, running, complete, failed
    summary: Optional[str] = None
    tools: List[WorkerToolInfo] = []


class ToolCallInfo(BaseModel):
    """Tool call made by supervisor."""

    tool_call_id: str
    tool_name: str
    args: Optional[dict] = None
    result: Optional[str] = None
    # For spawn_worker tools, includes worker activity
    worker: Optional[WorkerInfo] = None


class JarvisChatMessage(BaseModel):
    """Single chat message in history."""

    role: str = Field(..., description="Message role: user or assistant")
    content: str = Field(..., description="Message content")
    timestamp: datetime = Field(..., description="Message timestamp")
    usage: Optional[dict] = Field(None, description="Optional LLM usage metadata for this assistant response")
    tool_calls: Optional[List[ToolCallInfo]] = Field(None, description="Tool calls made by this assistant message")


class JarvisHistoryResponse(BaseModel):
    """Chat history response."""

    messages: List[JarvisChatMessage] = Field(..., description="List of messages")
    total: int = Field(..., description="Total message count")


def _fetch_worker_activity(db: Session, agent_id: int, tool_call_ids: list[str]) -> dict[str, dict]:
    """Fetch worker activity for spawn_worker tool calls.

    Queries AgentRunEvent to build a complete picture of worker execution:
    - worker_spawned: task, job_id
    - worker_started: confirms worker began
    - worker_tool_started/completed/failed: nested tool calls
    - worker_complete: final status
    - worker_summary_ready: LLM-generated summary

    Args:
        db: Database session
        agent_id: Supervisor agent ID (to filter runs)
        tool_call_ids: List of spawn_worker tool_call_ids to look up

    Returns:
        Dict mapping tool_call_id -> worker activity dict with:
        - job_id, task, status, summary, tools[]
    """
    from zerg.models import AgentRun
    from zerg.models import AgentRunEvent

    if not tool_call_ids:
        return {}

    # Get runs for this agent
    run_ids = [r.id for r in db.query(AgentRun.id).filter(AgentRun.agent_id == agent_id).all()]
    if not run_ids:
        return {}

    # Fetch all relevant events in one query
    events = (
        db.query(AgentRunEvent)
        .filter(
            AgentRunEvent.run_id.in_(run_ids),
            AgentRunEvent.event_type.in_(
                [
                    "supervisor_tool_started",
                    "worker_spawned",
                    "worker_started",
                    "worker_tool_started",
                    "worker_tool_completed",
                    "worker_tool_failed",
                    "worker_complete",
                    "worker_summary_ready",
                ]
            ),
        )
        .order_by(AgentRunEvent.id.asc())
        .all()
    )

    # Build job_id -> worker activity from worker events
    job_activity: dict[int, dict] = {}
    for e in events:
        payload = e.payload or {}
        job_id = payload.get("job_id")

        if e.event_type == "worker_spawned" and job_id:
            job_activity[job_id] = {
                "job_id": job_id,
                "task": payload.get("task", ""),
                "status": "spawned",
                "summary": None,
                "tools": [],
            }
        elif e.event_type == "worker_started" and job_id and job_id in job_activity:
            job_activity[job_id]["status"] = "running"
        elif e.event_type == "worker_tool_started" and job_id and job_id in job_activity:
            job_activity[job_id]["tools"].append(
                {
                    "tool_call_id": payload.get("tool_call_id"),
                    "tool_name": payload.get("tool_name", "unknown"),
                    "status": "running",
                }
            )
        elif e.event_type == "worker_tool_completed" and job_id and job_id in job_activity:
            tc_id = payload.get("tool_call_id")
            for t in job_activity[job_id]["tools"]:
                if t["tool_call_id"] == tc_id:
                    t["status"] = "completed"
                    t["duration_ms"] = payload.get("duration_ms")
                    t["result_preview"] = payload.get("result_preview")
                    break
        elif e.event_type == "worker_tool_failed" and job_id and job_id in job_activity:
            tc_id = payload.get("tool_call_id")
            for t in job_activity[job_id]["tools"]:
                if t["tool_call_id"] == tc_id:
                    t["status"] = "failed"
                    t["duration_ms"] = payload.get("duration_ms")
                    t["error"] = payload.get("error")
                    break
        elif e.event_type == "worker_complete" and job_id and job_id in job_activity:
            job_activity[job_id]["status"] = "complete" if payload.get("status") == "success" else "failed"
        elif e.event_type == "worker_summary_ready" and job_id and job_id in job_activity:
            job_activity[job_id]["summary"] = payload.get("summary")

    # Now map tool_call_id -> job_id by looking at supervisor_tool_started + worker_spawned correlation
    # The spawn_worker tool_call_id is in supervisor_tool_started, and the job_id is in the subsequent worker_spawned
    result: dict[str, dict] = {}

    # Group events by run_id for correlation
    events_by_run: dict[int, list] = {}
    for e in events:
        if e.run_id not in events_by_run:
            events_by_run[e.run_id] = []
        events_by_run[e.run_id].append(e)

    for run_id, run_events in events_by_run.items():
        # Find spawn_worker tool_call_id and corresponding job_id
        pending_tool_call_id = None
        for e in run_events:
            payload = e.payload or {}
            if e.event_type == "supervisor_tool_started" and payload.get("tool_name") == "spawn_worker":
                tc_id = payload.get("tool_call_id")
                if tc_id in tool_call_ids:
                    pending_tool_call_id = tc_id
            elif e.event_type == "worker_spawned" and pending_tool_call_id:
                job_id = payload.get("job_id")
                if job_id and job_id in job_activity:
                    result[pending_tool_call_id] = job_activity[job_id]
                pending_tool_call_id = None

    return result


@router.get("/history", response_model=JarvisHistoryResponse)
def jarvis_history(
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_jarvis_user),
) -> JarvisHistoryResponse:
    """Get conversation history from Supervisor thread.

    Returns paginated message history from the user's supervisor thread.
    Only includes user and assistant messages (filters out system messages).

    Args:
        limit: Maximum number of messages to return (default 50)
        offset: Number of messages to skip (default 0)
        db: Database session
        current_user: Authenticated user

    Returns:
        JarvisHistoryResponse with messages and total count
    """
    from zerg.services.supervisor_service import SupervisorService

    supervisor_service = SupervisorService(db)

    # Get supervisor thread (creates if doesn't exist)
    agent = supervisor_service.get_or_create_supervisor_agent(current_user.id)
    thread = supervisor_service.get_or_create_supervisor_thread(current_user.id, agent)

    # Query messages from thread (user and assistant only, excluding internal orchestration messages)
    # Internal messages (continuation prompts, system notifications) are stored for LLM context
    # but should NOT be shown to users in chat history.
    query = (
        db.query(ThreadMessage)
        .filter(
            ThreadMessage.thread_id == thread.id,
            ThreadMessage.role.in_(["user", "assistant"]),
            ThreadMessage.internal.is_(False),  # Exclude internal orchestration messages
        )
        .order_by(ThreadMessage.sent_at.asc())
    )

    # Get total count
    total = query.count()

    # Get paginated messages
    messages = query.offset(offset).limit(limit).all()

    # Collect all tool_call_ids that are spawn_worker to batch-fetch worker activity
    spawn_worker_tool_call_ids = []
    for msg in messages:
        if msg.role == "assistant" and msg.tool_calls:
            for tc in msg.tool_calls:
                if tc.get("name") == "spawn_worker" and tc.get("id"):
                    spawn_worker_tool_call_ids.append(tc["id"])

    # Batch fetch worker activity for all spawn_worker tool calls
    worker_activity_map: dict[str, dict] = {}
    if spawn_worker_tool_call_ids:
        worker_activity_map = _fetch_worker_activity(db, agent.id, spawn_worker_tool_call_ids)

    # Also need to find tool results from ToolMessages
    # Get all ToolMessages for this thread to map tool_call_id -> result
    tool_results_map: dict[str, str] = {}
    tool_messages = (
        db.query(ThreadMessage)
        .filter(
            ThreadMessage.thread_id == thread.id,
            ThreadMessage.role == "tool",
            ThreadMessage.tool_call_id.isnot(None),
        )
        .all()
    )
    for tm in tool_messages:
        if tm.tool_call_id:
            tool_results_map[tm.tool_call_id] = tm.content or ""

    # Convert to response format
    chat_messages = []
    for msg in messages:
        tool_calls_info = None
        if msg.role == "assistant" and msg.tool_calls:
            tool_calls_info = []
            for tc in msg.tool_calls:
                tc_id = tc.get("id", "")
                tc_name = tc.get("name", "unknown")
                tc_args = tc.get("args")
                tc_result = tool_results_map.get(tc_id)

                # For spawn_worker, include worker activity
                worker_info = None
                if tc_name == "spawn_worker" and tc_id in worker_activity_map:
                    wa = worker_activity_map[tc_id]
                    worker_info = WorkerInfo(
                        job_id=wa["job_id"],
                        task=wa["task"],
                        status=wa["status"],
                        summary=wa.get("summary"),
                        tools=[
                            WorkerToolInfo(
                                tool_name=t["tool_name"],
                                status=t["status"],
                                duration_ms=t.get("duration_ms"),
                                result_preview=t.get("result_preview"),
                                error=t.get("error"),
                            )
                            for t in wa.get("tools", [])
                        ],
                    )

                tool_calls_info.append(
                    ToolCallInfo(
                        tool_call_id=tc_id,
                        tool_name=tc_name,
                        args=tc_args,
                        result=tc_result,
                        worker=worker_info,
                    )
                )

        chat_messages.append(
            JarvisChatMessage(
                role=msg.role,
                content=msg.content or "",
                timestamp=msg.sent_at,
                usage=(msg.message_metadata or {}).get("usage") if msg.role == "assistant" else None,
                tool_calls=tool_calls_info,
            )
        )

    logger.debug(
        f"Jarvis history: returned {len(chat_messages)} messages "
        f"(offset={offset}, limit={limit}, total={total}) for user {current_user.id}",
        extra={"tag": "JARVIS"},
    )

    return JarvisHistoryResponse(
        messages=chat_messages,
        total=total,
    )


@router.delete("/history", status_code=status.HTTP_204_NO_CONTENT)
def jarvis_clear_history(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_jarvis_user),
) -> None:
    """Clear conversation history by deleting all messages from Supervisor thread.

    This clears all conversation messages from the user's Supervisor thread.
    The thread itself and the agent's system instructions are preserved.

    System prompts are injected fresh on every run from agent.system_instructions,
    so clearing history doesn't affect the agent's behavior.

    Args:
        db: Database session
        current_user: Authenticated user
    """
    from zerg.crud import crud
    from zerg.models.enums import ThreadType

    agents = crud.get_agents(db, owner_id=current_user.id)
    agent = next((a for a in agents if (a.config or {}).get("is_supervisor")), None)
    if agent is None:
        logger.info(f"Jarvis history cleared: no supervisor agent found for user {current_user.id} (noop)")
        return

    threads = crud.get_threads(db, agent_id=agent.id)
    old_thread = next((t for t in threads if t.thread_type == ThreadType.SUPER), None)
    if old_thread is None:
        logger.info(f"Jarvis history cleared: no supervisor thread found for user {current_user.id} (noop)")
        return

    # Delete all messages from the thread (keeps thread, clears history)
    deleted_count = (
        db.query(ThreadMessage)
        .filter(
            ThreadMessage.thread_id == old_thread.id,
        )
        .delete()
    )

    db.commit()

    logger.info(
        f"Jarvis history cleared: deleted {deleted_count} messages from thread {old_thread.id} " f"for user {current_user.id}",
        extra={"tag": "JARVIS"},
    )


# ---------------------------------------------------------------------------
# BFF Proxy Endpoints - Configuration and Preferences
# ---------------------------------------------------------------------------


class JarvisModelInfo(BaseModel):
    """Model information for frontend display."""

    id: str = Field(..., description="Model ID (e.g., gpt-5.2)")
    display_name: str = Field(..., description="Human-readable name")
    description: str = Field(..., description="Brief description")
    capabilities: Optional[Dict] = Field(default=None, description="Model capabilities (reasoning, etc.)")


class JarvisPreferences(BaseModel):
    """User preferences for Jarvis chat."""

    chat_model: str = Field(..., description="Selected model for text chat")
    reasoning_effort: str = Field(..., description="Reasoning effort: none, low, medium, high")


class JarvisBootstrapResponse(BaseModel):
    """Bootstrap response with prompt, tools, and user context."""

    prompt: str = Field(..., description="Complete Jarvis system prompt")
    enabled_tools: List[dict] = Field(..., description="List of available tools")
    user_context: dict = Field(..., description="User context summary (safe subset)")
    available_models: List[JarvisModelInfo] = Field(..., description="Models available for selection")
    preferences: JarvisPreferences = Field(..., description="User's saved preferences")


@router.get("/bootstrap", response_model=JarvisBootstrapResponse)
def jarvis_bootstrap(
    current_user=Depends(get_current_jarvis_user),
) -> JarvisBootstrapResponse:
    """Get Jarvis bootstrap configuration.

    Returns the complete system prompt (built from user context),
    list of enabled tools, and a safe subset of user context for display.

    This is the single source of truth for Jarvis configuration.
    """
    from zerg.models_config import get_all_models
    from zerg.models_config import get_default_model_id_str
    from zerg.prompts.composer import build_jarvis_prompt

    # Define all available personal tools (Phase 4 v2.1)
    # These are now Supervisor-owned tools, NOT Realtime tools.
    # Realtime has zero tools (I/O only: transcription + VAD).
    # All user input goes to Supervisor via POST /api/jarvis/chat.
    # Supervisor can call these tools directly using connector credentials.
    AVAILABLE_TOOLS = {
        "location": {"name": "get_current_location", "description": "Get GPS location via Traccar"},
        "whoop": {"name": "get_whoop_data", "description": "Get WHOOP health metrics"},
        "obsidian": {"name": "search_notes", "description": "Search Obsidian vault via Runner"},
    }

    # Get tool configuration from user context (default all enabled)
    ctx = current_user.context or {}

    # Build enabled tools list based on user configuration
    enabled_tools = []
    for tool_key, tool_def in AVAILABLE_TOOLS.items():
        # Default to enabled if not explicitly configured
        if _is_tool_enabled(ctx, tool_key):
            enabled_tools.append(tool_def)

    prompt = build_jarvis_prompt(current_user, enabled_tools)

    user_context = {
        "display_name": ctx.get("display_name"),
        "role": ctx.get("role"),
        "location": ctx.get("location"),
        "servers": [{"name": s.get("name"), "purpose": s.get("purpose")} for s in ctx.get("servers", [])],
    }

    # Get available models (exclude test models)
    from zerg.testing.test_models import is_test_model

    all_models = get_all_models()
    available_models = [
        JarvisModelInfo(
            id=m.id,
            display_name=m.display_name,
            description=m.description or "",
            capabilities=m.capabilities,
        )
        for m in all_models
        if not is_test_model(m.id)
    ]

    available_model_ids = {m.id for m in available_models}
    default_model_id = get_default_model_id_str()

    # Get user preferences (with defaults + validation)
    prefs = ctx.get("preferences", {}) or {}
    requested_model = prefs.get("chat_model") or default_model_id
    if requested_model not in available_model_ids:
        requested_model = (
            default_model_id
            if default_model_id in available_model_ids
            else (available_models[0].id if available_models else default_model_id)
        )

    requested_effort = (prefs.get("reasoning_effort") or "none").lower()
    if requested_effort not in {"none", "low", "medium", "high"}:
        requested_effort = "none"

    preferences = JarvisPreferences(chat_model=requested_model, reasoning_effort=requested_effort)

    return JarvisBootstrapResponse(
        prompt=prompt,
        enabled_tools=enabled_tools,
        user_context=user_context,
        available_models=available_models,
        preferences=preferences,
    )


class SupervisorThreadInfo(BaseModel):
    """Supervisor thread information."""

    thread_id: int = Field(..., description="Thread ID")
    title: str = Field(..., description="Thread title")
    message_count: int = Field(..., description="Number of messages in thread")


@router.get("/supervisor/thread", response_model=SupervisorThreadInfo)
def get_supervisor_thread(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_jarvis_user),
) -> SupervisorThreadInfo:
    """Get the supervisor thread for the current user.

    Returns basic information about the user's supervisor thread.
    """
    from zerg.services.supervisor_service import SupervisorService

    service = SupervisorService(db)
    agent = service.get_or_create_supervisor_agent(current_user.id)
    thread = service.get_or_create_supervisor_thread(current_user.id, agent)

    # Count messages using proper count query (not limited to 100)
    # Exclude internal orchestration messages from the count shown to users
    from sqlalchemy import func

    from zerg.models.thread import ThreadMessage

    message_count = (
        db.query(func.count(ThreadMessage.id))
        .filter(
            ThreadMessage.thread_id == thread.id,
            ThreadMessage.role.in_(["user", "assistant"]),  # Only count user/assistant messages
            ThreadMessage.internal.is_(False),  # Exclude internal orchestration messages
        )
        .scalar()
        or 0
    )

    return SupervisorThreadInfo(
        thread_id=thread.id,
        title=thread.title or "Supervisor",
        message_count=message_count,
    )


class JarvisPreferencesUpdate(BaseModel):
    """Request to update user preferences."""

    chat_model: Optional[str] = Field(None, description="Model for text chat")
    reasoning_effort: Optional[str] = Field(None, description="Reasoning effort: none, low, medium, high")


@router.patch("/preferences", response_model=JarvisPreferences)
def jarvis_update_preferences(
    update: JarvisPreferencesUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_jarvis_user),
) -> JarvisPreferences:
    """Update user's Jarvis preferences.

    Saves preferences to the user's context in the database.
    Only provided fields are updated; others remain unchanged.
    """
    from zerg.models_config import get_default_model_id_str
    from zerg.models_config import get_model_by_id

    ctx = current_user.context or {}
    prefs = ctx.get("preferences", {}) or {}

    # Validate and update chat_model
    from zerg.testing.test_models import is_test_model

    if update.chat_model is not None:
        model = get_model_by_id(update.chat_model)
        if not model or is_test_model(update.chat_model):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid model: {update.chat_model}",
            )
        prefs["chat_model"] = update.chat_model

    # Validate and update reasoning_effort
    if update.reasoning_effort is not None:
        valid_efforts = {"none", "low", "medium", "high"}
        if update.reasoning_effort.lower() not in valid_efforts:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid reasoning_effort: {update.reasoning_effort}. Must be one of: {valid_efforts}",
            )
        prefs["reasoning_effort"] = update.reasoning_effort.lower()

    # Save to user context
    ctx["preferences"] = prefs
    current_user.context = ctx
    db.commit()

    # Return preferences with sensible defaults
    chat_model = prefs.get("chat_model") or get_default_model_id_str()
    reasoning = (prefs.get("reasoning_effort") or "none").lower()
    if reasoning not in {"none", "low", "medium", "high"}:
        reasoning = "none"

    return JarvisPreferences(
        chat_model=chat_model,
        reasoning_effort=reasoning,
    )


# ---------------------------------------------------------------------------
# OpenAI Realtime Session Endpoints
# ---------------------------------------------------------------------------


@router.get("/session")
async def jarvis_session_get(
    request: Request,
    current_user=Depends(get_current_jarvis_user),
):
    """Mint an ephemeral OpenAI Realtime session token.

    Directly calls OpenAI's API - no separate jarvis-server needed.
    """
    import httpx

    from zerg.services.openai_realtime import mint_realtime_session_token

    try:
        result = await mint_realtime_session_token()
        return result
    except httpx.TimeoutException:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="OpenAI API timeout")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=f"OpenAI API error: {e.response.text}")


@router.post("/session")
async def jarvis_session_post(
    request: Request,
    current_user=Depends(get_current_jarvis_user),
):
    """Backwards compatibility: some clients may still POST."""
    import httpx

    from zerg.services.openai_realtime import mint_realtime_session_token

    logger.debug("Jarvis session: received POST /session; handling directly")
    try:
        result = await mint_realtime_session_token()
        return result
    except httpx.TimeoutException:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="OpenAI API timeout")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=f"OpenAI API error: {e.response.text}")


@router.post("/conversation/title")
async def jarvis_conversation_title(
    request: Request,
    current_user=Depends(get_current_jarvis_user),
):
    """Generate a conversation title using OpenAI.

    Directly calls OpenAI's API - no separate jarvis-server needed.
    """
    import httpx

    from zerg.services.title_generator import generate_conversation_title

    try:
        body = await request.json()
        messages = body.get("messages", [])

        title = await generate_conversation_title(messages)
        if title:
            return {"title": title}
        else:
            return {"title": None, "error": "Could not generate title"}
    except httpx.TimeoutException:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="OpenAI API timeout")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=f"OpenAI API error: {e.response.text}")
