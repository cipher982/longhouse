#!/usr/bin/env python3
"""Seed user context from a local config file.

This script populates the user's context field with servers, integrations,
and preferences that will be injected into AI agent prompts.

Usage:
    python scripts/seed_user_context.py                    # Uses default path
    python scripts/seed_user_context.py /path/to/context.json

The context file should be a JSON file with the user context structure.
See scripts/user_context.example.json for the expected format.
"""

import json
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select
from zerg.database import SessionLocal
from zerg.models.models import User


# Default locations to look for context file (in order)
DEFAULT_CONTEXT_PATHS = [
    Path(__file__).parent / "user_context.local.json",  # scripts/user_context.local.json
    Path.home() / ".config" / "zerg" / "user_context.json",  # ~/.config/zerg/user_context.json
]


def load_context(path: Path | None = None) -> dict:
    """Load user context from JSON file.

    Args:
        path: Explicit path to context file, or None to search defaults

    Returns:
        User context dictionary

    Raises:
        FileNotFoundError: If no context file found
    """
    if path:
        paths_to_try = [path]
    else:
        paths_to_try = DEFAULT_CONTEXT_PATHS

    for p in paths_to_try:
        if p.exists():
            print(f"Loading context from: {p}")
            with open(p) as f:
                return json.load(f)

    # No file found - provide helpful error
    raise FileNotFoundError(
        f"No user context file found. Looked in:\n"
        f"  - {DEFAULT_CONTEXT_PATHS[0]}\n"
        f"  - {DEFAULT_CONTEXT_PATHS[1]}\n\n"
        f"Create a context file by copying the example:\n"
        f"  cp scripts/user_context.example.json scripts/user_context.local.json\n"
        f"Then edit it with your personal configuration."
    )


def main():
    """Seed user context from local config file.

    Use --force to overwrite existing context.
    Without --force, skips if user already has context (idempotent).
    """
    # Parse arguments
    force = "--force" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    context_path = Path(args[0]) if args else None

    # Load context from file
    try:
        context = load_context(context_path)
    except FileNotFoundError as e:
        # In auto-seed mode (no file), just skip silently
        print(f"SKIP: {e}")
        return 0  # Return success - no file is not an error for auto-seed

    # Connect to database and update user
    db = SessionLocal()
    try:
        # Find first user
        result = db.execute(select(User).limit(1))
        user = result.scalar_one_or_none()

        if not user:
            print("SKIP: No users found in database yet.")
            return 0  # Not an error - user may not exist yet

        # Check if user already has context (idempotent)
        if user.context and user.context.get("display_name") and not force:
            print(f"SKIP: User {user.email} already has context. Use --force to overwrite.")
            return 0

        print(f"Seeding context for user: {user.email} (ID: {user.id})")

        # Update context
        user.context = context
        db.commit()

        print("SUCCESS: User context updated!")
        print(f"  - Display name: {context.get('display_name', '(not set)')}")
        print(f"  - Servers: {len(context.get('servers', []))}")
        print(f"  - Integrations: {len(context.get('integrations', {}))}")

        return 0

    except Exception as e:
        print(f"ERROR: {e}")
        db.rollback()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
