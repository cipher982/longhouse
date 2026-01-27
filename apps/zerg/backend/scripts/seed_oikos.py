#!/usr/bin/env python3
"""Seed the Oikos Fiche for the oikos/commis architecture.

This script creates a pre-configured Oikos Fiche that users can interact with
to delegate tasks to commis fiches.

Usage:
    uv run python scripts/seed_oikos.py

Optional arguments:
    --user-email EMAIL    Specify user email (default: uses first user or creates dev user)
    --name NAME          Custom oikos name (default: "Oikos")
"""

import argparse
import sys
from pathlib import Path

# Add parent directory to path so we can import zerg modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from zerg.crud import crud
from zerg.database import get_db
from zerg.models.enums import FicheStatus
from zerg.models.models import Fiche
from zerg.models_config import DEFAULT_MODEL_ID
from zerg.prompts import build_oikos_prompt


def get_or_create_user(db, email: str = None):
    """Get existing user or create one for development."""
    if email:
        user = crud.get_user_by_email(db, email)
        if not user:
            print(f"❌ User with email {email} not found")
            sys.exit(1)
        return user

    # Get first user or create dev user
    users = crud.get_fiches(db, limit=1)
    if users:
        # Get owner of first fiche
        return users[0].owner

    # Create development user
    print("Creating development user: dev@local")
    user = crud.create_user(
        db,
        email="dev@local",
        provider="dev",
        role="ADMIN",
    )
    user.display_name = "Developer"
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def seed_oikos(user_email: str = None, name: str = "Oikos"):
    """Create or update the Oikos Fiche."""
    print("🌱 Seeding Oikos Fiche...")

    # Get database session
    db = next(get_db())

    # Get or create user
    user = get_or_create_user(db, user_email)
    print(f"👤 User: {user.email} (ID: {user.id})")

    # Check if oikos already exists
    existing = db.query(Fiche).filter(
        Fiche.name == name,
        Fiche.owner_id == user.id,
    ).first()

    # Define oikos configuration
    oikos_config = {
        "is_oikos": True,
        "temperature": 0.7,
        "max_tokens": 2000,
    }

    # Oikos tools - carefully selected for delegation and direct tasks
    oikos_tools = [
        # Oikos/delegation tools
        "spawn_commis",
        "list_commiss",
        "read_commis_result",
        "read_commis_file",
        "grep_commiss",
        "get_commis_metadata",
        # Direct utility tools
        "get_current_time",
        "http_request",
        # Notification tools (if configured)
        "send_email",
        # Knowledge base
        "knowledge_search",
        # Runner setup
        "runner_list",
        "runner_create_enroll_token",
    ]

    # Get the context-aware system prompt (preferred over legacy template)
    system_prompt = build_oikos_prompt(user)

    # Simple task instructions that will be appended to every conversation
    task_instructions = """You are helping the user accomplish their goals.

Analyze their request and decide:
- Can you handle this directly with your tools? → Do it.
- Does this need investigation or multiple steps? → Delegate to a commis.
- Is this a follow-up about previous work? → Query past commis.

Be helpful, concise, and transparent about what you're doing."""

    if existing:
        print(f"  ⚠️  Oikos already exists: {name} (ID: {existing.id})")
        print(f"  🔄 Updating configuration...")

        # Update existing fiche
        existing.system_instructions = system_prompt
        existing.task_instructions = task_instructions
        existing.model = DEFAULT_MODEL_ID  # Oikos should be smart
        existing.config = oikos_config
        existing.allowed_tools = oikos_tools
        existing.status = FicheStatus.IDLE
        existing.schedule = None  # No automatic scheduling for oikos

        db.add(existing)
        db.commit()
        db.refresh(existing)

        print(f"  ✅ Oikos updated successfully")
        fiche = existing
    else:
        print(f"  ✨ Creating new oikos: {name}")

        # Create new oikos fiche
        fiche = Fiche(
            owner_id=user.id,
            name=name,
            system_instructions=system_prompt,
            task_instructions=task_instructions,
            model=DEFAULT_MODEL_ID,  # Oikos should be smart
            config=oikos_config,
            allowed_tools=oikos_tools,
            status=FicheStatus.IDLE,
            schedule=None,  # No automatic scheduling
        )
        db.add(fiche)
        db.commit()
        db.refresh(fiche)

        print(f"  ✅ Oikos created successfully (ID: {fiche.id})")

    print(f"\n📋 Oikos Configuration:")
    print(f"   Name: {fiche.name}")
    print(f"   ID: {fiche.id}")
    print(f"   Owner: {user.email}")
    print(f"   Model: {fiche.model}")
    print(f"   Tools: {len(fiche.allowed_tools)} tools")
    print(f"     - Oikos: spawn_commis, list_commiss, read_commis_result, etc.")
    print(f"     - Direct: get_current_time, http_request, send_email")

    print(f"\n🚀 Oikos is ready!")
    print(f"   You can now interact with the oikos through:")
    print(f"   - Chat UI: Create a thread with this fiche")
    print(f"   - API: POST /api/fiches/{fiche.id}/threads")
    print(f"   - Oikos: Configure voice interaction")

    return fiche


def main():
    """Main entry point with argument parsing."""
    parser = argparse.ArgumentParser(
        description="Seed the Oikos Fiche for oikos/commis architecture"
    )
    parser.add_argument(
        "--user-email",
        type=str,
        help="Email of user to own the oikos (default: first user or create dev user)",
    )
    parser.add_argument(
        "--name",
        type=str,
        default="Oikos",
        help="Name for the oikos fiche (default: Oikos)",
    )

    args = parser.parse_args()

    try:
        seed_oikos(user_email=args.user_email, name=args.name)
    except Exception as e:
        print(f"\n❌ Error seeding oikos: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
