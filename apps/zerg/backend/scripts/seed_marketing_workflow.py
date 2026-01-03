#!/usr/bin/env python3
"""
Seed marketing-ready data for landing page screenshots.

Creates:
1. Three distinct workflows (Health, Inbox, Home Automation)
2. Agents with varied statuses and recent activity
3. AgentRun records showing execution history
4. A Supervisor thread with a realistic chat conversation

This script is idempotent - it cleans up existing marketing data before seeding.

Usage:
    cd apps/zerg/backend && uv run python scripts/seed_marketing_workflow.py
"""

import sys
import uuid
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage
from langchain_core.messages import ToolMessage

from zerg.crud import crud
from zerg.database import db_session
from zerg.models.enums import AgentStatus
from zerg.models.enums import RunStatus
from zerg.models.enums import RunTrigger
from zerg.models.enums import ThreadType
from zerg.models.models import Agent
from zerg.models.models import Workflow
from zerg.models.run import AgentRun
from zerg.models.thread import Thread
from zerg.models_config import DEFAULT_MODEL_ID
from zerg.models_config import DEFAULT_WORKER_MODEL_ID
from zerg.services.thread_service import ThreadService

# Marketing tag to identify seeded data for cleanup
MARKETING_TAG = "marketing_demo"

# ============================================================================
# WORKFLOW DEFINITIONS
# ============================================================================

HEALTH_WORKFLOW = {
    "name": "health",  # Short name for URL addressability: /canvas?workflow=health
    "description": "Automated health monitoring and daily wellness summary",
    "agents": [
        {"name": "WHOOP Monitor", "instructions": "Pull recovery and sleep metrics from WHOOP API."},
        {"name": "Health Analyzer", "instructions": "Analyze health trends and flag anomalies."},
        {"name": "Wellness Notifier", "instructions": "Send daily wellness summary via Slack or email."},
    ],
    "layout": [
        # (name, x, y)
        ("trigger", 100, 200),
        ("WHOOP Monitor", 350, 200),
        ("Health Analyzer", 600, 200),
        ("Wellness Notifier", 850, 200),
    ],
    "edges": [
        ("trigger", "WHOOP Monitor"),
        ("WHOOP Monitor", "Health Analyzer"),
        ("Health Analyzer", "Wellness Notifier"),
    ],
    "trigger_text": "6:00 AM Daily",
}

INBOX_WORKFLOW = {
    "name": "inbox",  # Short name for URL addressability: /canvas?workflow=inbox
    "description": "Intelligent email triage and task management",
    "agents": [
        {"name": "Email Watcher", "instructions": "Monitor incoming emails and classify by priority."},
        {"name": "Content Analyzer", "instructions": "Analyze message content for sentiment and urgency."},
        {"name": "Priority Router", "instructions": "Route messages based on priority and sender."},
        {"name": "Slack Notifier", "instructions": "Send notifications to appropriate Slack channels."},
        {"name": "Task Creator", "instructions": "Create tasks from emails and messages."},
        {"name": "Calendar Checker", "instructions": "Check calendar for conflicts and availability."},
    ],
    "layout": [
        ("trigger", 100, 300),
        ("Email Watcher", 350, 300),
        ("Content Analyzer", 600, 300),
        ("Priority Router", 600, 500),
        ("Slack Notifier", 850, 400),
        ("Task Creator", 350, 500),
        ("Calendar Checker", 350, 650),
    ],
    "edges": [
        ("trigger", "Email Watcher"),
        ("Email Watcher", "Content Analyzer"),
        ("Content Analyzer", "Priority Router"),
        ("Priority Router", "Slack Notifier"),
        ("Priority Router", "Task Creator"),
        ("Task Creator", "Calendar Checker"),
    ],
    "trigger_text": "New Email",
}

HOME_WORKFLOW = {
    "name": "home",  # Short name for URL addressability: /canvas?workflow=home
    "description": "Location-aware home automation and presence detection",
    "agents": [
        {"name": "Location Tracker", "instructions": "Monitor GPS location from Traccar."},
        {"name": "Presence Detector", "instructions": "Determine home/away status from location."},
        {"name": "Light Controller", "instructions": "Control smart lights based on presence."},
        {"name": "Thermostat Manager", "instructions": "Adjust temperature based on presence."},
    ],
    "layout": [
        ("trigger", 100, 250),
        ("Location Tracker", 350, 250),
        ("Presence Detector", 600, 250),
        ("Light Controller", 850, 150),
        ("Thermostat Manager", 850, 350),
    ],
    "edges": [
        ("trigger", "Location Tracker"),
        ("Location Tracker", "Presence Detector"),
        ("Presence Detector", "Light Controller"),
        ("Presence Detector", "Thermostat Manager"),
    ],
    "trigger_text": "Location Change",
}

ALL_WORKFLOWS = [HEALTH_WORKFLOW, INBOX_WORKFLOW, HOME_WORKFLOW]

# ============================================================================
# CHAT CONVERSATION
# ============================================================================

def build_chat_conversation() -> list:
    """Build the marketing chat conversation using LangChain message types.

    This ensures the tool_calls format matches exactly what the real agent runner produces,
    avoiding format mismatches between seeded data and live data.
    """
    return [
        HumanMessage(content="Check my health data and suggest activities for today"),
        AIMessage(
            content="I'll check your WHOOP data to see how recovered you are today.",
            tool_calls=[
                {"id": "call_health_1", "name": "get_whoop_data", "args": {}},
            ],
        ),
        ToolMessage(
            content='{"recovery_score": 85, "hrv": 68, "resting_hr": 52, "sleep_hours": 7.5, "strain": 8.2}',
            tool_call_id="call_health_1",
            name="get_whoop_data",
        ),
        AIMessage(
            content="Great news! Your recovery score is **85%** with excellent HRV (68ms). "
            "You got 7.5 hours of sleep last night. Based on this, you're well-recovered "
            "and ready for moderate to high intensity activities.\n\n"
            "I'd recommend a solid workout today - your body can handle it.",
        ),
        HumanMessage(content="What's the weather like? Should I go for a run?"),
        AIMessage(
            content="Let me check your location and the weather there.",
            tool_calls=[
                {"id": "call_loc_1", "name": "get_current_location", "args": {}},
            ],
        ),
        ToolMessage(
            content='{"city": "San Francisco", "lat": 37.7749, "lon": -122.4194}',
            tool_call_id="call_loc_1",
            name="get_current_location",
        ),
        AIMessage(
            content="You're in San Francisco. It's a beautiful sunny day (72°F) with low humidity - "
            "perfect running weather! Given your high recovery score, I'd recommend a 5-7 mile run "
            "at moderate pace.\n\nWould you like me to create a calendar event for your run?",
        ),
        HumanMessage(content="Yes, block out an hour at 5pm"),
        AIMessage(content="Done! I've blocked 5-6pm today for your run. Have a great workout!"),
    ]


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================


def cleanup_marketing_data(db, dev_user):
    """Remove all existing marketing demo data using raw SQL for proper cascading.

    IMPORTANT: All queries are scoped to dev_user.id to avoid deleting real user data.
    """
    from sqlalchemy import text

    print("🧹 Cleaning up existing marketing data...")

    # Find marketing workflows by name - SCOPED TO DEV USER
    workflow_names = [w["name"] for w in ALL_WORKFLOWS]
    workflows = db.query(Workflow).filter(
        Workflow.name.in_(workflow_names),
        Workflow.owner_id == dev_user.id,
    ).all()

    # Find marketing agents - SCOPED TO DEV USER
    agent_names = []
    for wf in ALL_WORKFLOWS:
        agent_names.extend([a["name"] for a in wf["agents"]])
    agent_names.append("Jarvis")  # Supervisor for chat

    agents = db.query(Agent).filter(
        Agent.name.in_(agent_names),
        Agent.owner_id == dev_user.id,
    ).all()
    agent_ids = [a.id for a in agents]

    # Delete workflows first (no FK deps)
    for wf in workflows:
        db.delete(wf)
        print(f"  - Deleted workflow: {wf.name}")

    if agent_ids:
        # Use raw SQL with proper order to handle FK constraints
        # 1. Delete run events (references agent_runs)
        db.execute(text("""
            DELETE FROM agent_run_events
            WHERE run_id IN (SELECT id FROM agent_runs WHERE agent_id = ANY(:ids))
        """), {"ids": agent_ids})

        # 2. Delete runs (references agent_threads)
        db.execute(text("""
            DELETE FROM agent_runs WHERE agent_id = ANY(:ids)
        """), {"ids": agent_ids})

        # 3. Delete messages (self-referential parent_id + references threads)
        db.execute(text("""
            DELETE FROM thread_messages
            WHERE thread_id IN (SELECT id FROM agent_threads WHERE agent_id = ANY(:ids))
        """), {"ids": agent_ids})

        # 4. Delete threads
        db.execute(text("""
            DELETE FROM agent_threads WHERE agent_id = ANY(:ids)
        """), {"ids": agent_ids})

        # 5. Delete agents
        db.execute(text("""
            DELETE FROM agents WHERE id = ANY(:ids)
        """), {"ids": agent_ids})

        for agent in agents:
            print(f"  - Deleted agent: {agent.name}")

    # Also clean up orphaned marketing thread - SCOPED TO DEV USER via agent ownership
    # IMPORTANT: Only clean up MANUAL threads to avoid deleting the real SUPER supervisor thread
    db.execute(text("""
        DELETE FROM thread_messages WHERE thread_id IN (
            SELECT t.id FROM agent_threads t
            JOIN agents a ON t.agent_id = a.id
            WHERE t.title = 'marketing' AND t.thread_type = 'manual' AND a.owner_id = :owner_id
        )
    """), {"owner_id": dev_user.id})
    db.execute(text("""
        DELETE FROM agent_threads WHERE id IN (
            SELECT t.id FROM agent_threads t
            JOIN agents a ON t.agent_id = a.id
            WHERE t.title = 'marketing' AND t.thread_type = 'manual' AND a.owner_id = :owner_id
        )
    """), {"owner_id": dev_user.id})

    db.commit()
    print("  ✓ Cleanup complete\n")


def create_workflow_canvas(agents: list[Agent], workflow_def: dict) -> dict:
    """Create workflow canvas JSON with nodes and edges."""
    agent_map = {a.name: a.id for a in agents}
    nodes = []
    edges = []
    node_ids = {}

    # Create nodes
    for item in workflow_def["layout"]:
        name, x, y = item
        node_id = str(uuid.uuid4())
        node_ids[name] = node_id

        if name == "trigger":
            nodes.append({
                "id": node_id,
                "type": "trigger",
                "position": {"x": float(x), "y": float(y)},
                "config": {
                    "text": workflow_def.get("trigger_text", "Trigger"),
                    "trigger": {
                        "type": "webhook",
                        "config": {"enabled": True, "params": {}, "filters": []},
                    },
                },
            })
        elif name in agent_map:
            nodes.append({
                "id": node_id,
                "type": "agent",
                "position": {"x": float(x), "y": float(y)},
                "config": {"text": name, "agent_id": agent_map[name]},
            })

    # Create edges
    for source, target in workflow_def["edges"]:
        source_id = node_ids.get(source)
        target_id = node_ids.get(target)
        if source_id and target_id:
            edges.append({
                "from_node_id": source_id,
                "to_node_id": target_id,
                "config": {},
            })

    return {"nodes": nodes, "edges": edges}


def seed_agents_for_workflow(db, user, workflow_def: dict) -> list[Agent]:
    """Create agents for a workflow."""
    agents = []
    for agent_def in workflow_def["agents"]:
        agent = Agent(
            owner_id=user.id,
            name=agent_def["name"],
            system_instructions=agent_def["instructions"],
            task_instructions="",
            model=DEFAULT_WORKER_MODEL_ID,
            status=AgentStatus.IDLE,
        )
        db.add(agent)
        db.flush()
        agents.append(agent)
        print(f"    ✨ Created agent: {agent_def['name']}")
    return agents


def seed_agent_runs(db, agents: list[Agent], user):
    """Create AgentRun records with varied statuses for visual appeal."""
    now = datetime.now(timezone.utc)

    for i, agent in enumerate(agents):
        # Create a thread for the run
        thread = Thread(
            agent_id=agent.id,
            title=f"Run for {agent.name}",
            thread_type=ThreadType.MANUAL,
        )
        db.add(thread)
        db.flush()

        # Vary statuses for visual interest
        if i % 4 == 0:
            status = RunStatus.SUCCESS
            started = now - timedelta(minutes=30)
            finished = now - timedelta(minutes=25)
            duration = 5 * 60 * 1000  # 5 min
            agent.status = AgentStatus.IDLE
            agent.last_run_at = finished
        elif i % 4 == 1:
            status = RunStatus.RUNNING
            started = now - timedelta(minutes=2)
            finished = None
            duration = None
            agent.status = AgentStatus.RUNNING
        elif i % 4 == 2:
            status = RunStatus.SUCCESS
            started = now - timedelta(hours=2)
            finished = now - timedelta(hours=1, minutes=55)
            duration = 5 * 60 * 1000
            agent.status = AgentStatus.IDLE
            agent.last_run_at = finished
        else:
            status = RunStatus.SUCCESS
            started = now - timedelta(hours=6)
            finished = now - timedelta(hours=5, minutes=58)
            duration = 2 * 60 * 1000
            agent.status = AgentStatus.IDLE
            agent.last_run_at = finished

        run = AgentRun(
            agent_id=agent.id,
            thread_id=thread.id,
            status=status,
            trigger=RunTrigger.SCHEDULE,
            started_at=started,
            finished_at=finished,
            duration_ms=duration,
            total_tokens=1500 + (i * 200),
            total_cost_usd=0.002 + (i * 0.0005),
            summary=f"Executed {agent.name} task successfully.",
        )
        db.add(run)

    db.commit()


def seed_chat_thread(db, supervisor: Agent):
    """Create a Supervisor thread with realistic chat messages.

    Uses ThreadService.save_new_messages() to ensure messages are stored
    in the exact same format as the real agent runner produces.

    IMPORTANT: Uses MANUAL thread type to avoid collision with the real
    supervisor thread (which uses SUPER type and has "one per user" constraint).
    """
    print("  💬 Creating chat conversation...")

    thread = Thread(
        agent_id=supervisor.id,
        title="marketing",  # Short name for URL addressability: /chat?thread=marketing
        thread_type=ThreadType.MANUAL,  # MANUAL to avoid collision with real supervisor
        active=True,
    )
    db.add(thread)
    db.flush()

    # Build conversation using LangChain message types (same as real agent)
    langchain_messages = build_chat_conversation()

    # Use ThreadService to save messages - this ensures format consistency
    # with the real agent runner code path
    ThreadService.save_new_messages(db, thread_id=thread.id, messages=langchain_messages)

    db.commit()
    print(f"    ✓ Created thread with {len(langchain_messages)} messages")
    return thread


# ============================================================================
# MAIN SEEDING FUNCTION
# ============================================================================


def seed_marketing_data():
    """Seed all marketing data for screenshots."""
    print("🎨 Seeding marketing data for screenshots...")
    print("=" * 60)

    with db_session() as db:
        # Get or create dev user
        dev_email = "dev@local"
        dev_user = crud.get_user_by_email(db, dev_email)
        if not dev_user:
            print(f"  Creating dev user: {dev_email}")
            dev_user = crud.create_user(db, email=dev_email, provider="dev", role="USER")
            dev_user.display_name = "Dev User"
            db.commit()
            db.refresh(dev_user)

        # Cleanup existing marketing data (scoped to dev user)
        cleanup_marketing_data(db, dev_user)

        # Seed each workflow
        all_agents = []
        for workflow_def in ALL_WORKFLOWS:
            print(f"\n📊 Seeding workflow: {workflow_def['name']}")

            # Create agents
            agents = seed_agents_for_workflow(db, dev_user, workflow_def)
            all_agents.extend(agents)

            # Create workflow
            canvas = create_workflow_canvas(agents, workflow_def)
            workflow = Workflow(
                owner_id=dev_user.id,
                name=workflow_def["name"],
                description=workflow_def["description"],
                canvas=canvas,
                is_active=True,
            )
            db.add(workflow)
            db.commit()

            print(f"    ✓ Created workflow with {len(canvas['nodes'])} nodes, {len(canvas['edges'])} edges")

        # Seed agent runs for varied statuses
        print("\n📈 Seeding agent runs...")
        seed_agent_runs(db, all_agents, dev_user)
        print(f"    ✓ Created runs for {len(all_agents)} agents")

        # Create Supervisor for chat
        print("\n🤖 Creating Supervisor for chat...")
        supervisor = Agent(
            owner_id=dev_user.id,
            name="Jarvis",
            system_instructions="You are Jarvis, a helpful AI assistant.",
            task_instructions="Help the user with their requests.",
            model=DEFAULT_MODEL_ID,
            status=AgentStatus.IDLE,
            config={"is_supervisor": True},
        )
        db.add(supervisor)
        db.flush()
        print(f"    ✓ Created Supervisor: Jarvis (ID: {supervisor.id})")

        # Create chat thread
        thread = seed_chat_thread(db, supervisor)

        db.commit()

        # Summary
        print("\n" + "=" * 60)
        print("✅ Marketing data seeded successfully!")
        print(f"   Workflows: {len(ALL_WORKFLOWS)}")
        print(f"   Agents: {len(all_agents) + 1}")  # +1 for Supervisor
        print(f"   Chat thread ID: {thread.id}")
        print("\n📸 Ready for screenshots!")
        print("   - Canvas: /canvas?workflow=health&marketing=true")
        print("   - Chat: /chat?thread=marketing&marketing=true")
        print("   - Dashboard: /dashboard?marketing=true")


if __name__ == "__main__":
    try:
        seed_marketing_data()
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
