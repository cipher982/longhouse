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
            claude_config_dir = Path(config_dir).expanduser()
        else:
            claude_config_dir = Path.home() / ".claude"

    return claude_config_dir / "longhouse-device-token"


def _get_legacy_token_path(claude_config_dir: Path | None = None) -> Path:
    """Get the path to the legacy device token file (zerg-device-token).

    Used for migration from old installations.
    """
    if claude_config_dir is None:
        config_dir = os.getenv("CLAUDE_CONFIG_DIR")
        if config_dir:
            claude_config_dir = Path(config_dir).expanduser()
        else:
            claude_config_dir = Path.home() / ".claude"

    return claude_config_dir / "zerg-device-token"


def load_token(claude_config_dir: Path | None = None) -> str | None:
    """Load the device token from local storage.

    Checks new path first, falls back to legacy path for migration.

    Args:
        claude_config_dir: Optional override for Claude config directory.

    Returns:
        The token string if it exists, None otherwise.
    """
    token_path = get_token_path(claude_config_dir)

    # Try new path first
    if token_path.exists():
        try:
            token = token_path.read_text().strip()
            if token:
                return token
        except (OSError, IOError):
            pass

    # Fall back to legacy path for existing installations
    legacy_path = _get_legacy_token_path(claude_config_dir)
    if legacy_path.exists():
        try:
            token = legacy_path.read_text().strip()
            if token:
                # Migrate to new path
                save_token(token, claude_config_dir)
                return token
        except (OSError, IOError):
            pass

    return None


def save_token(token: str, claude_config_dir: Path | None = None) -> None:
    """Save a device token to local storage.

    Creates the config directory if it doesn't exist.
    Uses secure file creation to avoid permission race conditions.

    Args:
        token: The token to save.
        claude_config_dir: Optional override for Claude config directory.

    Raises:
        OSError: If unable to write the token file.
    """
    import sys
    import tempfile

    token_path = get_token_path(claude_config_dir)

    # Ensure parent directory exists
    token_path.parent.mkdir(parents=True, exist_ok=True)

    content = token.strip() + "\n"

    if sys.platform == "win32":
        # Windows: simple write, chmod not fully supported
        token_path.write_text(content)
    else:
        # Unix: atomic write with secure permissions
        # Write to temp file then rename for atomicity
        fd, tmp_path = tempfile.mkstemp(
            dir=token_path.parent,
            prefix=".token-",
            suffix=".tmp",
        )
        try:
            os.write(fd, content.encode())
            os.fchmod(fd, 0o600)
            os.close(fd)
            os.rename(tmp_path, token_path)
        except Exception:
            os.close(fd)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


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
    """Load the configured Longhouse API URL from local storage.

    Checks new path first, falls back to legacy path for migration.

    Args:
        claude_config_dir: Optional override for Claude config directory.

    Returns:
        The URL string if configured, None otherwise.
    """
    url_path = _get_url_path(claude_config_dir)

    # Try new path first
    if url_path.exists():
        try:
            url = url_path.read_text().strip()
            if url:
                return url
        except (OSError, IOError):
            pass

    # Fall back to legacy path for existing installations
    legacy_path = _get_legacy_url_path(claude_config_dir)
    if legacy_path.exists():
        try:
            url = legacy_path.read_text().strip()
            if url:
                # Migrate to new path
                save_zerg_url(url, claude_config_dir)
                return url
        except (OSError, IOError):
            pass

    return None


def save_zerg_url(url: str, claude_config_dir: Path | None = None) -> None:
    """Save the Longhouse API URL to local storage.

    Uses secure file creation to avoid permission race conditions.

    Args:
        url: The URL to save.
        claude_config_dir: Optional override for Claude config directory.
    """
    import sys
    import tempfile

    url_path = _get_url_path(claude_config_dir)

    # Ensure parent directory exists
    url_path.parent.mkdir(parents=True, exist_ok=True)

    content = url.strip() + "\n"

    if sys.platform == "win32":
        # Windows: simple write, chmod not fully supported
        url_path.write_text(content)
    else:
        # Unix: atomic write with secure permissions
        fd, tmp_path = tempfile.mkstemp(
            dir=url_path.parent,
            prefix=".url-",
            suffix=".tmp",
        )
        try:
            os.write(fd, content.encode())
            os.fchmod(fd, 0o600)
            os.close(fd)
            os.rename(tmp_path, url_path)
        except Exception:
            os.close(fd)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


def _get_url_path(claude_config_dir: Path | None = None) -> Path:
    """Get the path to the Longhouse URL file."""
    if claude_config_dir is None:
        config_dir = os.getenv("CLAUDE_CONFIG_DIR")
        if config_dir:
            claude_config_dir = Path(config_dir).expanduser()
        else:
            claude_config_dir = Path.home() / ".claude"

    return claude_config_dir / "longhouse-url"


def _get_legacy_url_path(claude_config_dir: Path | None = None) -> Path:
    """Get the path to the legacy URL file (zerg-url).

    Used for migration from old installations.
    """
    if claude_config_dir is None:
        config_dir = os.getenv("CLAUDE_CONFIG_DIR")
        if config_dir:
            claude_config_dir = Path(config_dir).expanduser()
        else:
            claude_config_dir = Path.home() / ".claude"

    return claude_config_dir / "zerg-url"
