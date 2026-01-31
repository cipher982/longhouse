"""Shared database utilities.

This module contains low-level database helpers that need to be importable
without triggering circular dependencies. Specifically, these functions
are used by both config/__init__.py and database.py during module load.
"""

from sqlalchemy.engine.url import make_url


def is_sqlite_url(url: str) -> bool:
    """Check if a database URL is SQLite, handling quoted URLs.

    Uses SQLAlchemy's make_url() for proper parsing instead of string matching.
    This handles cases where the URL has surrounding quotes from .env files.

    Args:
        url: Database URL string (possibly with surrounding quotes)

    Returns:
        True if the URL is a SQLite database
    """
    url = (url or "").strip()
    if not url:
        return False

    # Strip surrounding quotes (common from .env files)
    if (url.startswith('"') and url.endswith('"')) or (url.startswith("'") and url.endswith("'")):
        url = url[1:-1].strip()

    if not url:
        return False

    try:
        parsed = make_url(url)
        return parsed.drivername.startswith("sqlite")
    except Exception:
        # Fallback to string matching if parsing fails
        return url.startswith("sqlite")
