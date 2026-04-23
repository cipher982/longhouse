"""Machine target/auth storage for Longhouse local installs."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from zerg.services.longhouse_paths import get_machine_token_path
from zerg.services.machine_state import clear_machine_runtime_url
from zerg.services.machine_state import load_machine_state


def get_token_path(config_dir: Path | None = None) -> Path:
    """Get the path to the device token file."""
    return get_machine_token_path(config_dir)


def load_token(config_dir: Path | None = None) -> str | None:
    """Load the device token from local storage.

    Args:
        config_dir: Optional Longhouse home or provider-config override.

    Returns:
        The token string if it exists, None otherwise.
    """
    token_path = get_token_path(config_dir)

    if token_path.exists():
        try:
            token = token_path.read_text().strip()
            if token:
                return token
        except (OSError, IOError):
            pass

    return None


def save_token(token: str, config_dir: Path | None = None) -> None:
    """Save a device token to local storage.

    Creates the Longhouse machine config directory if it doesn't exist.
    Uses secure file creation to avoid permission race conditions.

    Args:
        token: The token to save.
        config_dir: Optional Longhouse home or provider-config override.

    Raises:
        OSError: If unable to write the token file.
    """
    import sys
    import tempfile

    token_path = get_token_path(config_dir)

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


def clear_token(config_dir: Path | None = None) -> bool:
    """Remove the device token from local storage.

    Args:
        config_dir: Optional Longhouse home or provider-config override.

    Returns:
        True if a token was removed, False if no token existed.
    """
    token_path = get_token_path(config_dir)

    if not token_path.exists():
        return False

    try:
        token_path.unlink()
        return True
    except (OSError, IOError):
        return False


def clear_zerg_url(config_dir: Path | None = None) -> bool:
    """Remove the stored Zerg API URL from local storage.

    Args:
        config_dir: Optional Longhouse home or provider-config override.

    Returns:
        True if the URL was removed, False if no URL existed.
    """
    try:
        return clear_machine_runtime_url(config_dir, written_by="shipper-clear-url")
    except RuntimeError:
        return False


def get_zerg_url(config_dir: Path | None = None) -> str | None:
    """Load the configured Longhouse API URL from local storage.

    Args:
        config_dir: Optional Longhouse home or provider-config override.

    Returns:
        The URL string if configured, None otherwise.
    """
    state = load_machine_state(config_dir)
    return state.runtime_url if state else None


def normalize_zerg_url(url: object | None) -> str | None:
    """Return a valid Longhouse URL or None.

    This guards against poisoned config like Typer OptionInfo objects being
    stringified into the persisted url file.
    """
    if not isinstance(url, str):
        return None

    normalized = url.strip()
    if not normalized:
        return None
    if "typer.models.OptionInfo" in normalized or "<" in normalized or ">" in normalized:
        return None

    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.hostname is None:
        return None

    return normalized


def save_zerg_url(url: str, config_dir: Path | None = None) -> None:
    """Save the Longhouse API URL to canonical machine state."""
    normalized_url = normalize_zerg_url(url)
    if normalized_url is None:
        raise ValueError(f"Invalid Longhouse URL: {url!r}")

    # Route durable machine config changes through the safe apply seam so
    # installed launch artifacts stay in sync with canonical state.
    from zerg.services.local_runtime_installer import apply_machine_state_update

    apply_machine_state_update(
        claude_dir=None,
        base_dir=config_dir,
        written_by="shipper-save-url",
        runtime_url=normalized_url,
    )


def sanitize_machine_name(name: str) -> str:
    """Sanitize a machine name to be safe for shell args and XML.

    - Strips leading/trailing whitespace
    - Replaces whitespace runs with hyphens (safe for systemd ExecStart)
    - Strips XML-significant characters (& < >) to avoid breaking plist
    - Collapses multiple hyphens
    - Truncates to 64 chars
    """
    import re

    name = name.strip()
    name = re.sub(r"\s+", "-", name)
    name = re.sub(r"[&<>\"']", "", name)
    name = re.sub(r"-{2,}", "-", name)
    name = name.strip("-")
    return name[:64] or "unknown"


def load_machine_name(config_dir: Path | None = None) -> str | None:
    """Load the configured Longhouse machine label."""
    state = load_machine_state(config_dir)
    return state.machine_name if state else None


def save_machine_name(name: str, config_dir: Path | None = None) -> None:
    """Save the machine name label through the canonical machine-state seam."""
    normalized_name = sanitize_machine_name(name)

    from zerg.services.local_runtime_installer import apply_machine_state_update

    apply_machine_state_update(
        claude_dir=None,
        base_dir=config_dir,
        written_by="shipper-save-machine-name",
        machine_name=normalized_name,
    )
