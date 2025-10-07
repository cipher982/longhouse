"""Seed baseline Jarvis agents for the Swarm Platform.

This script creates a set of pre-configured agents designed to work with Jarvis:
- Morning Digest: Daily summary of health, calendar, and important info
- Health Watch: Periodic check-ins on WHOOP data and trends
- Finance Snapshot: Daily financial overview

Usage:
    uv run python scripts/seed_jarvis_agents.py
"""

import sys
from pathlib import Path

# Add parent directory to path so we can import zerg modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from zerg.config import get_settings
from zerg.crud import crud
from zerg.database import get_db
from zerg.models.enums import AgentStatus
from zerg.models.models import Agent

# Agent definitions
JARVIS_AGENTS = [
    {
        "name": "Morning Digest",
        "system_instructions": """You are the Morning Digest assistant for Jarvis.

Your role is to provide a concise, actionable summary of:
1. Health metrics from WHOOP (recovery, sleep, strain)
2. Today's calendar and upcoming commitments
3. Weather forecast for the day
4. Any urgent notifications or reminders

Be brief, positive, and focus on actionable insights. Limit response to 3-4 paragraphs.""",
        "task_instructions": """Generate my morning digest:
1. Check my WHOOP recovery score and sleep quality
2. Summarize today's calendar appointments
3. Check the weather forecast
4. Highlight any urgent tasks or notifications

Present this as a friendly morning briefing.""",
        "schedule": "0 7 * * *",  # 7 AM daily
        "model": "gpt-4o-mini",
        "config": {"temperature": 0.7, "max_tokens": 500},
    },
    {
        "name": "Health Watch",
        "system_instructions": """You are the Health Watch assistant for Jarvis.

Your role is to monitor and analyze health trends from WHOOP data:
- Recovery trends over the past week
- Sleep quality patterns
- Strain and exertion levels
- Recommendations for optimization

Provide data-driven insights with specific recommendations.""",
        "task_instructions": """Analyze my health trends:
1. Review WHOOP data for the past 7 days
2. Identify patterns in recovery, sleep, and strain
3. Compare to my typical baseline
4. Provide 2-3 actionable recommendations

Be specific with numbers and trends.""",
        "schedule": "0 20 * * *",  # 8 PM daily
        "model": "gpt-4o-mini",
        "config": {"temperature": 0.5, "max_tokens": 400},
    },
    {
        "name": "Weekly Planning Assistant",
        "system_instructions": """You are the Weekly Planning assistant for Jarvis.

Your role is to help plan and organize the upcoming week:
- Review calendar and commitments
- Identify time blocks for focused work
- Suggest prioritization of tasks
- Check for schedule conflicts

Be strategic and help optimize time management.""",
        "task_instructions": """Help me plan the upcoming week:
1. Review calendar for next 7 days
2. Identify key commitments and deadlines
3. Suggest optimal time blocks for focused work
4. Flag any potential conflicts or overcommitments

Provide a structured weekly overview.""",
        "schedule": "0 18 * * 0",  # 6 PM every Sunday
        "model": "gpt-4o-mini",
        "config": {"temperature": 0.6, "max_tokens": 600},
    },
    {
        "name": "Quick Status Check",
        "system_instructions": """You are a quick status assistant for Jarvis.

Provide ultra-concise status updates on demand:
- Current time and date
- Weather right now
- Any urgent notifications
- Today's next calendar event

Respond in 2-3 sentences max. Be direct and efficient.""",
        "task_instructions": """Quick status update:
1. Current time and weather
2. Next calendar event (if any in next 2 hours)
3. Any urgent notifications

Keep it to 2-3 sentences total.""",
        "schedule": None,  # On-demand only
        "model": "gpt-4o-mini",
        "config": {"temperature": 0.3, "max_tokens": 150},
    },
]


def seed_agents():
    """Create Jarvis baseline agents in the database."""
    print("🌱 Seeding Jarvis baseline agents...")

    # Get database session
    db = next(get_db())

    # Get or create Jarvis user
    jarvis_email = "jarvis@swarm.local"
    jarvis_user = crud.get_user_by_email(db, jarvis_email)

    if not jarvis_user:
        print(f"Creating Jarvis user: {jarvis_email}")
        jarvis_user = crud.create_user(
            db,
            email=jarvis_email,
            provider="jarvis",
            role="ADMIN",
        )
        jarvis_user.display_name = "Jarvis Assistant"
        db.add(jarvis_user)
        db.commit()
        db.refresh(jarvis_user)
    else:
        print(f"Found existing Jarvis user: {jarvis_email}")

    # Create agents
    created_count = 0
    updated_count = 0

    for agent_def in JARVIS_AGENTS:
        # Check if agent already exists
        existing = db.query(Agent).filter(
            Agent.name == agent_def["name"],
            Agent.owner_id == jarvis_user.id,
        ).first()

        if existing:
            print(f"  ⚠️  Agent already exists: {agent_def['name']} (updating...)")
            # Update existing agent
            existing.system_instructions = agent_def["system_instructions"]
            existing.task_instructions = agent_def["task_instructions"]
            existing.schedule = agent_def["schedule"]
            existing.model = agent_def["model"]
            existing.config = agent_def.get("config", {})
            existing.status = AgentStatus.IDLE
            db.add(existing)
            updated_count += 1
        else:
            print(f"  ✨ Creating agent: {agent_def['name']}")
            # Create new agent
            agent = Agent(
                owner_id=jarvis_user.id,
                name=agent_def["name"],
                system_instructions=agent_def["system_instructions"],
                task_instructions=agent_def["task_instructions"],
                schedule=agent_def["schedule"],
                model=agent_def["model"],
                config=agent_def.get("config", {}),
                status=AgentStatus.IDLE,
            )
            db.add(agent)
            created_count += 1

    db.commit()

    print(f"\n✅ Seeding complete!")
    print(f"   Created: {created_count} agents")
    print(f"   Updated: {updated_count} agents")
    print(f"   Total: {created_count + updated_count} Jarvis agents")
    print("\nThese agents can now be dispatched via /api/jarvis/dispatch")
    print("Scheduled agents will run automatically via APScheduler")


if __name__ == "__main__":
    try:
        seed_agents()
    except Exception as e:
        print(f"❌ Error seeding agents: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
