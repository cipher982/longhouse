"""Daily digest job - emails a summary of AI coding sessions from the previous day.

This job uses a map-reduce pattern:
1. MAP: Summarize each session individually (parallel, max 3 concurrent)
2. REDUCE: Aggregate summaries into an HTML digest email

Configuration:
- DIGEST_EMAIL: Target email address (required to enable job)
- DIGEST_CRON: Schedule (default: "0 8 * * *" = 8 AM daily)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
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
from zerg.models_config import get_model_for_use_case
from zerg.shared import redact_text
from zerg.shared import send_digest_email
from zerg.shared import truncate_to_tokens

logger = logging.getLogger(__name__)

# Constants
SESSION_TOKEN_BUDGET = 8000  # Max tokens per session for summarization
MESSAGE_TOKEN_LIMIT = 1000  # Max tokens per individual message
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
class Message:
    """A simplified message for summarization."""

    role: str
    content: str
    tool_name: str | None = None


@dataclass
class SessionSummary:
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
class DigestResult:
    """Result of the digest job."""

    success: bool
    sessions_processed: int
    sessions_summarized: int
    email_sent: bool
    message_id: str | None = None
    error: str | None = None
    usage: Usage = field(default_factory=Usage)


# -----------------------------------------------------------------------------
# Noise stripping patterns
# -----------------------------------------------------------------------------

# Patterns to strip from content before summarization
NOISE_PATTERNS = [
    # System reminders
    re.compile(r"<system-reminder>[\s\S]*?</system-reminder>", re.IGNORECASE),
    # Function results (long tool outputs)
    re.compile(r"<function_results>[\s\S]*?</function_results>", re.IGNORECASE),
    # Verbose XML tags
    re.compile(r"<env>[\s\S]*?</env>", re.IGNORECASE),
    # Claude background info
    re.compile(r"<claude_background_info>[\s\S]*?</claude_background_info>", re.IGNORECASE),
]


def strip_noise(content: str) -> str:
    """Remove noise patterns from content."""
    if not content:
        return content
    result = content
    for pattern in NOISE_PATTERNS:
        result = pattern.sub("", result)
    # Collapse multiple newlines
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


# -----------------------------------------------------------------------------
# Data fetching
# -----------------------------------------------------------------------------


def fetch_sessions_for_day(db: Session, target_date: datetime) -> list[SessionData]:
    """Fetch all production sessions for a given day.

    Args:
        db: Database session
        target_date: The date to fetch sessions for

    Returns:
        List of SessionData for the day
    """
    # Calculate day boundaries in UTC
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


def fetch_session_thread(db: Session, session_id: str) -> list[Message]:
    """Fetch all events for a session as a simplified message thread.

    Args:
        db: Database session
        session_id: The session ID

    Returns:
        List of Message objects representing the conversation
    """
    stmt = select(AgentEvent).where(AgentEvent.session_id == session_id).order_by(AgentEvent.timestamp)

    events = db.execute(stmt).scalars().all()

    messages = []
    for event in events:
        content = event.content_text or ""
        # Include tool output in content if present
        if event.tool_output_text:
            content = f"{content}\n\nTool output: {event.tool_output_text[:500]}..."

        if content.strip():
            messages.append(
                Message(
                    role=event.role,
                    content=content,
                    tool_name=event.tool_name,
                )
            )

    return messages


# -----------------------------------------------------------------------------
# Thread preparation
# -----------------------------------------------------------------------------


def prepare_thread(messages: list[Message], budget_tokens: int) -> str:
    """Prepare a thread for summarization.

    Applies noise stripping, redaction, and truncation.

    Args:
        messages: List of messages
        budget_tokens: Max tokens for the output

    Returns:
        Formatted thread string
    """
    # Format messages
    parts = []
    for msg in messages:
        role_label = msg.role.upper()
        if msg.tool_name:
            role_label = f"{role_label} (tool: {msg.tool_name})"

        # Clean and truncate individual message
        content = strip_noise(msg.content)
        content = redact_text(content)
        content, _ = truncate_to_tokens(content, MESSAGE_TOKEN_LIMIT)

        parts.append(f"[{role_label}]\n{content}")

    thread_text = "\n\n---\n\n".join(parts)

    # Truncate entire thread to budget
    thread_text, _ = truncate_to_tokens(thread_text, budget_tokens)

    return thread_text


# -----------------------------------------------------------------------------
# LLM calls
# -----------------------------------------------------------------------------


async def map_session(
    session: SessionData,
    db: Session,
    client: AsyncOpenAI,
    model: str,
    semaphore: asyncio.Semaphore,
) -> tuple[SessionSummary | None, Usage]:
    """Summarize a single session (map phase).

    Args:
        session: Session metadata
        db: Database session
        client: OpenAI client
        model: Model ID to use
        semaphore: Concurrency limiter

    Returns:
        Tuple of (SessionSummary or None, Usage)
    """
    usage = Usage()

    async with semaphore:
        try:
            # Fetch and prepare thread
            messages = fetch_session_thread(db, session.id)
            if not messages:
                logger.debug("Session %s has no messages, skipping", session.id)
                return None, usage

            thread_text = prepare_thread(messages, SESSION_TOKEN_BUDGET)
            if not thread_text.strip():
                return None, usage

            # Build context
            context_parts = []
            if session.project:
                context_parts.append(f"Project: {session.project}")
            if session.provider:
                context_parts.append(f"Provider: {session.provider}")
            if session.git_branch:
                context_parts.append(f"Branch: {session.git_branch}")
            context = ", ".join(context_parts) if context_parts else "Unknown context"

            # Call LLM
            prompt = f"""Summarize this AI coding session in 2-4 sentences.
Focus on: what was worked on, what was accomplished, notable outcomes.
Be specific about files, features, or bugs mentioned.

Context: {context}
Duration: {session.user_messages} user messages, {session.assistant_messages} assistant messages, {session.tool_calls} tool calls

Session transcript:
{thread_text}"""

            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
            )

            # Track usage
            if response.usage:
                usage.prompt_tokens = response.usage.prompt_tokens
                usage.completion_tokens = response.usage.completion_tokens
                usage.total_tokens = response.usage.total_tokens

            summary_text = response.choices[0].message.content or ""

            return (
                SessionSummary(
                    session_id=session.id,
                    project=session.project,
                    provider=session.provider,
                    started_at=session.started_at,
                    summary=summary_text.strip(),
                    user_messages=session.user_messages,
                    assistant_messages=session.assistant_messages,
                    tool_calls=session.tool_calls,
                ),
                usage,
            )

        except Exception as e:
            logger.exception("Failed to summarize session %s: %s", session.id, e)
            return None, usage


async def reduce_digest(
    summaries: list[SessionSummary],
    client: AsyncOpenAI,
    model: str,
    target_date: datetime,
) -> tuple[bool, str | None, str | None, Usage]:
    """Generate the final digest HTML from summaries (reduce phase).

    Args:
        summaries: List of session summaries
        client: OpenAI client
        model: Model ID to use
        target_date: The date being summarized

    Returns:
        Tuple of (success, html, error, usage)
    """
    usage = Usage()

    if not summaries:
        return False, None, "No summaries to reduce", usage

    # Group by project
    by_project: dict[str, list[SessionSummary]] = {}
    for s in summaries:
        project = s.project or "Unspecified"
        by_project.setdefault(project, []).append(s)

    # Format summaries for LLM
    summary_lines = []
    for project, project_summaries in sorted(by_project.items()):
        summary_lines.append(f"\n## {project}")
        for s in project_summaries:
            time_str = s.started_at.strftime("%H:%M")
            stats = f"({s.user_messages}u/{s.assistant_messages}a/{s.tool_calls}t)"
            summary_lines.append(f"- [{time_str}] {s.provider} {stats}: {s.summary}")

    summaries_text = "\n".join(summary_lines)
    date_str = target_date.strftime("%Y-%m-%d")
    total_sessions = len(summaries)

    prompt = f"""Generate a daily work digest email in HTML format for {date_str}.

Requirements:
- Executive summary (2-3 sentences) highlighting key accomplishments
- Sections for each project with bullet points
- Footer with total session count ({total_sessions} sessions)
- Clean, professional HTML (inline styles OK)
- Use a simple color scheme (dark text, light background)

Session summaries by project:
{summaries_text}

Generate ONLY the HTML body content (no doctype, html, head, or body tags)."""

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
        )

        if response.usage:
            usage.prompt_tokens = response.usage.prompt_tokens
            usage.completion_tokens = response.usage.completion_tokens
            usage.total_tokens = response.usage.total_tokens

        html = response.choices[0].message.content or ""

        # Basic validation
        if not html.strip():
            return False, None, "Empty response from LLM", usage

        return True, html.strip(), None, usage

    except Exception as e:
        logger.exception("Failed to generate digest: %s", e)
        return False, None, str(e), usage


# -----------------------------------------------------------------------------
# Main entry point
# -----------------------------------------------------------------------------


async def run() -> dict[str, Any]:
    """Run the daily digest job.

    Returns:
        Dict with job results
    """
    # Check if configured
    digest_email = os.getenv("DIGEST_EMAIL")
    if not digest_email:
        logger.info("DIGEST_EMAIL not set, skipping digest job")
        return {
            "success": True,
            "skipped": True,
            "reason": "DIGEST_EMAIL not configured",
        }

    # Check for OpenAI API key
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.error("OPENAI_API_KEY not set")
        return {
            "success": False,
            "error": "OPENAI_API_KEY not configured",
        }

    # Get model for summarization
    model = get_model_for_use_case("summarization")
    logger.info("Using model %s for summarization", model)

    # Calculate yesterday's date
    now = datetime.now(UTC)
    yesterday = now - timedelta(days=1)

    total_usage = Usage()
    result = DigestResult(
        success=False,
        sessions_processed=0,
        sessions_summarized=0,
        email_sent=False,
    )

    try:
        # Create OpenAI client
        client = AsyncOpenAI(api_key=api_key)

        # Fetch sessions
        with db_session() as db:
            sessions = fetch_sessions_for_day(db, yesterday)

        result.sessions_processed = len(sessions)
        logger.info("Found %d sessions for %s", len(sessions), yesterday.strftime("%Y-%m-%d"))

        if not sessions:
            result.success = True
            return {
                "success": True,
                "sessions_processed": 0,
                "message": "No sessions found for yesterday",
            }

        # MAP phase: summarize each session (parallel with semaphore)
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_MAPS)
        summaries: list[SessionSummary] = []

        async def process_session(session: SessionData) -> tuple[SessionSummary | None, Usage]:
            with db_session() as db:
                return await map_session(session, db, client, model, semaphore)

        tasks = [process_session(s) for s in sessions]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, Exception):
                logger.error("Session processing error: %s", r)
                continue
            summary, usage = r
            total_usage.add(usage)
            if summary:
                summaries.append(summary)

        result.sessions_summarized = len(summaries)
        logger.info("Summarized %d/%d sessions", len(summaries), len(sessions))

        if not summaries:
            result.success = True
            return {
                "success": True,
                "sessions_processed": result.sessions_processed,
                "sessions_summarized": 0,
                "message": "No sessions had content to summarize",
            }

        # REDUCE phase: generate digest
        success, html, error, reduce_usage = await reduce_digest(summaries, client, model, yesterday)
        total_usage.add(reduce_usage)

        if not success:
            result.error = error
            return {
                "success": False,
                "error": error,
                "sessions_processed": result.sessions_processed,
                "sessions_summarized": result.sessions_summarized,
            }

        # Send email
        date_str = yesterday.strftime("%Y-%m-%d")
        subject = f"AI Coding Digest - {date_str}"
        plain_text = f"Daily digest for {date_str}. View in HTML-capable email client."

        message_id = send_digest_email(
            subject,
            plain_text,
            html=html,
            job_id="daily-digest",
            metadata={
                "date": date_str,
                "sessions": result.sessions_summarized,
                "usage": {
                    "prompt_tokens": total_usage.prompt_tokens,
                    "completion_tokens": total_usage.completion_tokens,
                    "total_tokens": total_usage.total_tokens,
                },
            },
        )

        result.email_sent = message_id is not None
        result.message_id = message_id
        result.success = result.email_sent
        result.usage = total_usage

        if not result.email_sent:
            result.error = "Failed to send email (check SES configuration)"

        return {
            "success": result.success,
            "sessions_processed": result.sessions_processed,
            "sessions_summarized": result.sessions_summarized,
            "email_sent": result.email_sent,
            "message_id": result.message_id,
            "error": result.error,
            "usage": {
                "prompt_tokens": total_usage.prompt_tokens,
                "completion_tokens": total_usage.completion_tokens,
                "total_tokens": total_usage.total_tokens,
            },
        }

    except Exception as e:
        logger.exception("Daily digest job failed: %s", e)
        return {
            "success": False,
            "error": str(e),
            "sessions_processed": result.sessions_processed,
            "sessions_summarized": result.sessions_summarized,
        }


# -----------------------------------------------------------------------------
# Job registration
# -----------------------------------------------------------------------------

# Register the job - auto-enabled when DIGEST_EMAIL is set
job_registry.register(
    JobConfig(
        id="daily-digest",
        cron=os.getenv("DIGEST_CRON", "0 8 * * *"),
        func=run,
        enabled=bool(os.getenv("DIGEST_EMAIL")),
        timeout_seconds=600,  # 10 minutes
        tags=["digest", "email", "builtin"],
        description="Daily email digest of AI coding sessions",
    )
)
