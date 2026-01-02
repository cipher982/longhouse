#!/usr/bin/env python3
"""
Seed a marketing-ready workflow for landing page screenshots.

Creates:
1. Named agents with meaningful names (Email Analyzer, Slack Notifier, etc.)
2. A workflow with agents positioned in a visually appealing layout
3. Edges connecting them to show data flow

Usage:
    cd apps/zerg/backend && uv run python scripts/seed_marketing_workflow.py
"""

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from zerg.crud import crud
from zerg.database import db_session
from zerg.models.enums import AgentStatus
from zerg.models.models import Agent, Workflow, User
from zerg.models_config import DEFAULT_WORKER_MODEL_ID


# Marketing-ready agents with descriptive names
MARKETING_AGENTS = [
    {
        "name": "Email Watcher",
        "system_instructions": "Monitor incoming emails and classify by priority.",
    },
    {
        "name": "Content Analyzer",
        "system_instructions": "Analyze message content for sentiment and urgency.",
    },
    {
        "name": "Priority Router",
        "system_instructions": "Route messages based on priority and sender.",
    },
    {
        "name": "Slack Notifier",
        "system_instructions": "Send notifications to appropriate Slack channels.",
    },
    {
        "name": "Calendar Checker",
        "system_instructions": "Check calendar for conflicts and availability.",
    },
    {
        "name": "Task Creator",
        "system_instructions": "Create tasks from emails and messages.",
    },
]


def create_workflow_canvas(agents: list[Agent]) -> dict:
    """
    Create a visually appealing workflow canvas layout.

    Layout:
        [Trigger] ──→ [Email Watcher] ──→ [Content Analyzer]
                                              │
                                              ↓
        [Task Creator] ←── [Priority Router] ←┘
              │                    │
              ↓                    ↓
        [Calendar Checker]   [Slack Notifier]
    """

    # Map agent names to their IDs
    agent_map = {a.name: a.id for a in agents}

    nodes = []
    edges = []

    # Trigger node (top-left)
    trigger_id = str(uuid.uuid4())
    nodes.append({
        "id": trigger_id,
        "type": "trigger",
        "position": {"x": 100.0, "y": 200.0},
        "config": {
            "text": "New Email",
            "trigger": {
                "type": "webhook",
                "config": {
                    "enabled": True,
                    "params": {},
                    "filters": [],
                }
            }
        }
    })

    # Position agents in a flowing layout
    positions = [
        ("Email Watcher", 350, 200),      # Right of trigger
        ("Content Analyzer", 600, 200),   # Right of email watcher
        ("Priority Router", 600, 400),    # Below analyzer
        ("Slack Notifier", 850, 400),     # Right of router
        ("Task Creator", 350, 400),       # Left of router
        ("Calendar Checker", 350, 550),   # Below task creator
    ]

    node_ids = {"trigger": trigger_id}  # track node IDs for edges

    for agent_name, x, y in positions:
        if agent_name not in agent_map:
            continue

        node_id = str(uuid.uuid4())
        node_ids[agent_name] = node_id

        nodes.append({
            "id": node_id,
            "type": "agent",
            "position": {"x": float(x), "y": float(y)},
            "config": {
                "text": agent_name,
                "agent_id": agent_map[agent_name],
            }
        })

    # Create edges to show data flow
    edge_connections = [
        ("trigger", "Email Watcher"),
        ("Email Watcher", "Content Analyzer"),
        ("Content Analyzer", "Priority Router"),
        ("Priority Router", "Slack Notifier"),
        ("Priority Router", "Task Creator"),
        ("Task Creator", "Calendar Checker"),
    ]

    for source, target in edge_connections:
        source_id = node_ids.get(source, source)
        target_id = node_ids.get(target)

        if source_id and target_id:
            edges.append({
                "from_node_id": source_id,
                "to_node_id": target_id,
                "config": {},
            })

    return {"nodes": nodes, "edges": edges}


def seed_marketing_workflow():
    """Create marketing-ready agents and workflow."""
    print("🎨 Seeding marketing workflow for screenshots...")

    with db_session() as db:
        # Use dev@local user (the default dev mode user)
        dev_email = "dev@local"
        dev_user = crud.get_user_by_email(db, dev_email)

        if not dev_user:
            print(f"  Creating dev user: {dev_email}")
            dev_user = crud.create_user(
                db,
                email=dev_email,
                provider="dev",
                role="USER",
            )
            dev_user.display_name = "Dev User"
            db.commit()
            db.refresh(dev_user)

        # Create or update agents
        created_agents = []
        for agent_def in MARKETING_AGENTS:
            existing = db.query(Agent).filter(
                Agent.name == agent_def["name"],
                Agent.owner_id == dev_user.id,
            ).first()

            if existing:
                print(f"  ✓ Agent exists: {agent_def['name']}")
                created_agents.append(existing)
            else:
                print(f"  ✨ Creating agent: {agent_def['name']}")
                agent = Agent(
                    owner_id=dev_user.id,
                    name=agent_def["name"],
                    system_instructions=agent_def["system_instructions"],
                    task_instructions="",
                    model=DEFAULT_WORKER_MODEL_ID,
                    status=AgentStatus.IDLE,
                )
                db.add(agent)
                db.flush()
                created_agents.append(agent)

        db.commit()

        # Refresh to get IDs
        for agent in created_agents:
            db.refresh(agent)

        # Create or update workflow
        workflow_name = "Email Automation Pipeline"
        existing_workflow = db.query(Workflow).filter(
            Workflow.name == workflow_name,
            Workflow.owner_id == dev_user.id,
        ).first()

        canvas = create_workflow_canvas(created_agents)

        if existing_workflow:
            print(f"  ✓ Updating workflow: {workflow_name}")
            existing_workflow.canvas = canvas
            db.add(existing_workflow)
        else:
            print(f"  ✨ Creating workflow: {workflow_name}")
            workflow = Workflow(
                owner_id=dev_user.id,
                name=workflow_name,
                description="Automated email processing and notification pipeline",
                canvas=canvas,
                is_active=True,
            )
            db.add(workflow)

        db.commit()

        print("\n✅ Marketing workflow seeded!")
        print(f"   Agents: {len(created_agents)}")
        print(f"   Nodes: {len(canvas['nodes'])}")
        print(f"   Edges: {len(canvas['edges'])}")
        print("\n📸 Open /canvas to take screenshot")


if __name__ == "__main__":
    try:
        seed_marketing_workflow()
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
