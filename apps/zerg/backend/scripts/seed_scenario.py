#!/usr/bin/env python3
"""Seed deterministic scenarios for demos and tests.

Usage (run from backend directory):
    cd apps/zerg/backend
    uv run python scripts/seed_scenario.py --list
    uv run python scripts/seed_scenario.py swarm-mvp
    uv run python scripts/seed_scenario.py swarm-mvp --owner-email dev@local
    uv run python scripts/seed_scenario.py swarm-mvp --no-clean
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from zerg.config import get_settings
from zerg.crud import crud
from zerg.database import get_session_factory
from zerg.models.models import User
from zerg.scenarios.seed import ScenarioError
from zerg.scenarios.seed import list_scenarios
from zerg.scenarios.seed import seed_scenario
from zerg.utils.time import utc_now_naive


def ensure_owner(db, email: str) -> User:
    user = crud.get_user_by_email(db, email)
    if user is not None:
        return user

    settings = get_settings()
    role = "ADMIN" if settings.dev_admin else "USER"

    user = User(
        email=email,
        provider="dev",
        provider_user_id=email,
        role=role,
        is_active=True,
        created_at=utc_now_naive(),
        updated_at=utc_now_naive(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed deterministic scenario data")
    parser.add_argument("scenario", nargs="?", help="Scenario name (YAML file without extension)")
    parser.add_argument("--list", action="store_true", help="List available scenarios")
    parser.add_argument("--owner-email", default="dev@local", help="Owner email for seeded runs")
    parser.add_argument("--no-clean", action="store_true", help="Skip cleanup of prior scenario runs")

    args = parser.parse_args()

    if args.list:
        scenarios = list_scenarios()
        if not scenarios:
            print("No scenarios found.")
        else:
            print("Available scenarios:")
            for name in scenarios:
                print(f"- {name}")
        return 0

    if not args.scenario:
        parser.error("scenario is required unless --list is provided")

    session_factory = get_session_factory()
    db = session_factory()
    try:
        owner = ensure_owner(db, args.owner_email)
        result = seed_scenario(db, args.scenario, owner_id=owner.id, clean=not args.no_clean)
        msg = f"Seeded scenario '{result['scenario']}' -> runs={result['runs']}, messages={result['messages']}, events={result['events']}"
        if result.get("worker_jobs"):
            msg += f", worker_jobs={result['worker_jobs']}"
        print(msg)
    except ScenarioError as exc:
        print(f"Scenario error: {exc}")
        return 2
    finally:
        db.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
