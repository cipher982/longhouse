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
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from zerg.crud import crud
from zerg.database import db_session
from zerg.models.enums import AgentStatus, RunStatus, RunTrigger, ThreadType
from zerg.models.models import Agent, Workflow
from zerg.models.run import AgentRun
from zerg.models.thread import Thread, ThreadMessage
from zerg.models_config import DEFAULT_MODEL_ID, DEFAULT_WORKER_MODEL_ID

# Marketing tag to identify seeded data for cleanup
MARKETING_TAG = "marketing_demo"

# ============================================================================
# WORKFLOW DEFINITIONS
# ============================================================================

HEALTH_WORKFLOW = {
    "name": "Morning Health Check",
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
    "name": "Email Automation Pipeline",
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
    "name": "Smart Home Automation",
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

CHAT_CONVERSATION = [
    {
        "role": "user",
        "content": "Check my health data and suggest activities for today",
    },
    {
        "role": "assistant",
        "content": "I'll check your WHOOP data to see how recovered you are today.",
        "tool_calls": [
            {
                "id": "call_health_1",
                "type": "function",
                "function": {"name": "get_whoop_data", "arguments": "{}"},
            }
        ],
    },
    {
        "role": "tool",
        "content": '{"recovery_score": 85, "hrv": 68, "resting_hr": 52, "sleep_hours": 7.5, "strain": 8.2}',
        "tool_call_id": "call_health_1",
        "name": "get_whoop_data",
    },
    {
        "role": "assistant",
        "content": "Great news! Your recovery score is **85%** with excellent HRV (68ms). You got 7.5 hours of sleep last night. Based on this, you're well-recovered and ready for moderate to high intensity activities.\n\nI'd recommend a solid workout today - your body can handle it.",
    },
    {
        "role": "user",
        "content": "What's the weather like? Should I go for a run?",
    },
    {
        "role": "assistant",
        "content": "Let me check your location and the weather there.",
        "tool_calls": [
            {
                "id": "call_loc_1",
                "type": "function",
                "function": {"name": "get_current_location", "arguments": "{}"},
            }
        ],
    },
    {
        "role": "tool",
        "content": '{"city": "San Francisco", "lat": 37.7749, "lon": -122.4194}',
        "tool_call_id": "call_loc_1",
        "name": "get_current_location",
    },
    {
        "role": "assistant",
        "content": "You're in San Francisco. It's a beautiful sunny day (72°F) with low humidity - perfect running weather! Given your high recovery score, I'd recommend a 5-7 mile run at moderate pace.\n\nWould you like me to create a calendar event for your run?",
    },
    {
        "role": "user",
        "content": "Yes, block out an hour at 5pm",
    },
    {
        "role": "assistant",
        "content": "Done! I've blocked 5-6pm today for your run. Have a great workout!",
    },
]


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================


def cleanup_marketing_data(db):
    """Remove all existing marketing demo data."""
    print("🧹 Cleaning up existing marketing data...")

    # Find marketing workflows by name
    workflow_names = [w["name"] for w in ALL_WORKFLOWS]
    workflows = db.query(Workflow).filter(Workflow.name.in_(workflow_names)).all()

    # Find marketing agents
    agent_names = []
    for wf in ALL_WORKFLOWS:
        agent_names.extend([a["name"] for a in wf["agents"]])
    agent_names.append("Jarvis")  # Supervisor for chat

    agents = db.query(Agent).filter(Agent.name.in_(agent_names)).all()

    # Delete workflows
    for wf in workflows:
        db.delete(wf)
        print(f"  - Deleted workflow: {wf.name}")

    # Delete agent runs, threads, then agents
    for agent in agents:
        # Delete runs
        for run in agent.runs:
            db.delete(run)
        # Delete threads (cascades to messages)
        for thread in agent.threads:
            db.delete(thread)
        db.delete(agent)
        print(f"  - Deleted agent: {agent.name}")

    # Also clean up orphaned marketing thread
    marketing_threads = db.query(Thread).filter(Thread.title == "Marketing Demo Chat").all()
    for thread in marketing_threads:
        db.delete(thread)
        print(f"  - Deleted thread: {thread.title}")

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
    """Create a Supervisor thread with realistic chat messages."""
    print("  💬 Creating chat conversation...")

    thread = Thread(
        agent_id=supervisor.id,
        title="Marketing Demo Chat",
        thread_type=ThreadType.SUPER,
        active=True,
    )
    db.add(thread)
    db.flush()

    # Add messages
    base_time = datetime.now(timezone.utc) - timedelta(minutes=10)
    for i, msg in enumerate(CHAT_CONVERSATION):
        message = ThreadMessage(
            thread_id=thread.id,
            role=msg["role"],
            content=msg["content"],
            tool_calls=msg.get("tool_calls"),
            tool_call_id=msg.get("tool_call_id"),
            name=msg.get("name"),
            sent_at=base_time + timedelta(seconds=i * 30),
            processed=True,
        )
        db.add(message)

    db.commit()
    print(f"    ✓ Created thread with {len(CHAT_CONVERSATION)} messages")
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

        # Cleanup existing marketing data
        cleanup_marketing_data(db)

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
        print("   - Canvas: /canvas (select workflow from dropdown)")
        print("   - Chat: /chat (select 'Marketing Demo Chat' thread)")
        print("   - Dashboard: /dashboard")


if __name__ == "__main__":
    try:
        seed_marketing_data()
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
