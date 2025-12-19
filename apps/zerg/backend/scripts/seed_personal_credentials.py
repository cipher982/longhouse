#!/usr/bin/env python3
"""Seed personal tool credentials from a local config file.

This script populates the account_connector_credentials table with credentials
for personal integrations (Traccar, WHOOP, Obsidian) that enable Jarvis's
personal tools.

Usage:
    python scripts/seed_personal_credentials.py                    # Uses default path, first user
    python scripts/seed_personal_credentials.py --email me@x.com   # Specific user
    python scripts/seed_personal_credentials.py /path/to/creds.json
    python scripts/seed_personal_credentials.py --force            # Overwrite existing

The credentials file should be a JSON file with connector credentials.
See scripts/personal_credentials.example.json for the expected format.

SECURITY NOTE: The credentials file contains sensitive data. Use .local.json
suffix (git-ignored) or store in ~/.config/zerg/ for local development.
"""

import json
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select
from zerg.database import default_session_factory
from zerg.models.models import User, AccountConnectorCredential
from zerg.utils.crypto import encrypt


# Default locations to look for credentials file (in order)
DEFAULT_CREDS_PATHS = [
    Path(__file__).parent / "personal_credentials.local.json",  # scripts/personal_credentials.local.json
    Path.home() / ".config" / "zerg" / "personal_credentials.json",  # ~/.config/zerg/personal_credentials.json
]

# Connector types that this script handles (personal tools from Phase 4 v2.1)
PERSONAL_CONNECTORS = ["traccar", "whoop", "obsidian"]


def load_credentials(path: Path | None = None) -> dict:
    """Load credentials from JSON file.

    Args:
        path: Explicit path to credentials file, or None to search defaults

    Returns:
        Credentials dictionary keyed by connector type

    Raises:
        FileNotFoundError: If no credentials file found
    """
    if path:
        paths_to_try = [path]
    else:
        paths_to_try = DEFAULT_CREDS_PATHS

    for p in paths_to_try:
        if p.exists():
            print(f"Loading credentials from: {p}")
            with open(p) as f:
                return json.load(f)

    # No file found - provide helpful error
    raise FileNotFoundError(
        f"No personal credentials file found. Looked in:\n"
        f"  - {DEFAULT_CREDS_PATHS[0]}\n"
        f"  - {DEFAULT_CREDS_PATHS[1]}\n\n"
        f"Create a credentials file by copying the example:\n"
        f"  cp scripts/personal_credentials.example.json scripts/personal_credentials.local.json\n"
        f"Then edit it with your personal credentials."
    )


def find_user(db, email: str | None = None) -> User | None:
    """Find user by email or get first user."""
    if email:
        result = db.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()
    else:
        result = db.execute(select(User).limit(1))
        return result.scalar_one_or_none()


def seed_credential(db, user: User, connector_type: str, creds: dict, force: bool = False) -> bool:
    """Seed a single connector credential.

    Args:
        db: Database session
        user: User to seed credentials for
        connector_type: Type of connector (traccar, whoop, obsidian)
        creds: Credential dictionary to encrypt and store
        force: If True, overwrite existing credentials

    Returns:
        True if credential was created/updated, False if skipped
    """
    # Check for existing credential
    existing = db.query(AccountConnectorCredential).filter(
        AccountConnectorCredential.owner_id == user.id,
        AccountConnectorCredential.connector_type == connector_type,
    ).first()

    if existing and not force:
        print(f"  SKIP: {connector_type} (already configured, use --force to overwrite)")
        return False

    # Encrypt the credential payload
    encrypted_value = encrypt(json.dumps(creds))

    if existing:
        # Update existing
        existing.encrypted_value = encrypted_value
        existing.test_status = "untested"
        print(f"  UPDATE: {connector_type}")
    else:
        # Create new
        credential = AccountConnectorCredential(
            owner_id=user.id,
            connector_type=connector_type,
            encrypted_value=encrypted_value,
            display_name=f"Personal {connector_type.title()}",
            test_status="untested",
        )
        db.add(credential)
        print(f"  CREATE: {connector_type}")

    return True


def main():
    """Seed personal credentials from local config file."""
    # Parse arguments
    force = "--force" in sys.argv
    email = None
    creds_path = None

    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    # Check for --email flag
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--email" and i < len(sys.argv) - 1:
            email = sys.argv[i + 1]
            break

    # Remaining arg is path
    path_args = [a for a in args if a != email]
    if path_args:
        creds_path = Path(path_args[0])

    # Load credentials from file
    try:
        credentials = load_credentials(creds_path)
    except FileNotFoundError as e:
        # In auto-seed mode (no file), just skip silently
        print(f"SKIP: {e}")
        return 0  # Return success - no file is not an error for auto-seed

    # Connect to database
    db = default_session_factory()
    try:
        # Find user
        user = find_user(db, email)

        if not user:
            if email:
                print(f"SKIP: User with email '{email}' not found.")
            else:
                print("SKIP: No users found in database yet.")
            return 0  # Not an error - user may not exist yet

        print(f"Seeding credentials for user: {user.email} (ID: {user.id})")

        # Seed each connector type
        updated_count = 0
        for connector_type in PERSONAL_CONNECTORS:
            if connector_type in credentials:
                creds = credentials[connector_type]
                if seed_credential(db, user, connector_type, creds, force):
                    updated_count += 1

        if updated_count > 0:
            db.commit()
            print(f"\nSUCCESS: {updated_count} credential(s) seeded!")
        else:
            print("\nNo credentials were updated.")

        return 0

    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        db.rollback()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
