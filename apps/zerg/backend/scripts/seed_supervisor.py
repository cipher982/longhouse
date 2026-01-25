#!/usr/bin/env python3
"""Seed the Supervisor Agent for the supervisor/worker architecture.

This script creates a pre-configured Supervisor Agent that users can interact with
to delegate tasks to worker agents.

Usage:
    uv run python scripts/seed_supervisor.py

Optional arguments:
    --user-email EMAIL    Specify user email (default: uses first user or creates dev user)
    --name NAME          Custom supervisor name (default: "Supervisor")
"""

import argparse
import sys
from pathlib import Path

# Add parent directory to path so we can import zerg modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from zerg.crud import crud
from zerg.database import get_db
from zerg.models.enums import AgentStatus
from zerg.models.models import Agent
from zerg.models_config import DEFAULT_MODEL_ID
from zerg.prompts import build_supervisor_prompt


def get_or_create_user(db, email: str = None):
    """Get existing user or create one for development."""
    if email:
        user = crud.get_user_by_email(db, email)
        if not user:
            print(f"❌ User with email {email} not found")
            sys.exit(1)
        return user

    # Get first user or create dev user
    users = crud.get_agents(db, limit=1)
    if users:
        # Get owner of first agent
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


def seed_supervisor(user_email: str = None, name: str = "Supervisor"):
    """Create or update the Supervisor Agent."""
    print("🌱 Seeding Supervisor Agent...")

    # Get database session
    db = next(get_db())

    # Get or create user
    user = get_or_create_user(db, user_email)
    print(f"👤 User: {user.email} (ID: {user.id})")

    # Check if supervisor already exists
    existing = db.query(Agent).filter(
        Agent.name == name,
        Agent.owner_id == user.id,
    ).first()

    # Define supervisor configuration
    supervisor_config = {
        "is_supervisor": True,
        "temperature": 0.7,
        "max_tokens": 2000,
    }

    # Supervisor tools - carefully selected for delegation and direct tasks
    supervisor_tools = [
        # Supervisor/delegation tools
        "spawn_commis",
        "list_commis",
        "read_commis_result",
        "read_commis_file",
        "grep_commis",
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
    system_prompt = build_supervisor_prompt(user)

    # Simple task instructions that will be appended to every conversation
    task_instructions = """You are helping the user accomplish their goals.

Analyze their request and decide:
- Can you handle this directly with your tools? → Do it.
- Does this need investigation or multiple steps? → Delegate to a worker.
- Is this a follow-up about previous work? → Query past workers.

Be helpful, concise, and transparent about what you're doing."""

    if existing:
        print(f"  ⚠️  Supervisor already exists: {name} (ID: {existing.id})")
        print(f"  🔄 Updating configuration...")

        # Update existing agent
        existing.system_instructions = system_prompt
        existing.task_instructions = task_instructions
        existing.model = DEFAULT_MODEL_ID  # Supervisor should be smart
        existing.config = supervisor_config
        existing.allowed_tools = supervisor_tools
        existing.status = AgentStatus.IDLE
        existing.schedule = None  # No automatic scheduling for supervisor

        db.add(existing)
        db.commit()
        db.refresh(existing)

        print(f"  ✅ Supervisor updated successfully")
        agent = existing
    else:
        print(f"  ✨ Creating new supervisor: {name}")

        # Create new supervisor agent
        agent = Agent(
            owner_id=user.id,
            name=name,
            system_instructions=system_prompt,
            task_instructions=task_instructions,
            model=DEFAULT_MODEL_ID,  # Supervisor should be smart
            config=supervisor_config,
            allowed_tools=supervisor_tools,
            status=AgentStatus.IDLE,
            schedule=None,  # No automatic scheduling
        )
        db.add(agent)
        db.commit()
        db.refresh(agent)

        print(f"  ✅ Supervisor created successfully (ID: {agent.id})")

    print(f"\n📋 Supervisor Configuration:")
    print(f"   Name: {agent.name}")
    print(f"   ID: {agent.id}")
    print(f"   Owner: {user.email}")
    print(f"   Model: {agent.model}")
    print(f"   Tools: {len(agent.allowed_tools)} tools")
    print(f"     - Supervisor: spawn_commis, list_commis, read_commis_result, etc.")
    print(f"     - Direct: get_current_time, http_request, send_email")

    print(f"\n🚀 Supervisor is ready!")
    print(f"   You can now interact with the supervisor through:")
    print(f"   - Chat UI: Create a thread with this agent")
    print(f"   - API: POST /api/agents/{agent.id}/threads")
    print(f"   - Jarvis: Configure voice interaction")

    return agent


def main():
    """Main entry point with argument parsing."""
    parser = argparse.ArgumentParser(
        description="Seed the Supervisor Agent for supervisor/worker architecture"
    )
    parser.add_argument(
        "--user-email",
        type=str,
        help="Email of user to own the supervisor (default: first user or create dev user)",
    )
    parser.add_argument(
        "--name",
        type=str,
        default="Supervisor",
        help="Name for the supervisor agent (default: Supervisor)",
    )

    args = parser.parse_args()

    try:
        seed_supervisor(user_email=args.user_email, name=args.name)
    except Exception as e:
        print(f"\n❌ Error seeding supervisor: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
