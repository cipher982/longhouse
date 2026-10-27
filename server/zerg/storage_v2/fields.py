"""Field rules for the storage-v2 wire, shared by the ingest edge and catalogd.

The router validates an engine's envelope with these rules and catalogd
re-validates the RPC it receives with the same ones, so an envelope the edge
accepts can never be refused deeper in the host for a field the engine sent.
"""

from __future__ import annotations

import unicodedata
from datetime import UTC
from datetime import datetime

_SESSION_FIELDS = frozenset(
    {
        "environment",
        "project",
        "cwd",
        "git_repo",
        "git_branch",
        "started_at",
        "last_activity_at",
        "ended_at",
        "origin_kind",
        "hidden_from_default_timeline",
        "launch_actor",
        "launch_surface",
    }
)
# Each optional field lets an engine that predates it keep shipping; absent
# means what an older engine meant.
_OPTIONAL_SESSION_FIELDS = frozenset(
    {
        "provider_session_id",
        "provider_version",
        "is_subagent",
        "parent_provider_session_id",
        "parent_tool_call_id",
        "workflow_run_id",
    }
)
# What the provider reports about where it ran: paths and names as the
# filesystem and git spell them.
_SESSION_TEXT_FIELDS = (
    ("project", 255),
    ("cwd", 4_096),
    ("git_repo", 500),
    ("git_branch", 255),
)
# Tokens and ids that are matched against other rows.
_SESSION_IDENTITY_FIELDS = (
    ("origin_kind", 64),
    ("launch_actor", 32),
    ("launch_surface", 32),
    ("provider_session_id", 255),
    ("parent_provider_session_id", 255),
    ("parent_tool_call_id", 255),
    ("workflow_run_id", 255),
)


def bounded_text(value: object, field: str, maximum_bytes: int) -> str:
    """Text as a provider reported it: paths, branch names, transcript content."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{field} exceeds {maximum_bytes} UTF-8 bytes")
    return value


def canonical_text(value: object, field: str, maximum_bytes: int) -> str:
    """Identity text is hashed and compared byte for byte, so it must be NFC."""
    text = bounded_text(value, field, maximum_bytes)
    if unicodedata.normalize("NFC", text) != text:
        raise ValueError(f"{field} must already be NFC-normalized")
    return text


def utc_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def provider_version_or_none(value: object) -> str | None:
    """Keep a provider CLI version only when it is a short, non-empty string.

    The version is advisory evidence for the provider factory, never a reason
    to refuse an envelope, so anything unusable is dropped rather than raised.
    """
    if not isinstance(value, str):
        return None
    version = value.strip()
    if not version or len(version.encode("utf-8")) > 64:
        return None
    return version


def parse_session_facts(value: object) -> dict[str, object]:
    """Validate protocol-v2 session facts; timestamps come back as UTC RFC3339."""
    if not isinstance(value, dict) or not _SESSION_FIELDS <= value.keys() or value.keys() - _SESSION_FIELDS - _OPTIONAL_SESSION_FIELDS:
        raise ValueError("session fields do not match protocol v2")
    result = dict(value)
    result.setdefault("is_subagent", False)
    for field in _OPTIONAL_SESSION_FIELDS:
        result.setdefault(field, None)
    result["provider_version"] = provider_version_or_none(result["provider_version"])
    result["environment"] = canonical_text(result["environment"], "session.environment", 32)
    for field, maximum in _SESSION_TEXT_FIELDS:
        if result[field] is not None:
            result[field] = bounded_text(result[field], f"session.{field}", maximum)
    for field, maximum in _SESSION_IDENTITY_FIELDS:
        if result[field] is not None:
            result[field] = canonical_text(result[field], f"session.{field}", maximum)
    started_at = utc_timestamp(result["started_at"], "session.started_at")
    last_activity_at = utc_timestamp(result["last_activity_at"], "session.last_activity_at")
    ended_at = utc_timestamp(result["ended_at"], "session.ended_at") if result["ended_at"] is not None else None
    if last_activity_at < started_at:
        raise ValueError("session.last_activity_at cannot precede session.started_at")
    if ended_at is not None and ended_at < started_at:
        raise ValueError("session.ended_at cannot precede session.started_at")
    result["started_at"] = started_at.isoformat()
    result["last_activity_at"] = last_activity_at.isoformat()
    result["ended_at"] = ended_at.isoformat() if ended_at is not None else None
    for field in ("hidden_from_default_timeline", "is_subagent"):
        if type(result[field]) is not bool:
            raise ValueError(f"session.{field} must be a boolean")
    return result
