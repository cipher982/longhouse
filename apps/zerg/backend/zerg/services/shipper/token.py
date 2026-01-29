"""Token storage for device authentication.

Handles local storage of device tokens for the shipper CLI.
Tokens are stored in the Claude config directory alongside other
shipper state files.
"""

from __future__ import annotations

import os
from pathlib import Path


def get_token_path(claude_config_dir: Path | None = None) -> Path:
    """Get the path to the device token file.

    Respects CLAUDE_CONFIG_DIR environment variable if set.

    Args:
        claude_config_dir: Optional override for Claude config directory.
                          If None, uses CLAUDE_CONFIG_DIR env var or ~/.claude

    Returns:
        Path to the token file (may not exist)
    """
    if claude_config_dir is None:
        config_dir = os.getenv("CLAUDE_CONFIG_DIR")
        if config_dir:
            claude_config_dir = Path(config_dir)
        else:
            claude_config_dir = Path.home() / ".claude"

    return claude_config_dir / "zerg-device-token"


def load_token(claude_config_dir: Path | None = None) -> str | None:
    """Load the device token from local storage.

    Args:
        claude_config_dir: Optional override for Claude config directory.

    Returns:
        The token string if it exists, None otherwise.
    """
    token_path = get_token_path(claude_config_dir)

    if not token_path.exists():
        return None

    try:
        token = token_path.read_text().strip()
        return token if token else None
    except (OSError, IOError):
        return None


def save_token(token: str, claude_config_dir: Path | None = None) -> None:
    """Save a device token to local storage.

    Creates the config directory if it doesn't exist.

    Args:
        token: The token to save.
        claude_config_dir: Optional override for Claude config directory.

    Raises:
        OSError: If unable to write the token file.
    """
    token_path = get_token_path(claude_config_dir)

    # Ensure parent directory exists
    token_path.parent.mkdir(parents=True, exist_ok=True)

    # Write token with restricted permissions (owner read/write only)
    token_path.write_text(token.strip() + "\n")

    # Set file permissions to 600 (owner read/write only)
    try:
        token_path.chmod(0o600)
    except OSError:
        # Windows doesn't support chmod the same way, ignore
        pass


def clear_token(claude_config_dir: Path | None = None) -> bool:
    """Remove the device token from local storage.

    Args:
        claude_config_dir: Optional override for Claude config directory.

    Returns:
        True if a token was removed, False if no token existed.
    """
    token_path = get_token_path(claude_config_dir)

    if not token_path.exists():
        return False

    try:
        token_path.unlink()
        return True
    except (OSError, IOError):
        return False


def clear_zerg_url(claude_config_dir: Path | None = None) -> bool:
    """Remove the stored Zerg API URL from local storage.

    Args:
        claude_config_dir: Optional override for Claude config directory.

    Returns:
        True if the URL was removed, False if no URL existed.
    """
    url_path = _get_url_path(claude_config_dir)

    if not url_path.exists():
        return False

    try:
        url_path.unlink()
        return True
    except (OSError, IOError):
        return False


def get_zerg_url(claude_config_dir: Path | None = None) -> str | None:
    """Load the configured Zerg API URL from local storage.

    Args:
        claude_config_dir: Optional override for Claude config directory.

    Returns:
        The URL string if configured, None otherwise.
    """
    url_path = _get_url_path(claude_config_dir)

    if not url_path.exists():
        return None

    try:
        url = url_path.read_text().strip()
        return url if url else None
    except (OSError, IOError):
        return None


def save_zerg_url(url: str, claude_config_dir: Path | None = None) -> None:
    """Save the Zerg API URL to local storage.

    Args:
        url: The URL to save.
        claude_config_dir: Optional override for Claude config directory.
    """
    url_path = _get_url_path(claude_config_dir)

    # Ensure parent directory exists
    url_path.parent.mkdir(parents=True, exist_ok=True)

    url_path.write_text(url.strip() + "\n")


def _get_url_path(claude_config_dir: Path | None = None) -> Path:
    """Get the path to the Zerg URL file."""
    if claude_config_dir is None:
        config_dir = os.getenv("CLAUDE_CONFIG_DIR")
        if config_dir:
            claude_config_dir = Path(config_dir)
        else:
            claude_config_dir = Path.home() / ".claude"

    return claude_config_dir / "zerg-url"
