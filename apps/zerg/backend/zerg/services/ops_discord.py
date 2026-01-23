"""Discord notifications for Ops (budget alerts, daily digest)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from datetime import timezone
from typing import Optional

import httpx

from zerg.config import get_settings

logger = logging.getLogger(__name__)


_last_alert_key: tuple[str, str] | None = None
_last_alert_ts: float | None = None
_DEBOUNCE_SECONDS = 600  # 10 minutes


async def _post_discord(webhook_url: str, content: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(webhook_url, json={"content": content})
            if resp.status_code >= 300:
                logger.warning("Discord webhook returned %s: %s", resp.status_code, resp.text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Discord webhook error: %s", exc)


def _webhook_url() -> Optional[str]:
    url = getattr(get_settings(), "discord_webhook_url", None)
    return url if url else None


def _alerts_enabled() -> bool:
    settings = get_settings()
    if settings.testing:
        return False
    return bool(getattr(settings, "discord_enable_alerts", False))


async def send_budget_alert(scope: str, percent: float, used_usd: float, limit_cents: int, user_email: Optional[str] = None) -> None:
    """Send a budget threshold alert to Discord.

    Fire-and-forget; respects DISCORD_ENABLE_ALERTS and webhook presence.
    De-bounces identical alerts within a short window.
    """
    if not _alerts_enabled():
        return
    url = _webhook_url()
    if not url:
        return

    global _last_alert_key, _last_alert_ts
    key = (scope, f"{int(percent)}")
    now_ts = datetime.now(timezone.utc).timestamp()
    if _last_alert_key == key and _last_alert_ts and (now_ts - _last_alert_ts) < _DEBOUNCE_SECONDS:
        return

    budget_usd = limit_cents / 100.0
    level = "DENY" if percent >= 100.0 else "WARN"
    who = f" by {user_email}" if user_email else ""
    content = f"[Budget {level}] {scope} at {percent:.1f}% ({used_usd:.2f}/${budget_usd:.2f}){who}."

    # Fire-and-forget
    try:
        asyncio.create_task(_post_discord(url, content))
    except RuntimeError:
        # Fallback for sync contexts where no event loop is running
        import threading

        threading.Thread(target=lambda: httpx.post(url, json={"content": content}), daemon=True).start()

    _last_alert_key = key
    _last_alert_ts = now_ts


async def send_daily_digest(content: str) -> None:
    """Send a daily digest string to Discord (optional)."""
    if not _alerts_enabled():
        return
    url = _webhook_url()
    if not url:
        return
    await _post_discord(url, content)


async def send_user_signup_alert(user_email: str, user_count: Optional[int] = None) -> None:
    """Send a user signup notification to Discord with @here ping.

    Fire-and-forget; respects DISCORD_ENABLE_ALERTS and webhook presence.
    """
    if not _alerts_enabled():
        return
    url = _webhook_url()
    if not url:
        return

    # Format user count info if provided
    count_info = f" (#{user_count} total)" if user_count else ""

    content = f"@here 🎉 **New User Signup!** {user_email} just joined Swarmlet{count_info}"

    # Fire-and-forget
    try:
        asyncio.create_task(_post_discord(url, content))
    except RuntimeError:
        # Fallback for sync contexts where no event loop is running
        import threading

        threading.Thread(target=lambda: httpx.post(url, json={"content": content}), daemon=True).start()


async def send_waitlist_signup_alert(email: str, source: str, waitlist_count: Optional[int] = None) -> None:
    """Send a waitlist signup notification to Discord.

    Fire-and-forget; respects DISCORD_ENABLE_ALERTS and webhook presence.
    """
    if not _alerts_enabled():
        return
    url = _webhook_url()
    if not url:
        return

    count_info = f" (#{waitlist_count} on waitlist)" if waitlist_count else ""
    content = f"📋 **Waitlist Signup!** {email} joined the {source} waitlist{count_info}"

    # Fire-and-forget
    try:
        asyncio.create_task(_post_discord(url, content))
    except RuntimeError:
        # Fallback for sync contexts where no event loop is running
        import threading

        threading.Thread(target=lambda: httpx.post(url, json={"content": content}), daemon=True).start()


async def send_qa_alert(issue: dict, dashboard_url: str = "https://swarmlet.com/reliability") -> None:
    """Send a QA chronic issue alert to Discord.

    Called when the QA agent detects a new chronic issue that needs attention.

    Parameters
    ----------
    issue
        Issue dict with: fingerprint, description, severity, first_seen,
        occurrences, consecutive, current_value, threshold
    dashboard_url
        URL to the reliability dashboard
    """
    if not _alerts_enabled():
        logger.debug("Discord alerts disabled, skipping QA alert")
        return
    url = _webhook_url()
    if not url:
        logger.debug("No Discord webhook configured, skipping QA alert")
        return

    # Build alert message
    severity = issue.get("severity", "warning").upper()
    emoji = "🔴" if severity == "CRITICAL" else "🟡"
    description = issue.get("description", "Unknown issue")
    first_seen = issue.get("first_seen", "unknown")
    occurrences = issue.get("occurrences", 1)
    consecutive = issue.get("consecutive", 1)
    current_value = issue.get("current_value", "N/A")
    threshold = issue.get("threshold", "N/A")

    content = f"""{emoji} **[SWARMLET QA] Chronic Issue Detected**

**Issue:** {description}
**Severity:** {severity}
**Duration:** Since {first_seen}
**Occurrences:** {occurrences} ({consecutive} consecutive)
**Current:** {current_value} (threshold: {threshold})

[View Dashboard]({dashboard_url})"""

    # Fire-and-forget
    try:
        asyncio.create_task(_post_discord(url, content))
        logger.info("Queued QA alert for issue: %s", issue.get("fingerprint", "unknown"))
    except RuntimeError:
        # Fallback for sync contexts where no event loop is running
        import threading

        threading.Thread(
            target=lambda: httpx.post(url, json={"content": content}),
            daemon=True,
        ).start()
        logger.info("Queued QA alert (sync fallback) for issue: %s", issue.get("fingerprint", "unknown"))


async def send_run_completion_notification(
    run_id: int,
    status: str,
    summary: Optional[str] = None,
    error: Optional[str] = None,
    run_url: Optional[str] = None,
    *,
    webhook_url: Optional[str] = None,
) -> None:
    """Send a run completion notification to Discord or Slack.

    This is used for cloud agent execution notifications, allowing users
    to close their laptop and get notified when work completes.

    Parameters
    ----------
    run_id
        The AgentRun ID
    status
        Run status: "success", "failed", etc.
    summary
        Brief summary of the result (for success)
    error
        Error message (for failure)
    run_url
        URL to view the run details
    webhook_url
        Override webhook URL (defaults to NOTIFICATION_WEBHOOK env var)
    """
    # Use provided webhook or fall back to config
    url = webhook_url
    if not url:
        settings = get_settings()
        url = getattr(settings, "notification_webhook", None)

    if not url:
        logger.debug("No notification webhook configured, skipping notification")
        return

    # Build message
    if status == "success":
        emoji = "✅"
        status_text = "completed successfully"
        details = summary[:500] if summary else "No summary available"
    elif status == "failed":
        emoji = "❌"
        status_text = "failed"
        details = error[:500] if error else "Unknown error"
    else:
        emoji = "ℹ️"
        status_text = status
        details = summary or error or ""

    content_parts = [f"{emoji} **Run {run_id}** {status_text}"]

    if details:
        # Truncate and format details
        if len(details) > 300:
            details = details[:297] + "..."
        content_parts.append(f"```\n{details}\n```")

    if run_url:
        content_parts.append(f"[View details]({run_url})")

    content = "\n".join(content_parts)

    # Fire-and-forget
    try:
        asyncio.create_task(_post_discord(url, content))
        logger.info(f"Queued completion notification for run {run_id}")
    except RuntimeError:
        # Fallback for sync contexts where no event loop is running
        import threading

        threading.Thread(
            target=lambda: httpx.post(url, json={"content": content}),
            daemon=True,
        ).start()
        logger.info(f"Queued completion notification for run {run_id} (sync fallback)")
