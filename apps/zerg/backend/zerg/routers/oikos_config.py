"""Oikos configuration endpoints: bootstrap, preferences, thread info, session token."""

import logging
from typing import Dict
from typing import List
from typing import Optional

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from zerg.database import get_db
from zerg.routers.oikos_auth import _is_tool_enabled
from zerg.routers.oikos_auth import get_current_oikos_user
from zerg.services.oikos_operator_policy import policy_from_user_context

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["oikos"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class OikosModelInfo(BaseModel):
    """Model information for frontend display."""

    id: str
    display_name: str
    description: str
    capabilities: Optional[Dict] = None


class OikosPreferences(BaseModel):
    """User preferences for Oikos chat."""

    class OperatorMode(BaseModel):
        enabled: bool
        shadow_mode: bool
        allow_continue: bool
        allow_notify: bool
        allow_small_repairs: bool

    chat_model: str
    reasoning_effort: str
    operator_mode: OperatorMode


class OikosBootstrapResponse(BaseModel):
    """Bootstrap response with prompt, tools, and user context."""

    prompt: str
    enabled_tools: List[dict]
    user_context: dict
    available_models: List[OikosModelInfo]
    preferences: OikosPreferences


class OikosThreadInfo(BaseModel):
    """Oikos thread information."""

    class CanonicalConversation(BaseModel):
        id: int
        kind: str
        title: str | None = None
        external_conversation_id: str
        message_count: int

    thread_id: int
    title: str
    message_count: int
    canonical_conversation: CanonicalConversation


class OikosPreferencesUpdate(BaseModel):
    """Request to update user preferences."""

    class OperatorModeUpdate(BaseModel):
        enabled: Optional[bool] = None
        shadow_mode: Optional[bool] = None
        allow_continue: Optional[bool] = None
        allow_notify: Optional[bool] = None
        allow_small_repairs: Optional[bool] = None

    chat_model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    operator_mode: Optional[OperatorModeUpdate] = None


# ---------------------------------------------------------------------------
# Available tools (used by bootstrap to build the prompt)
# ---------------------------------------------------------------------------

AVAILABLE_TOOLS = {
    "location": {"name": "get_current_location", "description": "Get GPS location via Traccar"},
    "whoop": {"name": "get_whoop_data", "description": "Get WHOOP health metrics"},
    "obsidian": {"name": "search_notes", "description": "Search Obsidian vault via Runner"},
}


def _build_operator_mode_preferences(context: dict | None) -> OikosPreferences.OperatorMode:
    policy = policy_from_user_context(context)
    return OikosPreferences.OperatorMode(
        enabled=policy.enabled,
        shadow_mode=policy.shadow_mode,
        allow_continue=policy.allow_continue,
        allow_notify=policy.allow_notify,
        allow_small_repairs=policy.allow_small_repairs,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/bootstrap", response_model=OikosBootstrapResponse)
def oikos_bootstrap(
    current_user=Depends(get_current_oikos_user),
) -> OikosBootstrapResponse:
    """Get Oikos bootstrap configuration.

    Returns system prompt, enabled tools, user context, available models,
    and saved preferences. This is the single source of truth for Oikos
    frontend initialization.
    """
    from zerg.models_config import get_all_models
    from zerg.models_config import get_default_model_id_str
    from zerg.prompts.composer import build_oikos_prompt
    from zerg.testing.test_models import is_test_model

    ctx = current_user.context or {}

    enabled_tools = [tool for key, tool in AVAILABLE_TOOLS.items() if _is_tool_enabled(ctx, key)]
    prompt = build_oikos_prompt(current_user, enabled_tools)

    user_context = {
        "display_name": ctx.get("display_name"),
        "role": ctx.get("role"),
        "location": ctx.get("location"),
        "servers": [{"name": s.get("name"), "purpose": s.get("purpose")} for s in ctx.get("servers", [])],
    }

    all_models = get_all_models()
    available_models = [
        OikosModelInfo(
            id=m.id,
            display_name=m.display_name,
            description=m.description or "",
            capabilities=m.capabilities,
        )
        for m in all_models
        if not is_test_model(m.id)
    ]

    available_ids = {m.id for m in available_models}
    default_id = get_default_model_id_str()

    prefs = ctx.get("preferences", {}) or {}
    model = prefs.get("chat_model") or default_id
    if model not in available_ids:
        if default_id in available_ids:
            model = default_id
        elif available_models:
            model = available_models[0].id
        else:
            model = default_id

    effort = (prefs.get("reasoning_effort") or "none").lower()
    if effort not in {"none", "low", "medium", "high"}:
        effort = "none"

    return OikosBootstrapResponse(
        prompt=prompt,
        enabled_tools=enabled_tools,
        user_context=user_context,
        available_models=available_models,
        preferences=OikosPreferences(
            chat_model=model,
            reasoning_effort=effort,
            operator_mode=_build_operator_mode_preferences(ctx),
        ),
    )


@router.get("/thread", response_model=OikosThreadInfo)
def get_oikos_thread(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_oikos_user),
) -> OikosThreadInfo:
    """Get the oikos thread for the current user."""
    from sqlalchemy import func

    from zerg.models.thread import ThreadMessage
    from zerg.services.conversation_service import ConversationService
    from zerg.services.oikos_service import OikosService

    service = OikosService(db)
    fiche = service.get_or_create_oikos_fiche(current_user.id)
    thread = service.get_or_create_oikos_thread(current_user.id, fiche)
    conversation = service.get_or_create_surface_conversation(
        owner_id=current_user.id,
        surface_id="web",
        external_conversation_id="web:main",
        backing_thread_id=thread.id,
        title=thread.title or "Oikos",
    )

    message_count = (
        db.query(func.count(ThreadMessage.id))
        .filter(
            ThreadMessage.thread_id == thread.id,
            ThreadMessage.role.in_(["user", "assistant"]),
            ThreadMessage.internal.is_(False),
        )
        .scalar()
        or 0
    )

    return OikosThreadInfo(
        thread_id=thread.id,
        title=thread.title or "Oikos",
        message_count=message_count,
        canonical_conversation=OikosThreadInfo.CanonicalConversation(
            id=conversation.id,
            kind=conversation.kind,
            title=conversation.title,
            external_conversation_id="web:main",
            message_count=ConversationService.count_messages(
                db,
                owner_id=current_user.id,
                conversation_id=conversation.id,
            ),
        ),
    )


@router.patch("/preferences", response_model=OikosPreferences)
def oikos_update_preferences(
    update: OikosPreferencesUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_oikos_user),
) -> OikosPreferences:
    """Update user's Oikos preferences (model, reasoning effort)."""
    from zerg.models_config import get_default_model_id_str
    from zerg.models_config import get_model_by_id
    from zerg.testing.test_models import is_test_model

    ctx = current_user.context or {}
    prefs = ctx.get("preferences", {}) or {}

    if update.chat_model is not None:
        model = get_model_by_id(update.chat_model)
        if not model or is_test_model(update.chat_model):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid model: {update.chat_model}")
        prefs["chat_model"] = update.chat_model

    if update.reasoning_effort is not None:
        valid = {"none", "low", "medium", "high"}
        if update.reasoning_effort.lower() not in valid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid reasoning_effort: {update.reasoning_effort}. Must be one of: {valid}",
            )
        prefs["reasoning_effort"] = update.reasoning_effort.lower()

    if update.operator_mode is not None:
        operator_mode = dict(prefs.get("operator_mode", {}) or {})
        for field_name in ("enabled", "shadow_mode", "allow_continue", "allow_notify", "allow_small_repairs"):
            value = getattr(update.operator_mode, field_name)
            if value is not None:
                operator_mode[field_name] = value
        prefs["operator_mode"] = operator_mode

    ctx["preferences"] = prefs
    current_user.context = ctx
    db.commit()

    chat_model = prefs.get("chat_model") or get_default_model_id_str()
    effort = (prefs.get("reasoning_effort") or "none").lower()
    if effort not in {"none", "low", "medium", "high"}:
        effort = "none"

    return OikosPreferences(
        chat_model=chat_model,
        reasoning_effort=effort,
        operator_mode=_build_operator_mode_preferences(ctx),
    )


@router.get("/session")
async def oikos_session(
    current_user=Depends(get_current_oikos_user),
):
    """Mint an ephemeral OpenAI Realtime session token."""
    import httpx

    from zerg.voice.realtime import mint_realtime_session_token

    try:
        return await mint_realtime_session_token()
    except httpx.TimeoutException:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="OpenAI API timeout")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=f"OpenAI API error: {e.response.text}")
