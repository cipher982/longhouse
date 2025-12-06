"""
Migration script to promote agent-level credentials to account-level.

Usage:
    python scripts/migrate_agent_credentials.py [--dry-run]

Logic:
1. Iterate through all users.
2. For each user, group their agent credentials by connector type.
3. For each connector type:
    - If user already has account-level credential, skip (manual intervention needed if they want to change it).
    - Collect all agent credentials for this type.
    - If no agent credentials, skip.
    - Determine the "best" credential to promote:
        - The one used by the most agents?
        - Or the most recently updated? (PRD suggests most recently updated)
    - Create AccountConnectorCredential with this value.
    - For ALL agents that had this EXACT SAME encrypted value:
        - Delete their agent-level credential (they will now inherit).
    - For agents with DIFFERENT values:
        - Keep their agent-level credential (as an override).
4. Print summary of actions.
"""

import argparse
import logging
import sys
from collections import defaultdict
from typing import Any, Dict, List, NamedTuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

# Add backend to path so we can import app modules
sys.path.append(".")

from zerg.database import db_session
from zerg.models.models import AccountConnectorCredential, Agent, ConnectorCredential, User

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


class CredentialGroup(NamedTuple):
    encrypted_value: str
    updated_at: Any
    agent_ids: List[int]
    display_name: str | None
    metadata: Dict | None


def migrate_credentials(db: Session, dry_run: bool = False):
    logger.info(f"Starting migration (dry_run={dry_run})")

    # Get all users
    users = db.query(User).all()
    logger.info(f"Found {len(users)} users")

    total_promoted = 0
    total_deleted = 0
    total_kept_override = 0

    for user in users:
        logger.info(f"Processing user {user.id} ({user.email})")

        # Get all credentials for this user's agents
        # Join Agent to get owner_id
        agent_creds = (
            db.query(ConnectorCredential, Agent)
            .join(Agent, ConnectorCredential.agent_id == Agent.id)
            .filter(Agent.owner_id == user.id)
            .all()
        )

        if not agent_creds:
            continue

        # Group by connector_type
        by_type: Dict[str, List[tuple[ConnectorCredential, Agent]]] = defaultdict(list)
        for cred, agent in agent_creds:
            by_type[cred.connector_type].append((cred, agent))

        for conn_type, items in by_type.items():
            # Check if account credential already exists
            existing_account_cred = (
                db.query(AccountConnectorCredential)
                .filter(
                    AccountConnectorCredential.owner_id == user.id,
                    AccountConnectorCredential.connector_type == conn_type,
                )
                .first()
            )

            if existing_account_cred:
                logger.info(
                    f"  [SKIP] {conn_type}: Account credential already exists. "
                    f"Keeping {len(items)} agent overrides."
                )
                continue

            # Group by encrypted value to find duplicates
            # We want to promote the "best" one.
            # Strategy: Use the most recently updated credential as the source of truth.

            # Sort items by updated_at desc
            items.sort(key=lambda x: x[0].updated_at or x[0].created_at, reverse=True)

            best_cred, best_agent = items[0]

            logger.info(
                f"  [PROMOTE] {conn_type}: Promoting credential from Agent {best_agent.id} ({best_agent.name})"
            )

            if not dry_run:
                # Create account credential
                new_account_cred = AccountConnectorCredential(
                    owner_id=user.id,
                    connector_type=conn_type,
                    encrypted_value=best_cred.encrypted_value,
                    display_name=best_cred.display_name,
                    connector_metadata=best_cred.connector_metadata,
                    test_status=best_cred.test_status,
                    last_tested_at=best_cred.last_tested_at,
                )
                db.add(new_account_cred)
                db.flush() # get ID
                total_promoted += 1

            # Identify redundant credentials
            ids_to_delete = []
            for cred, agent in items:
                if cred.encrypted_value == best_cred.encrypted_value:
                    # Exact match - can be deleted (will inherit)
                    ids_to_delete.append(cred.id)
                    if not dry_run:
                        db.delete(cred)
                    total_deleted += 1
                    logger.info(f"    -> Deleting redundant cred for Agent {agent.id}")
                else:
                    # Different value - keep as override
                    total_kept_override += 1
                    logger.info(f"    -> Keeping distinct override for Agent {agent.id}")

    if not dry_run:
        # db_session context manager commits automatically on exit, but we can't rely on that
        # if we are passing 'db' into this function.
        # Wait, db_session yields a session and commits on exit.
        # So we don't need explicit commit here IF we let the context manager handle it.
        # But for dry run rollback, we need control.
        # The db_session context manager rolls back on exception.
        # If we want dry run, we should probably raise an exception or manually rollback?
        pass
    else:
        # For dry run, we might want to rollback if we made changes?
        # But we didn't make changes if dry_run=True (logic above skipped adds/deletes).
        # Wait, if dry_run is True, I guarded the db.add and db.delete calls.
        # So no DB changes happened.
        logger.info("Dry run complete. No changes made.")

    logger.info("Summary:")
    logger.info(f"  Promoted (created account creds): {total_promoted}")
    logger.info(f"  Deleted (redundant agent creds): {total_deleted}")
    logger.info(f"  Kept (overrides): {total_kept_override}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate agent credentials to account level")
    parser.add_argument("--dry-run", action="store_true", help="Simulate migration without applying changes")
    args = parser.parse_args()

    print("Initializing database session...")
    # Use the context manager to get a session
    with db_session() as db:
        try:
            migrate_credentials(db, dry_run=args.dry_run)
        except Exception as e:
            logger.exception("Migration failed")
            sys.exit(1)
