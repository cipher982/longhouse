"""Daily digest job - emails users summaries of their AI coding sessions.

This job uses a map-reduce pattern:
1. MAP: Summarize each session individually (parallel, max 3 concurrent)
2. REDUCE: Aggregate summaries into a plain-text digest email

Users must:
1. Connect their Gmail account (OAuth)
2. Enable digest in their settings (digest_enabled = True)

The digest is sent via the user's own Gmail account (from themselves to themselves).
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

from openai import AsyncOpenAI
from sqlalchemy import and_
from sqlalchemy import select
from sqlalchemy.orm import Session

from zerg.database import db_session
from zerg.jobs.registry import JobConfig
from zerg.jobs.registry import job_registry
from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession
from zerg.models.connector import Connector
from zerg.models.user import User
from zerg.models_config import get_model_for_use_case
from zerg.services import gmail_api
from zerg.services.session_processing import summarize_events
from zerg.utils import crypto

logger = logging.getLogger(__name__)

# Constants
MAX_CONCURRENT_MAPS = 3  # Limit concurrent LLM calls


@dataclass
class SessionData:
    """Data about a single agent session."""

    id: str
    provider: str
    project: str | None
    started_at: datetime
    ended_at: datetime | None
    user_messages: int
    assistant_messages: int
    tool_calls: int
    cwd: str | None
    git_branch: str | None


@dataclass
class DigestSessionSummary:
    """Summary of a single session from the map phase."""

    session_id: str
    project: str | None
    provider: str
    started_at: datetime
    summary: str
    user_messages: int
    assistant_messages: int
    tool_calls: int


@dataclass
class Usage:
    """Token usage tracking."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, other: "Usage") -> None:
        """Add another usage to this one."""
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens


@dataclass
class UserDigestResult:
    """Result of sending digest to a single user."""

    user_id: int
    user_email: str
    success: bool
    sessions_processed: int = 0
    sessions_summarized: int = 0
    message_id: str | None = None
    error: str | None = None
    usage: Usage = field(default_factory=Usage)


# -----------------------------------------------------------------------------
# Data fetching
# -----------------------------------------------------------------------------


def fetch_sessions_for_day(db: Session, target_date: datetime) -> list[SessionData]:
    """Fetch all production sessions for a given day."""
    day_start = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    stmt = (
        select(AgentSession)
        .where(
            and_(
                AgentSession.started_at >= day_start,
                AgentSession.started_at < day_end,
                AgentSession.environment == "production",
            )
        )
        .order_by(AgentSession.started_at)
    )

    results = db.execute(stmt).scalars().all()

    return [
        SessionData(
            id=str(s.id),
            provider=s.provider,
            project=s.project,
            started_at=s.started_at,
            ended_at=s.ended_at,
            user_messages=s.user_messages or 0,
            assistant_messages=s.assistant_messages or 0,
            tool_calls=s.tool_calls or 0,
            cwd=s.cwd,
            git_branch=s.git_branch,
        )
        for s in results
    ]


def _fetch_events_as_dicts(db: Session, session_id: str) -> list[dict]:
    """Fetch AgentEvent rows and convert to dicts for build_transcript()."""
    stmt = select(AgentEvent).where(AgentEvent.session_id == session_id).order_by(AgentEvent.timestamp)
    events = db.execute(stmt).scalars().all()

    return [
        {
            "role": e.role,
            "content_text": e.content_text,
            "tool_name": e.tool_name,
            "tool_output_text": e.tool_output_text,
            "timestamp": e.timestamp,
            "session_id": str(e.session_id),
        }
        for e in events
    ]


# -----------------------------------------------------------------------------
# LLM calls
# -----------------------------------------------------------------------------


async def map_session(
    session: SessionData,
    db: Session,
    client: AsyncOpenAI,
    model: str,
    semaphore: asyncio.Semaphore,
    provider: str = "openai",
) -> tuple[DigestSessionSummary | None, Usage]:
    """Summarize a single session (map phase).

    Uses summarize_events() — the shared entry point that handles
    transcript building, context-window truncation, and LLM dispatch.
    """
    usage = Usage()

    async with semaphore:
        try:
            event_dicts = _fetch_events_as_dicts(db, session.id)
            if not event_dicts:
                return None, usage

            result = await summarize_events(
                event_dicts,
                client=client,
                model=model,
                provider=provider,
                metadata={
                    "project": session.project,
                    "provider": session.provider,
                    "git_branch": session.git_branch,
                },
            )

            if not result:
                return None, usage

            return (
                DigestSessionSummary(
                    session_id=session.id,
                    project=session.project,
                    provider=session.provider,
                    started_at=session.started_at,
                    summary=result.summary,
                    user_messages=session.user_messages,
                    assistant_messages=session.assistant_messages,
                    tool_calls=session.tool_calls,
                ),
                usage,
            )

        except Exception:
            logger.exception("Failed to summarize session %s", session.id)
            return None, usage


def format_plain_text_digest(summaries: list[DigestSessionSummary], target_date: datetime) -> str:
    """Generate plain-text digest from summaries (no LLM needed for MVP)."""
    date_str = target_date.strftime("%Y-%m-%d")

    # Group by project
    by_project: dict[str, list[DigestSessionSummary]] = {}
    for s in summaries:
        project = s.project or "Unspecified"
        by_project.setdefault(project, []).append(s)

    lines = [
        f"AI Coding Digest - {date_str}",
        "=" * 40,
        "",
    ]

    for project, project_summaries in sorted(by_project.items()):
        lines.append(f"## {project}")
        lines.append("")
        for s in project_summaries:
            time_str = s.started_at.strftime("%H:%M")
            stats = f"({s.user_messages}u/{s.assistant_messages}a/{s.tool_calls}t)"
            lines.append(f"  [{time_str}] {s.provider} {stats}")
            lines.append(f"    {s.summary}")
            lines.append("")

    lines.append("-" * 40)
    lines.append(f"Total: {len(summaries)} sessions")

    return "\n".join(lines)


# -----------------------------------------------------------------------------
# Per-user digest sending
# -----------------------------------------------------------------------------


async def send_user_digest(
    user_id: int,
    target_date: datetime,
    openai_client: AsyncOpenAI,
    model: str,
) -> UserDigestResult:
    """Send digest to a single user via their Gmail connector."""
    result = UserDigestResult(user_id=user_id, user_email="", success=False)

    with db_session() as db:
        # Get user
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            result.error = "User not found"
            return result

        result.user_email = user.email

        # Idempotency check - don't send if already sent today
        if user.last_digest_sent_at:
            last_sent_date = user.last_digest_sent_at.date()
            if last_sent_date >= target_date.date():
                result.error = f"Already sent digest for {last_sent_date}"
                result.success = True  # Not a failure, just skipped
                return result

        # Get Gmail connector
        connector = (
            db.query(Connector)
            .filter(
                Connector.owner_id == user_id,
                Connector.type == "email",
                Connector.provider == "gmail",
            )
            .first()
        )

        if not connector:
            result.error = "No Gmail connector configured"
            return result

        # Get refresh token
        config = connector.config or {}
        enc_token = config.get("refresh_token")
        if not enc_token:
            result.error = "Gmail connector missing refresh token"
            return result

        try:
            refresh_token = crypto.decrypt(enc_token)
        except Exception as e:
            result.error = f"Failed to decrypt refresh token: {e}"
            return result

        # Get stored email or fetch from profile
        user_gmail = config.get("email")
        if not user_gmail:
            try:
                access_token = await gmail_api.async_exchange_refresh_token(refresh_token)
                profile = await gmail_api.async_get_profile(access_token)
                user_gmail = profile.get("emailAddress")
                if not user_gmail:
                    result.error = "Could not get Gmail address from profile"
                    return result
            except Exception as e:
                result.error = f"Failed to get Gmail profile: {e}"
                return result

        # Fetch sessions for the target date
        sessions = fetch_sessions_for_day(db, target_date)

    result.sessions_processed = len(sessions)

    if not sessions:
        # No sessions - update last_sent_at anyway to prevent re-checking
        with db_session() as db:
            user = db.query(User).filter(User.id == user_id).first()
            if user:
                user.last_digest_sent_at = datetime.now(UTC)
                db.commit()
        result.success = True
        result.error = "No sessions to summarize"
        return result

    # MAP phase: summarize sessions
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_MAPS)
    summaries: list[DigestSessionSummary] = []

    async def process_session(session: SessionData) -> tuple[DigestSessionSummary | None, Usage]:
        with db_session() as db:
            return await map_session(session, db, openai_client, model, semaphore)

    tasks = [process_session(s) for s in sessions]
    map_results = await asyncio.gather(*tasks, return_exceptions=True)

    for r in map_results:
        if isinstance(r, Exception):
            logger.error("Session processing error: %s", r)
            continue
        summary, usage = r
        result.usage.add(usage)
        if summary:
            summaries.append(summary)

    result.sessions_summarized = len(summaries)

    if not summaries:
        with db_session() as db:
            user = db.query(User).filter(User.id == user_id).first()
            if user:
                user.last_digest_sent_at = datetime.now(UTC)
                db.commit()
        result.success = True
        result.error = "No sessions had content to summarize"
        return result

    # Format digest (plain text for MVP - no LLM reduce phase needed)
    digest_text = format_plain_text_digest(summaries, target_date)

    # Send via Gmail
    try:
        access_token = await gmail_api.async_exchange_refresh_token(refresh_token)
        date_str = target_date.strftime("%Y-%m-%d")
        subject = f"AI Coding Digest - {date_str}"

        message_id = await gmail_api.async_send_email(
            access_token=access_token,
            to=user_gmail,
            subject=subject,
            body_text=digest_text,
        )

        if message_id:
            result.success = True
            result.message_id = message_id

            # Update last_digest_sent_at
            with db_session() as db:
                user = db.query(User).filter(User.id == user_id).first()
                if user:
                    user.last_digest_sent_at = datetime.now(UTC)
                    db.commit()
        else:
            result.error = "Gmail API returned no message ID"

    except Exception as e:
        result.error = f"Failed to send email: {e}"
        logger.exception("Failed to send digest for user %d: %s", user_id, e)

    return result


# -----------------------------------------------------------------------------
# Main entry point
# -----------------------------------------------------------------------------


async def run() -> dict[str, Any]:
    """Run the daily digest job for all users with digest enabled.

    Returns:
        Dict with job results
    """
    # Check for OpenAI API key (required for summarization)
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.error("OPENAI_API_KEY not set")
        return {"success": False, "error": "OPENAI_API_KEY not configured"}

    model = get_model_for_use_case("summarization")
    logger.info("Using model %s for summarization", model)

    # Calculate yesterday's date
    now = datetime.now(UTC)
    yesterday = now - timedelta(days=1)

    # Find users with digest enabled
    with db_session() as db:
        users = db.query(User).filter(User.digest_enabled == True).all()  # noqa: E712
        user_ids = [u.id for u in users]

    if not user_ids:
        logger.info("No users have digest enabled")
        return {"success": True, "users_processed": 0, "message": "No users have digest enabled"}

    logger.info("Processing digests for %d users", len(user_ids))

    # Create OpenAI client
    openai_client = AsyncOpenAI(api_key=api_key)

    # Process each user
    results: list[UserDigestResult] = []
    for user_id in user_ids:
        result = await send_user_digest(user_id, yesterday, openai_client, model)
        results.append(result)
        logger.info(
            "User %d (%s): success=%s, sessions=%d, error=%s",
            result.user_id,
            result.user_email,
            result.success,
            result.sessions_summarized,
            result.error,
        )

    # Aggregate results
    total_success = sum(1 for r in results if r.success)
    total_failed = len(results) - total_success
    total_sessions = sum(r.sessions_summarized for r in results)
    total_usage = Usage()
    for r in results:
        total_usage.add(r.usage)

    return {
        "success": total_failed == 0,
        "users_processed": len(results),
        "users_success": total_success,
        "users_failed": total_failed,
        "total_sessions_summarized": total_sessions,
        "usage": {
            "prompt_tokens": total_usage.prompt_tokens,
            "completion_tokens": total_usage.completion_tokens,
            "total_tokens": total_usage.total_tokens,
        },
        "user_results": [
            {
                "user_id": r.user_id,
                "email": r.user_email,
                "success": r.success,
                "sessions": r.sessions_summarized,
                "message_id": r.message_id,
                "error": r.error,
            }
            for r in results
        ],
    }


# -----------------------------------------------------------------------------
# Job registration
# -----------------------------------------------------------------------------

# Always enabled - the job checks for users with digest_enabled
job_registry.register(
    JobConfig(
        id="daily-digest",
        cron=os.getenv("DIGEST_CRON", "0 8 * * *"),
        func=run,
        enabled=True,  # Always enabled, user preference controls delivery
        timeout_seconds=600,  # 10 minutes
        tags=["digest", "email", "builtin"],
        description="Daily email digest of AI coding sessions via Gmail",
    )
)
