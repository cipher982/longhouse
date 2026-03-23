"""AI-first loop controller for per-session turn-end decisions.

Each coding session gets its own loop-controller thread. The controller is a
small, isolated LLM judge that reviews completed assistant turns and returns a
structured next-step decision for that same session only.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from zerg.crud import create_fiche
from zerg.crud import create_thread_message
from zerg.crud import get_fiches
from zerg.crud import get_recent_thread_messages
from zerg.models import Thread
from zerg.models.agents import AgentSession
from zerg.models_config import get_llm_client_with_db_fallback
from zerg.models_config import get_model_for_use_case
from zerg.services.session_processing import safe_parse_json
from zerg.services.thread_service import ThreadService

logger = logging.getLogger(__name__)

_LOOP_CONTROLLER_NAME = "Loop Controller"
_LOOP_CONTROLLER_USE_CASE = "loop_controller"
_LOOP_THREAD_HISTORY_LIMIT = 6
_TRANSCRIPT_TAIL_LIMIT = 10
_MESSAGE_CONTENT_LIMIT = 3000

_LOOP_CONTROLLER_SYSTEM_PROMPT = """You are the loop controller for exactly one AI coding session.

Your job is to decide what should happen immediately after a completed
assistant turn. You are not the coding agent. You do not write code. You only
judge the next step for this same session.

Core principle: prefer AI-first continuation when the next step is obvious,
bounded, and part of the same task. Do not cheap out into passivity just
because the turn ended.

You must return exactly one JSON object with this shape:
{
  "decision": "continue|ask_user|wait|done|escalate",
  "summary": "short user-facing summary",
  "rationale": "why this decision is correct",
  "recommended_action": "continue_session|ask_user|wait|done|escalate",
  "follow_up_prompt": "exact bounded same-session prompt or null",
  "blocked_reasons": ["optional", "short", "reasons"]
}

Decision meanings:
- continue: immediately continue the same session with one obvious bounded next step
- ask_user: likely continue-able, but the user should explicitly approve/check in
- wait: do nothing yet; an external dependency or passive wait is appropriate
- done: the turn appears complete; no meaningful follow-up is needed now
- escalate: a human decision or risky/ambiguous situation requires direct attention

Hard rules:
- Never switch to a different session.
- Never invent unrelated work.
- Treat "continue" as same-session continuation only.
- If the next step is risky, ambiguous, destructive, or product-defining, use escalate.
- If the assistant is clearly asking for the routine "ok, continue" on the same task, prefer continue.
- Use the transcript tail, session summary, and recent loop decisions together.
- If you choose continue, include a concrete follow_up_prompt that can be sent
  directly back into the same coding session. Keep it narrow and action-oriented.
- If you do not choose continue, set follow_up_prompt to null.

Return JSON only. No markdown, no prose outside the JSON object."""


@dataclass(frozen=True)
class LoopControllerDecision:
    decision: str
    summary: str
    rationale: str
    recommended_action: str | None = None
    follow_up_prompt: str | None = None
    blocked_reasons: tuple[str, ...] = ()
    model_id: str | None = None
    raw_response: str | None = None
    loop_thread_id: int | None = None


def _truncate_text(value: str | None, *, limit: int = _MESSAGE_CONTENT_LIMIT) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _parse_decision(raw: str, *, model_id: str, loop_thread_id: int) -> LoopControllerDecision:
    parsed = safe_parse_json(raw)
    if not isinstance(parsed, dict):
        raise ValueError("loop controller returned invalid JSON")

    decision = str(parsed.get("decision") or "").strip().lower()
    if decision not in {"continue", "ask_user", "wait", "done", "escalate"}:
        raise ValueError(f"invalid loop decision '{decision}'")

    summary = str(parsed.get("summary") or "").strip()
    rationale = str(parsed.get("rationale") or "").strip()
    recommended_action = str(parsed.get("recommended_action") or "").strip() or None
    follow_up_prompt = str(parsed.get("follow_up_prompt") or "").strip() or None
    blocked_reasons_raw = parsed.get("blocked_reasons")
    blocked_reasons = (
        tuple(str(item).strip() for item in blocked_reasons_raw if str(item).strip()) if isinstance(blocked_reasons_raw, list) else ()
    )

    if not summary:
        raise ValueError("loop controller response missing summary")
    if not rationale:
        raise ValueError("loop controller response missing rationale")
    if decision == "continue" and not follow_up_prompt:
        raise ValueError("loop controller continue response missing follow_up_prompt")

    return LoopControllerDecision(
        decision=decision,
        summary=summary,
        rationale=rationale,
        recommended_action=recommended_action,
        follow_up_prompt=follow_up_prompt,
        blocked_reasons=blocked_reasons,
        model_id=model_id,
        raw_response=raw,
        loop_thread_id=loop_thread_id,
    )


def get_or_create_loop_controller_fiche(db: Session, owner_id: int):
    """Return the per-user loop-controller fiche."""
    fiches = get_fiches(db, owner_id=owner_id)
    for fiche in fiches:
        config = fiche.config or {}
        if config.get("is_loop_controller"):
            desired_model = get_model_for_use_case(_LOOP_CONTROLLER_USE_CASE)
            changed = False
            if fiche.model != desired_model:
                fiche.model = desired_model
                changed = True
            if fiche.system_instructions != _LOOP_CONTROLLER_SYSTEM_PROMPT:
                fiche.system_instructions = _LOOP_CONTROLLER_SYSTEM_PROMPT
                changed = True
            if fiche.allowed_tools != []:
                fiche.allowed_tools = []
                changed = True
            if changed:
                db.commit()
                db.refresh(fiche)
            return fiche

    fiche = create_fiche(
        db=db,
        owner_id=owner_id,
        name=_LOOP_CONTROLLER_NAME,
        model=get_model_for_use_case(_LOOP_CONTROLLER_USE_CASE),
        system_instructions=_LOOP_CONTROLLER_SYSTEM_PROMPT,
        task_instructions="Judge the next step for a single coding session after each completed assistant turn.",
        config={"is_loop_controller": True, "temperature": 0.2, "reasoning_effort": "none"},
    )
    fiche.allowed_tools = []
    db.commit()
    db.refresh(fiche)
    return fiche


def get_or_create_session_loop_thread(db: Session, *, owner_id: int, session: AgentSession) -> Thread:
    """Return the dedicated loop-controller thread for one coding session."""
    existing_thread_id = getattr(session, "loop_thread_id", None)
    if existing_thread_id:
        thread = db.query(Thread).filter(Thread.id == existing_thread_id).first()
        if thread is not None:
            return thread

    fiche = get_or_create_loop_controller_fiche(db, owner_id)
    thread = ThreadService.create_thread_with_system_message(
        db,
        fiche,
        title=f"Loop Control: {session.summary_title or session.project or session.id}",
        thread_type="manual",
        active=False,
    )
    thread.fiche_state = {"session_id": str(session.id)}
    session.loop_thread_id = thread.id
    db.commit()
    db.refresh(thread)
    db.refresh(session)
    return thread


def _build_controller_messages(
    *,
    loop_thread_id: int,
    payload: dict[str, Any],
    db: Session,
) -> list[dict[str, str]]:
    prior_messages = get_recent_thread_messages(
        db,
        loop_thread_id,
        limit=_LOOP_THREAD_HISTORY_LIMIT,
        include_internal=True,
    )
    messages: list[dict[str, str]] = [{"role": "system", "content": _LOOP_CONTROLLER_SYSTEM_PROMPT}]
    for row in prior_messages:
        if row.role not in {"user", "assistant"}:
            continue
        content = _truncate_text(row.content)
        if not content:
            continue
        messages.append({"role": row.role, "content": content})
    messages.append({"role": "user", "content": json.dumps(payload, indent=2, ensure_ascii=False)})
    return messages


def _serialize_dialog_tail(dialog_tail: list[dict[str, Any]]) -> list[dict[str, str]]:
    serialized: list[dict[str, str]] = []
    for item in dialog_tail[-_TRANSCRIPT_TAIL_LIMIT:]:
        role = str(item.get("role") or "").strip()
        if role not in {"user", "assistant"}:
            continue
        text = _truncate_text(str(item.get("text") or ""))
        if not text:
            continue
        serialized.append({"role": role, "text": text})
    return serialized


def build_loop_controller_payload(
    *,
    session: AgentSession,
    turn_text: str,
    last_user_text: str | None,
    turn_index: int,
    assistant_event_id: int,
    auto_continue_streak: int,
    dialog_tail: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "session": {
            "session_id": str(session.id),
            "provider": session.provider,
            "project": session.project,
            "cwd": session.cwd,
            "summary_title": _truncate_text(session.summary_title, limit=400),
            "summary": _truncate_text(session.summary, limit=1200),
            "loop_mode": getattr(session, "loop_mode", "manual"),
            "resume_supported": (session.provider or "").strip().lower() == "claude",
        },
        "latest_turn": {
            "assistant_event_id": assistant_event_id,
            "turn_index": turn_index,
            "assistant_text": _truncate_text(turn_text, limit=3500),
            "last_user_text": _truncate_text(last_user_text, limit=2000),
        },
        "recent_dialog_tail": _serialize_dialog_tail(dialog_tail),
        "loop_history": {
            "auto_continue_streak": auto_continue_streak,
        },
        "instructions": {
            "same_session_only": True,
            "ai_first": True,
        },
    }


async def evaluate_session_turn_with_llm(
    *,
    db: Session,
    owner_id: int,
    session: AgentSession,
    payload: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> LoopControllerDecision:
    """Evaluate one completed assistant turn with the per-session controller."""
    loop_thread = get_or_create_session_loop_thread(db, owner_id=owner_id, session=session)

    messages = _build_controller_messages(loop_thread_id=loop_thread.id, payload=payload, db=db)
    prompt_json = json.dumps(payload, indent=2, ensure_ascii=False)
    create_thread_message(
        db,
        thread_id=loop_thread.id,
        role="user",
        content=prompt_json,
        processed=True,
        internal=True,
        message_metadata={"kind": "loop_turn_payload", **(metadata or {})},
    )

    client, model_id, provider = get_llm_client_with_db_fallback(_LOOP_CONTROLLER_USE_CASE, db=db)
    if str(getattr(provider, "value", provider)).lower() == "anthropic":
        raise RuntimeError("Anthropic-native loop controller path is not implemented yet")

    try:
        response = await client.chat.completions.create(
            model=model_id,
            messages=messages,
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        raw = (response.choices[0].message.content or "").strip()
        decision = _parse_decision(raw, model_id=model_id, loop_thread_id=loop_thread.id)
    finally:
        await client.close()

    create_thread_message(
        db,
        thread_id=loop_thread.id,
        role="assistant",
        content=decision.raw_response or "",
        processed=True,
        internal=True,
        message_metadata={
            "kind": "loop_turn_decision",
            "model_id": decision.model_id,
            "decision": decision.decision,
            **(metadata or {}),
        },
    )
    return decision


__all__ = [
    "LoopControllerDecision",
    "build_loop_controller_payload",
    "evaluate_session_turn_with_llm",
    "get_or_create_loop_controller_fiche",
    "get_or_create_session_loop_thread",
]
