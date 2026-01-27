"""Tools for sending and reading iMessages on macOS hosts.

These helpers lean on the local Messages app and are intentionally scoped for
power users running the backend on a Mac with the Messages app signed in.

iMessage can be enabled via Fiche Settings when running on a macOS host.
"""

from __future__ import annotations

import logging
import platform
import plistlib
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from langchain_core.tools import StructuredTool

from zerg.connectors.context import get_credential_resolver
from zerg.connectors.registry import ConnectorType
from zerg.tools.error_envelope import ErrorType
from zerg.tools.error_envelope import tool_error
from zerg.tools.error_envelope import tool_success

logger = logging.getLogger(__name__)

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
IMESSAGE_DB_PATH = Path.home() / "Library" / "Messages" / "chat.db"
MAX_MESSAGE_LENGTH = 4000
MAX_QUERY_LIMIT = 200

_IMESSAGE_SEND_SCRIPT = """on run argv
  if (count of argv) < 2 then
    error "Recipient and message are required"
  end if

  set targetAddress to item 1 of argv
  set textMessage to item 2 of argv
  set chatGuid to ""
  if (count of argv) >= 3 then
    set chatGuid to item 3 of argv
  end if

  tell application "Messages"
    if chatGuid is not "" then
      try
        set targetChat to first chat whose id is chatGuid
      on error
        try
          set targetChat to first chat whose guid is chatGuid
        on error
          error "Unable to locate chat with GUID " & chatGuid
        end try
      end try
      send textMessage to targetChat
    else
      set targetService to first service whose service type = iMessage
      set targetBuddy to buddy targetAddress of targetService
      send textMessage to targetBuddy
    end if
  end tell
end run
"""


def _require_macos() -> Optional[Dict[str, Any]]:
    """Return an error payload if the host is not macOS."""
    if platform.system() != "Darwin":
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message="iMessage tools require macOS with the Messages app installed.",
            connector="imessage",
        )
    return None


def _convert_apple_timestamp(raw_value: Optional[int]) -> Optional[str]:
    """Convert the macOS absolute time (nanoseconds since 2001) into ISO 8601."""
    if raw_value is None:
        return None

    try:
        seconds = raw_value / 1_000_000_000
        dt = APPLE_EPOCH + timedelta(seconds=seconds)
        return dt.astimezone().isoformat()
    except Exception:  # pragma: no cover - defensive
        logger.debug("Unable to convert timestamp value %s", raw_value)
        return None


def _to_apple_timestamp(dt: datetime) -> int:
    """Convert a datetime to the macOS absolute time representation."""
    delta = dt.astimezone(timezone.utc) - APPLE_EPOCH
    return int(delta.total_seconds() * 1_000_000_000)


def _decode_attributed_body(value: Optional[bytes]) -> Optional[str]:
    """Attempt to decode the attributedBody blob stored by Messages."""
    if not value:
        return None

    try:
        payload = plistlib.loads(value)
        if isinstance(payload, dict):
            for candidate in ("NS.string", "NSString", "string"):
                text = payload.get(candidate)
                if isinstance(text, str) and text.strip():
                    return text
        decoded = value.decode("utf-8", errors="ignore").strip()
        return decoded or None
    except Exception:
        return None


def send_imessage(
    recipient: str,
    message: str,
    chat_guid: Optional[str] = None,
    timeout_seconds: float = 15.0,
) -> Dict[str, Any]:
    """Send an iMessage (or SMS fallback) using the local Messages app.

    iMessage can be enabled via Fiche Settings when running on a macOS host.
    """
    # Check if iMessage is configured for this fiche
    resolver = get_credential_resolver()
    if resolver:
        creds = resolver.get(ConnectorType.IMESSAGE)
        if not creds or not creds.get("enabled"):
            # iMessage not configured - this is OK for iMessage since it's host-dependent
            pass  # Continue with execution, it will fail naturally if not available

    env_error = _require_macos()
    if env_error:
        return env_error

    if not recipient or not recipient.strip():
        return tool_error(
            error_type=ErrorType.VALIDATION_ERROR,
            user_message="Recipient (phone or email) is required.",
            connector="imessage",
        )

    if not message or not message.strip():
        return tool_error(error_type=ErrorType.VALIDATION_ERROR, user_message="Message cannot be empty.", connector="imessage")

    if len(message) > MAX_MESSAGE_LENGTH:
        return tool_error(
            error_type=ErrorType.VALIDATION_ERROR,
            user_message=f"Message too long ({len(message)} chars). Max is {MAX_MESSAGE_LENGTH}.",
            connector="imessage",
        )

    if shutil.which("osascript") is None:
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message="osascript command not found. Enable AppleScript on macOS.",
            connector="imessage",
        )

    normalized_recipient = recipient.strip()
    script_args = ["osascript", "-", normalized_recipient, message]
    if chat_guid:
        script_args.append(chat_guid.strip())

    try:
        completed = subprocess.run(
            script_args,
            input=_IMESSAGE_SEND_SCRIPT,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        logger.error("Timed out sending iMessage to %s", normalized_recipient)
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message=f"Timed out after {timeout_seconds} seconds.",
            connector="imessage",
        )
    except FileNotFoundError:
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message="osascript binary is unavailable. iMessage send requires macOS.",
            connector="imessage",
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Unexpected error running osascript")
        return tool_error(error_type=ErrorType.EXECUTION_ERROR, user_message=f"Unexpected error: {exc}", connector="imessage")

    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message=stderr or "Messages app rejected the request.",
            connector="imessage",
        )

    return tool_success(
        {
            "recipient": normalized_recipient,
            "chat_guid": chat_guid,
            "status": "sent",
            "stdout": (completed.stdout or "").strip() or None,
        }
    )


def list_imessage_messages(
    limit: int = 50,
    include_outgoing: bool = False,
    chat_identifier: Optional[str] = None,
    since_row_id: Optional[int] = None,
    since_minutes: Optional[int] = None,
) -> Dict[str, Any]:
    """Read recent iMessage transcripts from the local chat database.

    iMessage can be enabled via Fiche Settings when running on a macOS host.
    """
    # Check if iMessage is configured for this fiche
    resolver = get_credential_resolver()
    if resolver:
        creds = resolver.get(ConnectorType.IMESSAGE)
        if not creds or not creds.get("enabled"):
            # iMessage not configured - this is OK for iMessage since it's host-dependent
            pass  # Continue with execution, it will fail naturally if not available

    env_error = _require_macos()
    if env_error:
        return env_error

    if limit <= 0:
        return tool_error(error_type=ErrorType.VALIDATION_ERROR, user_message="limit must be greater than zero.", connector="imessage")
    limit = min(limit, MAX_QUERY_LIMIT)

    db_path = IMESSAGE_DB_PATH
    if not db_path.exists():
        return tool_error(
            error_type=ErrorType.EXECUTION_ERROR,
            user_message=f"Messages database not found at {db_path}. Grant Full Disk Access.",
            connector="imessage",
        )

    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            temp_path = Path(tmp.name)
        shutil.copy2(db_path, temp_path)

        conn = sqlite3.connect(f"file:{temp_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row

        conditions: List[str] = ["1=1"]
        params: List[Any] = []

        if not include_outgoing:
            conditions.append("m.is_from_me = 0")

        if chat_identifier:
            conditions.append("(c.chat_identifier = ? OR c.guid = ?)")
            params.extend([chat_identifier, chat_identifier])

        if since_row_id:
            conditions.append("m.ROWID > ?")
            params.append(since_row_id)

        if since_minutes:
            cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=since_minutes)
            conditions.append("m.date >= ?")
            params.append(_to_apple_timestamp(cutoff))

        query = f"""
            SELECT
                m.ROWID AS row_id,
                m.text,
                m.is_from_me,
                m.date,
                m.date_read,
                m.attributedBody AS attributed_body,
                m.service,
                c.chat_identifier,
                c.guid AS chat_guid,
                h.id AS handle
            FROM message m
            JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
            JOIN chat c ON c.ROWID = cmj.chat_id
            LEFT JOIN handle h ON h.ROWID = m.handle_id
            WHERE {" AND ".join(conditions)}
            ORDER BY m.ROWID DESC
            LIMIT ?
        """
        params.append(limit)

        cursor = conn.cursor()
        cursor.execute(query, params)
        rows = cursor.fetchall()

        messages: List[Dict[str, Any]] = []
        for row in rows:
            text = row["text"]
            if not text:
                text = _decode_attributed_body(row["attributed_body"])

            messages.append(
                {
                    "row_id": row["row_id"],
                    "is_from_me": bool(row["is_from_me"]),
                    "text": text,
                    "chat_identifier": row["chat_identifier"],
                    "chat_guid": row["chat_guid"],
                    "handle": row["handle"],
                    "service": row["service"],
                    "sent_at": _convert_apple_timestamp(row["date"]),
                    "read_at": _convert_apple_timestamp(row["date_read"]),
                }
            )

        latest_row_id = messages[0]["row_id"] if messages else since_row_id
        return tool_success(
            {
                "messages": messages,
                "limit": limit,
                "include_outgoing": include_outgoing,
                "latest_row_id": latest_row_id,
            }
        )

    except PermissionError:
        logger.exception("Missing permissions to copy %s", db_path)
        return tool_error(
            error_type=ErrorType.PERMISSION_DENIED,
            user_message="Permission denied copying chat.db. Grant Full Disk Access.",
            connector="imessage",
        )
    except sqlite3.Error as exc:
        logger.exception("Failed to query iMessage database")
        return tool_error(error_type=ErrorType.EXECUTION_ERROR, user_message=f"SQLite error: {exc}", connector="imessage")
    finally:
        try:
            if "conn" in locals():
                conn.close()
        finally:
            if temp_path:
                try:
                    temp_path.unlink(missing_ok=True)
                except Exception:  # pragma: no cover - best effort cleanup
                    pass


TOOLS = [
    StructuredTool.from_function(
        func=send_imessage,
        name="send_imessage",
        description=(
            "Send an iMessage (or SMS fallback) via the local Messages app. "
            "Requires the backend to run on macOS with Messages signed in. "
            "Can be enabled via Fiche Settings."
        ),
    ),
    StructuredTool.from_function(
        func=list_imessage_messages,
        name="list_imessage_messages",
        description=(
            "Fetch recent iMessage transcripts from the local chat database. "
            "Supports filtering by chat and incremental polling via row_id. "
            "Can be enabled via Fiche Settings."
        ),
    ),
]
