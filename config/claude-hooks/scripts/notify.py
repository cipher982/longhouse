#!/usr/bin/env python3
"""Notification hook for Claude Code workers.

Sends notifications via Discord/Slack webhooks when workers need attention.
Reads hook input from stdin (JSON with session_id, message, etc).

Environment variables:
  DISCORD_WEBHOOK_URL: Discord webhook for notifications
  SLACK_WEBHOOK_URL: Slack webhook for notifications
  ZERG_NOTIFY_ENABLED: Set to "0" to disable notifications (default: enabled)
"""

import json
import os
import sys
import urllib.request
from datetime import datetime

# Allowed webhook URL prefixes (security: prevent SSRF)
ALLOWED_DISCORD_PREFIXES = ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")
ALLOWED_SLACK_PREFIXES = ("https://hooks.slack.com/",)


def validate_webhook_url(url: str, allowed_prefixes: tuple) -> bool:
    """Validate webhook URL starts with allowed prefix (SSRF prevention)."""
    if not url:
        return False
    return any(url.startswith(prefix) for prefix in allowed_prefixes)


def send_discord(webhook_url: str, message: str, title: str = "Zerg Worker") -> bool:
    """Send notification to Discord webhook."""
    payload = {
        "embeds": [
            {
                "title": title,
                "description": message,
                "color": 5814783,  # Blue
                "timestamp": datetime.utcnow().isoformat(),
            }
        ]
    }

    try:
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 204
    except Exception as e:
        print(f"Discord notification failed: {e}", file=sys.stderr)
        return False


def send_slack(webhook_url: str, message: str, title: str = "Zerg Worker") -> bool:
    """Send notification to Slack webhook."""
    payload = {
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": title},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": message},
            },
        ]
    }

    try:
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"Slack notification failed: {e}", file=sys.stderr)
        return False


def main():
    # Check if notifications are disabled
    if os.environ.get("ZERG_NOTIFY_ENABLED", "1") == "0":
        sys.exit(0)

    # Read hook input from stdin
    try:
        hook_input = json.load(sys.stdin)
    except json.JSONDecodeError:
        hook_input = {}

    # Extract notification details
    session_id = hook_input.get("session_id", "unknown")
    notification_type = hook_input.get("type", "notification")
    message = hook_input.get("message", "Worker needs attention")

    # Build notification message
    notification_msg = f"**Session:** `{session_id[:8]}...`\n**Type:** {notification_type}\n\n{message}"

    # Get webhook URLs
    discord_url = os.environ.get("DISCORD_WEBHOOK_URL")
    slack_url = os.environ.get("SLACK_WEBHOOK_URL")

    sent = False

    if discord_url:
        if validate_webhook_url(discord_url, ALLOWED_DISCORD_PREFIXES):
            sent = send_discord(discord_url, notification_msg) or sent
        else:
            print(f"Invalid Discord webhook URL (must start with {ALLOWED_DISCORD_PREFIXES})", file=sys.stderr)

    if slack_url:
        if validate_webhook_url(slack_url, ALLOWED_SLACK_PREFIXES):
            sent = send_slack(slack_url, notification_msg) or sent
        else:
            print(f"Invalid Slack webhook URL (must start with {ALLOWED_SLACK_PREFIXES})", file=sys.stderr)

    if not discord_url and not slack_url:
        # No webhooks configured - log to stderr for visibility
        print(f"[NOTIFY] {notification_msg}", file=sys.stderr)

    # Exit 0 to not block Claude
    sys.exit(0)


if __name__ == "__main__":
    main()
