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
    return bool(getattr(get_settings(), "discord_enable_alerts", False))


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
    asyncio.create_task(_post_discord(url, content))

    _last_alert_key = key
    _last_alert_ts = now_ts


async def send_daily_digest(content: str) -> None:
    """Send a daily digest string to Discord (optional)."""
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

    content = f"@here 🎉 **New User Signup!** {user_email} just joined Zerg{count_info}"

    # Fire-and-forget
    asyncio.create_task(_post_discord(url, content))
