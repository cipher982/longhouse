#!/usr/bin/env python3
"""Seed user context for David (or first user in database).

This script populates the user's context field with servers, integrations,
and preferences that will be injected into AI agent prompts.
"""

import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select
from zerg.database import SessionLocal
from zerg.models.models import User


DAVID_CONTEXT = {
    "display_name": "David",
    "role": "software engineer",
    "location": "NYC",
    "description": "I manage several servers running Docker containers for web apps and AI projects. I use Kopia for backups to a Synology NAS, track health with WHOOP, and keep notes in Obsidian.",
    "servers": [
        {
            "name": "cube",
            "ip": "100.70.237.79",
            "purpose": "Home GPU server - AI workloads, cameras (Frigate), Stop Sign Nanny, home automation",
            "platform": "Ubuntu 22.04",
            "notes": "Has RTX GPU, 32GB RAM. Runs Kopia backups to Bremen NAS.",
        },
        {
            "name": "clifford",
            "ip": "REDACTED_IP",
            "purpose": "Production VPS - 90% of web apps via Coolify",
            "platform": "Ubuntu 22.04 on Hetzner",
        },
        {
            "name": "zerg",
            "ip": "5.161.92.127",
            "purpose": "Zerg platform itself, dedicated project workloads",
            "platform": "Ubuntu 22.04 on Hetzner",
        },
        {
            "name": "slim",
            "ip": "135.181.204.0",
            "purpose": "EU VPS, mostly unused ($5/month placeholder)",
            "platform": "Ubuntu 22.04 on Hetzner",
        },
    ],
    "integrations": {
        "health_tracker": "WHOOP - tracks recovery score, sleep quality, strain",
        "notes": "Obsidian - personal knowledge base and project docs",
        "location": "Traccar - GPS tracking",
        "backups": "Kopia - backs up to Bremen NAS (Synology) with MinIO S3",
    },
    "custom_instructions": "I prefer concise responses. Get to the point quickly. Don't be verbose or bureaucratic.",
}


def main():
    """Seed user context for the first user (David)."""
    db = SessionLocal()
    try:
        # Find first user (or David specifically if email is known)
        result = db.execute(select(User).limit(1))
        user = result.scalar_one_or_none()

        if not user:
            print("ERROR: No users found in database. Create a user first.")
            return 1

        print(f"Seeding context for user: {user.email} (ID: {user.id})")

        # Update context
        user.context = DAVID_CONTEXT
        db.commit()

        print("SUCCESS: User context updated!")
        print(f"  - Display name: {DAVID_CONTEXT['display_name']}")
        print(f"  - Servers: {len(DAVID_CONTEXT['servers'])}")
        print(f"  - Integrations: {len(DAVID_CONTEXT['integrations'])}")

        return 0

    except Exception as e:
        print(f"ERROR: {e}")
        db.rollback()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
