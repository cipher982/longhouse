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


class JarvisChatMessage(BaseModel):
    """Single chat message in history."""

    role: str = Field(..., description="Message role: user or assistant")
    content: str = Field(..., description="Message content")
    timestamp: datetime = Field(..., description="Message timestamp")
    usage: Optional[dict] = Field(None, description="Optional LLM usage metadata for this assistant response")


class JarvisHistoryResponse(BaseModel):
    """Chat history response."""

    messages: List[JarvisChatMessage] = Field(..., description="List of messages")
    total: int = Field(..., description="Total message count")


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

    # Query messages from thread (user and assistant only)
    query = (
        db.query(ThreadMessage)
        .filter(
            ThreadMessage.thread_id == thread.id,
            ThreadMessage.role.in_(["user", "assistant"]),
        )
        .order_by(ThreadMessage.sent_at.asc())
    )

    # Get total count
    total = query.count()

    # Get paginated messages
    messages = query.offset(offset).limit(limit).all()

    # Convert to response format
    chat_messages = [
        JarvisChatMessage(
            role=msg.role,
            content=msg.content,
            timestamp=msg.sent_at,
            usage=(msg.message_metadata or {}).get("usage") if msg.role == "assistant" else None,
        )
        for msg in messages
    ]

    logger.info(
        f"Jarvis history: returned {len(chat_messages)} messages "
        f"(offset={offset}, limit={limit}, total={total}) for user {current_user.id}"
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
    from zerg.services.supervisor_service import SupervisorService

    supervisor_service = SupervisorService(db)

    # Get supervisor agent and current thread
    agent = supervisor_service.get_or_create_supervisor_agent(current_user.id)
    old_thread = supervisor_service.get_or_create_supervisor_thread(current_user.id, agent)

    # Delete all messages from the thread (keeps thread, clears history)
    deleted_count = (
        db.query(ThreadMessage)
        .filter(
            ThreadMessage.thread_id == old_thread.id,
        )
        .delete()
    )

    db.commit()

    logger.info(f"Jarvis history cleared: deleted {deleted_count} messages from thread {old_thread.id} for user {current_user.id}")


# ---------------------------------------------------------------------------
# BFF Proxy Endpoints - Configuration and Preferences
# ---------------------------------------------------------------------------


class JarvisModelInfo(BaseModel):
    """Model information for frontend display."""

    id: str = Field(..., description="Model ID (e.g., gpt-5.2)")
    display_name: str = Field(..., description="Human-readable name")
    description: str = Field(..., description="Brief description")


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

    # Get available models (exclude mock model)
    all_models = get_all_models()
    available_models = [
        JarvisModelInfo(
            id=m.id,
            display_name=m.display_name,
            description=m.description or "",
        )
        for m in all_models
        if m.id != "gpt-mock"
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
    if update.chat_model is not None:
        model = get_model_by_id(update.chat_model)
        if not model or update.chat_model == "gpt-mock":
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
