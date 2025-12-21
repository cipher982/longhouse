"""Auto-seeding service for development and production environments.

This service automatically seeds user context and credentials on startup.
All seeding is idempotent - safe to run multiple times.

Seeding sources (checked in order):
1. scripts/user_context.local.json (dev, git-ignored)
2. ~/.config/zerg/user_context.json (prod/personal)
"""

import json
import logging
from pathlib import Path

from sqlalchemy import select

from zerg.database import default_session_factory
from zerg.models.models import User

logger = logging.getLogger(__name__)

# Config file locations (checked in order)
USER_CONTEXT_PATHS = [
    Path("/app/scripts/user_context.local.json"),  # Docker dev
    Path(__file__).parent.parent.parent / "scripts" / "user_context.local.json",  # Local dev
    Path.home() / ".config" / "zerg" / "user_context.json",  # Prod/personal
]

CREDENTIALS_PATHS = [
    Path("/app/scripts/personal_credentials.local.json"),  # Docker dev
    Path(__file__).parent.parent.parent / "scripts" / "personal_credentials.local.json",  # Local dev
    Path.home() / ".config" / "zerg" / "personal_credentials.json",  # Prod/personal
]


def _find_config_file(paths: list[Path]) -> Path | None:
    """Find first existing config file from list of paths."""
    for path in paths:
        if path.exists():
            return path
    return None


def _seed_user_context() -> bool:
    """Seed user context from local config file.

    Returns:
        True if seeding succeeded or was skipped (idempotent), False on error.
    """
    config_path = _find_config_file(USER_CONTEXT_PATHS)
    if not config_path:
        logger.debug("No user context config found - skipping seed")
        return True

    try:
        with open(config_path) as f:
            context = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to load user context from {config_path}: {e}")
        return False

    db = default_session_factory()
    try:
        # Find first user
        result = db.execute(select(User).limit(1))
        user = result.scalar_one_or_none()

        if not user:
            logger.debug("No users in database yet - skipping context seed")
            return True

        # Idempotent: skip if already has meaningful context
        if user.context and user.context.get("display_name"):
            logger.debug(f"User {user.email} already has context - skipping")
            return True

        # Seed the context
        user.context = context
        db.commit()

        server_count = len(context.get("servers", []))
        logger.info(f"Seeded user context for {user.email}: {server_count} servers configured")
        return True

    except Exception as e:
        logger.error(f"Failed to seed user context: {e}")
        db.rollback()
        return False
    finally:
        db.close()


def _seed_personal_credentials() -> bool:
    """Seed personal tool credentials from local config file.

    Returns:
        True if seeding succeeded or was skipped (idempotent), False on error.
    """
    config_path = _find_config_file(CREDENTIALS_PATHS)
    if not config_path:
        logger.debug("No personal credentials config found - skipping seed")
        return True

    try:
        # Import the seeding function from the script
        # This reuses the existing logic which handles encryption, etc.
        import sys

        scripts_dir = config_path.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))

        from seed_personal_credentials import seed_credentials_for_user

        db = default_session_factory()
        try:
            result = db.execute(select(User).limit(1))
            user = result.scalar_one_or_none()

            if not user:
                logger.debug("No users in database yet - skipping credentials seed")
                return True

            # Load credentials
            with open(config_path) as f:
                creds = json.load(f)

            # Seed (idempotent - won't overwrite existing)
            seeded = seed_credentials_for_user(db, user.id, creds, force=False)
            if seeded:
                logger.info(f"Seeded personal credentials for {user.email}")
            else:
                logger.debug(f"Personal credentials already exist for {user.email}")
            return True

        except Exception as e:
            logger.warning(f"Failed to seed personal credentials: {e}")
            db.rollback()
            return False
        finally:
            db.close()

    except ImportError:
        # seed_personal_credentials script not available - skip silently
        logger.debug("Personal credentials seeder not available - skipping")
        return True
    except Exception as e:
        logger.warning(f"Failed to seed personal credentials: {e}")
        return False


def run_auto_seed() -> dict:
    """Run all auto-seeding tasks.

    Called during FastAPI startup. All seeding is idempotent.

    Returns:
        Dict with seeding results for logging.
    """
    results = {
        "user_context": "skipped",
        "credentials": "skipped",
    }

    # Seed user context (servers, integrations, preferences)
    if _seed_user_context():
        config_path = _find_config_file(USER_CONTEXT_PATHS)
        if config_path:
            results["user_context"] = f"ok ({config_path.name})"
    else:
        results["user_context"] = "failed"

    # Seed personal credentials (Traccar, WHOOP, etc.)
    if _seed_personal_credentials():
        config_path = _find_config_file(CREDENTIALS_PATHS)
        if config_path:
            results["credentials"] = f"ok ({config_path.name})"
    else:
        results["credentials"] = "failed"

    return results
