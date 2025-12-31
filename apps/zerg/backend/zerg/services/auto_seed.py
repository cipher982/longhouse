"""Auto-seeding service for development and production environments.

This service automatically seeds user context, credentials, and runners on startup.
All seeding is idempotent - safe to run multiple times.

Seeding sources (checked in order):
1. scripts/*.local.json (dev, git-ignored)
2. ~/.config/zerg/*.json (prod/personal)
"""

import json
import logging
import os
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

RUNNERS_PATHS = [
    Path("/app/scripts/runners.local.json"),  # Docker dev
    Path(__file__).parent.parent.parent / "scripts" / "runners.local.json",  # Local dev
    Path.home() / ".config" / "zerg" / "runners.json",  # Prod/personal
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


def _seed_runners() -> bool:
    """Seed runners from local config file.

    This allows dev environments to have pre-configured runners with known
    secrets, avoiding the need to re-register runners after database resets.

    The config file contains plaintext secrets that get hashed before storage.
    The runner daemon should use the same plaintext secret to connect.

    Returns:
        True if seeding succeeded or was skipped (idempotent), False on error.
    """
    from zerg.crud import runner_crud

    current_env = (os.getenv("ENVIRONMENT") or "").strip().lower()
    if current_env == "production":
        # Runner seeding is meant for dev DX; avoid silently creating runners in production.
        logger.debug("Skipping runners auto-seed in production environment")
        return True

    config_path = _find_config_file(RUNNERS_PATHS)
    if not config_path:
        logger.debug("No runners config found - skipping seed")
        return True

    try:
        with open(config_path) as f:
            config = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to load runners from {config_path}: {e}")
        return False

    runners_config = config.get("runners", [])
    if not runners_config:
        logger.debug("No runners defined in config - skipping")
        return True

    db = default_session_factory()
    try:
        # Find first user
        result = db.execute(select(User).limit(1))
        user = result.scalar_one_or_none()

        if not user:
            logger.debug("No users in database yet - skipping runners seed")
            return True

        seeded_count = 0
        skipped_count = 0

        for runner_config in runners_config:
            name = runner_config.get("name")
            secret = runner_config.get("secret")

            if not name or not secret:
                logger.warning(f"Runner config missing name or secret: {runner_config}")
                continue

            # Check if runner already exists (idempotent)
            existing = runner_crud.get_runner_by_name(db, user.id, name)
            if existing:
                logger.debug(f"Runner '{name}' already exists - skipping")
                skipped_count += 1
                continue

            # Create the runner with the known secret
            runner_crud.create_runner(
                db=db,
                owner_id=user.id,
                name=name,
                auth_secret=secret,
                labels=runner_config.get("labels"),
                capabilities=runner_config.get("capabilities", ["exec.readonly"]),
            )
            seeded_count += 1
            logger.info(f"Seeded runner '{name}' for {user.email}")

        if seeded_count > 0:
            logger.info(f"Seeded {seeded_count} runners ({skipped_count} already existed)")

        return True

    except Exception as e:
        logger.error(f"Failed to seed runners: {e}")
        db.rollback()
        return False
    finally:
        db.close()


def _seed_server_knowledge() -> bool:
    """Seed user's server info into the knowledge base for searchability.

    This makes knowledge_search("What servers do I have?") work by indexing
    the server info from user.context into a KnowledgeDocument.

    Returns:
        True if seeding succeeded or was skipped (idempotent), False on error.
    """
    from zerg.crud import knowledge_crud
    from zerg.models.models import KnowledgeSource

    db = default_session_factory()
    try:
        # Find first user with context
        result = db.execute(select(User).limit(1))
        user = result.scalar_one_or_none()

        if not user:
            logger.debug("No users in database yet - skipping server knowledge seed")
            return True

        # Check if user has servers in context
        context = user.context or {}
        servers = context.get("servers", [])
        if not servers:
            logger.debug("No servers in user context - skipping knowledge seed")
            return True

        # Check if we already have this knowledge source (idempotent)
        existing_source = db.query(KnowledgeSource).filter_by(owner_id=user.id, name="User Context - Servers").first()

        if existing_source:
            # Source exists, but update the document content in case servers changed
            pass
        else:
            # Create the knowledge source
            existing_source = knowledge_crud.create_knowledge_source(
                db,
                owner_id=user.id,
                name="User Context - Servers",
                source_type="user_context",
                config={"auto_seeded": True},
            )
            logger.info(f"Created knowledge source 'User Context - Servers' for {user.email}")

        # Format servers as searchable markdown
        lines = ["# My Servers\n"]
        for srv in servers:
            name = srv.get("name", "Unknown")
            ip = srv.get("ip", "")
            purpose = srv.get("purpose", "")
            ssh_user = srv.get("ssh_user", "")

            lines.append(f"## {name}")
            if ip:
                lines.append(f"- **IP Address:** {ip}")
            if purpose:
                lines.append(f"- **Purpose:** {purpose}")
            if ssh_user:
                lines.append(f"- **SSH User:** {ssh_user}")
            lines.append("")  # Blank line between servers

        content = "\n".join(lines)

        # Upsert the document (creates or updates)
        knowledge_crud.upsert_knowledge_document(
            db,
            source_id=existing_source.id,
            owner_id=user.id,
            path="user_context/servers.md",
            content_text=content,
            title="My Servers",
        )
        logger.info(f"Seeded {len(servers)} servers into knowledge base for {user.email}")
        return True

    except Exception as e:
        logger.error(f"Failed to seed server knowledge: {e}")
        db.rollback()
        return False
    finally:
        db.close()


def run_auto_seed() -> dict:
    """Run all auto-seeding tasks.

    Called during FastAPI startup. All seeding is idempotent.

    Returns:
        Dict with seeding results for logging.
    """
    # In dev mode (AUTH_DISABLED=1), many subsystems (runners, user-context, credentials)
    # assume at least one deterministic "dev@local" user exists. Most request paths
    # create it lazily via the auth layer, but runner websockets can connect before
    # any HTTP request occurs, causing noisy reconnect loops in logs.
    #
    # Creating the dev user here makes startup behavior deterministic and reduces log spam.
    try:
        from zerg.config import get_settings

        settings = get_settings()
        node_env = (os.getenv("NODE_ENV") or "").strip().lower()
        # Unit tests use NODE_ENV=test but don't always set TESTING=1; avoid mutating
        # the database state during tests.
        if settings.auth_disabled and not settings.testing and node_env != "test":
            from zerg import crud

            db = default_session_factory()
            try:
                result = db.execute(select(User).limit(1))
                user = result.scalar_one_or_none()
                if not user:
                    desired_role = "ADMIN" if settings.dev_admin else "USER"
                    existing = crud.get_user_by_email(db, "dev@local")
                    if not existing:
                        crud.create_user(
                            db,
                            email="dev@local",
                            provider="dev",
                            provider_user_id="dev-user-1",
                            role=desired_role,
                        )
            finally:
                db.close()
    except Exception:
        # Never block app startup on auto-seed user creation.
        pass

    results = {
        "user_context": "skipped",
        "credentials": "skipped",
        "runners": "skipped",
        "server_knowledge": "skipped",
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

    # Seed runners (dev infrastructure connectors)
    if _seed_runners():
        config_path = _find_config_file(RUNNERS_PATHS)
        if config_path:
            results["runners"] = f"ok ({config_path.name})"
    else:
        results["runners"] = "failed"

    # Seed server info into knowledge base (makes knowledge_search work for servers)
    # This runs AFTER user_context seed, so servers are available
    if _seed_server_knowledge():
        results["server_knowledge"] = "ok"
    else:
        results["server_knowledge"] = "failed"

    return results
