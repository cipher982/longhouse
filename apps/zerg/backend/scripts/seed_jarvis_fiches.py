"""Seed baseline Jarvis fiches for the Swarm Platform.

This script creates a set of pre-configured fiches designed to work with Jarvis:
- Morning Digest: Daily summary of health, calendar, and important info
- Health Watch: Periodic check-ins on WHOOP data and trends
- Finance Snapshot: Daily financial overview

Usage:
    uv run python scripts/seed_jarvis_fiches.py
"""

import sys
from pathlib import Path

# Add parent directory to path so we can import zerg modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from zerg.config import get_settings
from zerg.crud import crud
from zerg.database import get_db
from zerg.models.enums import FicheStatus
from zerg.models.models import Fiche
from zerg.models_config import DEFAULT_COMMIS_MODEL_ID

# Fiche definitions
JARVIS_FICHES = [
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
        "model": DEFAULT_COMMIS_MODEL_ID,
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
        "model": DEFAULT_COMMIS_MODEL_ID,
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
        "model": DEFAULT_COMMIS_MODEL_ID,
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
        "model": DEFAULT_COMMIS_MODEL_ID,
        "config": {"temperature": 0.3, "max_tokens": 150},
    },
]


def seed_fiches():
    """Create Jarvis baseline fiches in the database."""
    print("🌱 Seeding Jarvis baseline fiches...")

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

    # Create fiches
    created_count = 0
    updated_count = 0

    for fiche_def in JARVIS_FICHES:
        # Check if fiche already exists
        existing = db.query(Fiche).filter(
            Fiche.name == fiche_def["name"],
            Fiche.owner_id == jarvis_user.id,
        ).first()

        if existing:
            print(f"  ⚠️  Fiche already exists: {fiche_def['name']} (updating...)")
            # Update existing fiche
            existing.system_instructions = fiche_def["system_instructions"]
            existing.task_instructions = fiche_def["task_instructions"]
            existing.schedule = fiche_def["schedule"]
            existing.model = fiche_def["model"]
            existing.config = fiche_def.get("config", {})
            existing.status = FicheStatus.IDLE
            db.add(existing)
            updated_count += 1
        else:
            print(f"  ✨ Creating fiche: {fiche_def['name']}")
            # Create new fiche
            fiche = Fiche(
                owner_id=jarvis_user.id,
                name=fiche_def["name"],
                system_instructions=fiche_def["system_instructions"],
                task_instructions=fiche_def["task_instructions"],
                schedule=fiche_def["schedule"],
                model=fiche_def["model"],
                config=fiche_def.get("config", {}),
                status=FicheStatus.IDLE,
            )
            db.add(fiche)
            created_count += 1

    db.commit()

    print(f"\n✅ Seeding complete!")
    print(f"   Created: {created_count} fiches")
    print(f"   Updated: {updated_count} fiches")
    print(f"   Total: {created_count + updated_count} Jarvis fiches")
    print("\nThese fiches can now be dispatched via /api/jarvis/dispatch")
    print("Scheduled fiches will run automatically via APScheduler")


if __name__ == "__main__":
    try:
        seed_fiches()
    except Exception as e:
        print(f"❌ Error seeding fiches: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
