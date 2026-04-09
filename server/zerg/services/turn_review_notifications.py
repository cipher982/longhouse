"""Turn review notification dispatch (Telegram, push, mobile).

Extracted from session_turn_reviews.py — notification routing is a
separate concern from review orchestration.
"""

from __future__ import annotations

import logging
import os

from sqlalchemy.orm import Session

from zerg.config import get_settings
from zerg.models.agents import AgentSession
from zerg.models.agents import SessionTurnReview

logger = logging.getLogger(__name__)

_ATTENTION_EXECUTION_STATES = {"awaiting_user_approval", "needs_human"}


def _session_title(session: AgentSession) -> str:
    if session.summary_title and str(session.summary_title).strip():
        return str(session.summary_title).strip()
    if session.project and str(session.project).strip():
        return str(session.project).strip()
    if session.cwd and str(session.cwd).strip():
        return os.path.basename(str(session.cwd).rstrip("/")) or str(session.cwd).strip()
    return f"Session {str(session.id)[:8]}"


def _public_loop_url(review_id: int) -> str | None:
    settings = get_settings()
    base_url = str(settings.app_public_url or settings.public_site_url or "").strip().rstrip("/")
    if not base_url:
        return None
    return f"{base_url}/loop/card/{review_id}"


def _build_turn_review_notification_text(*, review: SessionTurnReview, session: AgentSession) -> str:
    title = _session_title(session)
    attention_label = "Needs approval" if review.execution_state == "awaiting_user_approval" else "Needs attention"
    lines = [
        f"**{title}**",
        attention_label,
        review.summary,
    ]
    if review.follow_up_prompt:
        lines.append(f"Suggested next step: {review.follow_up_prompt}")
    loop_url = _public_loop_url(int(review.id))
    if loop_url:
        lines.append(f"Open in Loop: {loop_url}")
    return "\n".join(line.strip() for line in lines if str(line).strip())


def _review_requires_mobile_attention(review: SessionTurnReview) -> bool:
    return review.execution_state in _ATTENTION_EXECUTION_STATES


async def _send_turn_review_telegram_notification(
    *,
    db: Session,
    review: SessionTurnReview,
    session: AgentSession,
) -> bool:
    if not _review_requires_mobile_attention(review):
        return False

    from zerg.models.user import User

    user = db.query(User).filter(User.id == review.owner_id).first()
    if user is None:
        return False

    chat_id = str((user.context or {}).get("telegram_chat_id", "")).strip()
    if not chat_id:
        return False

    from zerg.channels.registry import get_registry
    from zerg.channels.types import ChannelMessage
    from zerg.services.telegram_bridge import _format_for_telegram

    channel = get_registry().get("telegram")
    if not channel:
        return False

    message = _build_turn_review_notification_text(review=review, session=session)
    result = await channel.send_message(
        ChannelMessage(
            channel_id="telegram",
            to=chat_id,
            text=_format_for_telegram(message),
            parse_mode="html",
            disable_web_page_preview=True,
        )
    )
    return bool(result.get("success"))


def _send_turn_review_push_notification(
    *,
    db: Session,
    review: SessionTurnReview,
    session: AgentSession,
) -> bool:
    if not _review_requires_mobile_attention(review):
        return False
    from zerg.services.loop_push import send_loop_push_nudge

    return send_loop_push_nudge(
        db=db,
        owner_id=int(review.owner_id),
        review=review,
        session=session,
    )


async def _send_turn_review_mobile_notification(
    *,
    db: Session,
    review: SessionTurnReview,
    session: AgentSession,
) -> bool:
    if not _review_requires_mobile_attention(review):
        return False
    if _send_turn_review_push_notification(db=db, review=review, session=session):
        return True
    return await _send_turn_review_telegram_notification(db=db, review=review, session=session)
