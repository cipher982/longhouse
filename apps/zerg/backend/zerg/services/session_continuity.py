"""Session continuity service for cross-environment Claude Code session resumption.

This service enables seamless --resume of Claude Code sessions across environments:
- Laptop terminal -> Zerg commis
- Zerg commis -> Laptop terminal
- Zerg commis -> Zerg commis

Sessions are archived in Life Hub and can be fetched/shipped via its API.

Key insight: Claude Code path encoding is deterministic:
    encoded_cwd = re.sub(r'[^A-Za-z0-9-]', '-', absolute_path)
"""

from __future__ import annotations

import logging
import os
import platform
import re
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Life Hub API configuration
LIFE_HUB_URL = os.getenv("LIFE_HUB_URL", "https://data.drose.io")
LIFE_HUB_API_KEY = os.getenv("LIFE_HUB_API_KEY")

# Valid session ID pattern (alphanumeric, dashes, underscores only)
# Prevents path traversal attacks via malicious session IDs
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def get_claude_config_dir() -> Path:
    """Get the Claude config directory, respecting CLAUDE_CONFIG_DIR env var.

    Priority:
    1. CLAUDE_CONFIG_DIR environment variable
    2. ~/.claude (default)
    """
    config_dir = os.getenv("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir)
    return Path.home() / ".claude"


def validate_session_id(session_id: str) -> None:
    """Validate session ID to prevent path traversal attacks.

    Args:
        session_id: The session ID to validate

    Raises:
        ValueError: If session ID contains unsafe characters
    """
    if not session_id:
        raise ValueError("Session ID cannot be empty")
    if not SESSION_ID_PATTERN.match(session_id):
        raise ValueError(f"Invalid session ID format: {session_id}")
    if ".." in session_id or "/" in session_id or "\\" in session_id:
        raise ValueError(f"Session ID contains path traversal characters: {session_id}")


def encode_cwd_for_claude(absolute_path: str) -> str:
    """Encode a working directory path using Claude Code's algorithm.

    Claude Code stores sessions at ~/.claude/projects/{encoded_cwd}/{sessionId}.jsonl
    where encoded_cwd is the absolute path with non-alphanumeric chars replaced by dashes.

    Args:
        absolute_path: Absolute path to the working directory

    Returns:
        Encoded path string matching Claude Code's encoding
    """
    return re.sub(r"[^A-Za-z0-9-]", "-", absolute_path)


async def fetch_session_from_life_hub(session_id: str) -> tuple[bytes, str, str]:
    """Fetch a session from Life Hub for resumption.

    Args:
        session_id: Life Hub session UUID

    Returns:
        Tuple of (jsonl_bytes, cwd, provider_session_id)

    Raises:
        ValueError: If session not found or API error
        httpx.HTTPError: On network errors
    """
    if not LIFE_HUB_API_KEY:
        raise ValueError("LIFE_HUB_API_KEY not configured")

    url = f"{LIFE_HUB_URL}/query/fiches/sessions/{session_id}/export"

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(
            url,
            headers={"X-API-Key": LIFE_HUB_API_KEY},
        )

        if response.status_code == 404:
            raise ValueError(f"Session {session_id} not found in Life Hub")

        response.raise_for_status()

        # Extract metadata from headers
        cwd = response.headers.get("X-Session-CWD", "")
        provider_session_id = response.headers.get("X-Provider-Session-ID", "")

        # Validate provider_session_id to prevent path traversal
        if provider_session_id:
            validate_session_id(provider_session_id)

        return response.content, cwd, provider_session_id


async def prepare_session_for_resume(
    session_id: str,
    workspace_path: Path,
    claude_config_dir: Path | None = None,
) -> str:
    """Fetch session from Life Hub and prepare it for Claude Code --resume.

    Downloads the session JSONL and places it at the path Claude Code expects:
    {claude_config_dir}/projects/{encoded_cwd}/{provider_session_id}.jsonl

    Args:
        session_id: Life Hub session UUID to fetch
        workspace_path: The workspace directory where Claude Code will run
        claude_config_dir: Override for Claude config dir (default: from CLAUDE_CONFIG_DIR or ~/.claude)

    Returns:
        The provider_session_id to pass to --resume flag

    Raises:
        ValueError: If session not found or configuration error
    """
    # Fetch session from Life Hub
    jsonl_bytes, original_cwd, provider_session_id = await fetch_session_from_life_hub(session_id)

    if not provider_session_id:
        raise ValueError(f"Session {session_id} has no provider_session_id - cannot resume")

    # Validate provider_session_id to prevent path traversal (defense in depth)
    validate_session_id(provider_session_id)

    # Determine Claude config directory (respects CLAUDE_CONFIG_DIR env var)
    config_dir = claude_config_dir or get_claude_config_dir()

    # Use workspace path for the encoded_cwd (where the new session will run)
    # This allows resuming a session that started in a different directory
    encoded_cwd = encode_cwd_for_claude(str(workspace_path.absolute()))

    # Create the projects directory
    session_dir = config_dir / "projects" / encoded_cwd
    session_dir.mkdir(parents=True, exist_ok=True)

    # Write the session file
    session_file = session_dir / f"{provider_session_id}.jsonl"
    session_file.write_bytes(jsonl_bytes)

    logger.info(f"Prepared session {session_id} for resume at {session_file}")

    return provider_session_id


async def ship_session_to_life_hub(
    workspace_path: Path,
    commis_id: str,
    claude_config_dir: Path | None = None,
) -> str | None:
    """Ship a Claude Code session from workspace to Life Hub.

    Finds the most recent session file in the workspace's Claude config
    and ships it to Life Hub for future resumption.

    Args:
        workspace_path: The workspace directory where Claude Code ran
        commis_id: Commis ID for logging/tracking
        claude_config_dir: Override for Claude config dir (default: from CLAUDE_CONFIG_DIR or ~/.claude)

    Returns:
        The Life Hub session ID if shipped successfully, None otherwise
    """
    if not LIFE_HUB_API_KEY:
        logger.warning("LIFE_HUB_API_KEY not configured, skipping session ship")
        return None

    # Determine Claude config directory (respects CLAUDE_CONFIG_DIR env var)
    config_dir = claude_config_dir or get_claude_config_dir()

    # Find session file for this workspace
    encoded_cwd = encode_cwd_for_claude(str(workspace_path.absolute()))
    session_dir = config_dir / "projects" / encoded_cwd

    if not session_dir.exists():
        logger.debug(f"No Claude sessions found for workspace {workspace_path}")
        return None

    # Find most recent .jsonl file
    session_files = sorted(session_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)

    if not session_files:
        logger.debug(f"No session files found in {session_dir}")
        return None

    session_file = session_files[0]
    provider_session_id = session_file.stem

    logger.info(f"Shipping session {provider_session_id} for commis {commis_id}")

    # Read session content
    session_content = session_file.read_bytes()

    # Ship to Life Hub ingest endpoint
    # Note: The shipper service handles the full event ingestion format,
    # but for immediate shipping we use a simplified approach
    url = f"{LIFE_HUB_URL}/ingest/fiches/events"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Parse JSONL and extract events for ingestion
            import json

            events = []
            for line in session_content.decode("utf-8").splitlines():
                if line.strip():
                    try:
                        event = json.loads(line)
                        events.append({"raw_text": line, "raw_json": event})
                    except json.JSONDecodeError:
                        events.append({"raw_text": line})

            # Build device_id for Life Hub (required field)
            device_id = f"zerg-commis-{platform.node()}"

            payload = {
                "device_id": device_id,
                "provider": "claude",
                "source_path": str(session_file),
                "provider_session_id": provider_session_id,
                "cwd": str(workspace_path.absolute()),
                "events": events,
            }

            response = await client.post(
                url,
                headers={"X-API-Key": LIFE_HUB_API_KEY, "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()

            result = response.json()
            session_id = result.get("session_id")
            logger.info(f"Shipped session {provider_session_id} to Life Hub as {session_id}")
            return session_id

    except Exception as e:
        logger.warning(f"Failed to ship session {provider_session_id}: {e}")
        return None


__all__ = [
    "encode_cwd_for_claude",
    "fetch_session_from_life_hub",
    "prepare_session_for_resume",
    "ship_session_to_life_hub",
]
