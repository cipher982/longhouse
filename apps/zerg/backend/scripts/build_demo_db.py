#!/usr/bin/env python3
"""Build a demo SQLite database with seeded data.

Usage:
  uv run python apps/zerg/backend/scripts/build_demo_db.py
  uv run python apps/zerg/backend/scripts/build_demo_db.py --output /path/to/demo.db
  uv run python apps/zerg/backend/scripts/build_demo_db.py --scenario swarm-mvp
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.orm import sessionmaker

from zerg.crud import crud
from zerg.database import Base
from zerg.database import make_engine
from zerg.models.agents import AgentsBase
from zerg.models.models import User
from zerg.scenarios.seed import ScenarioError
from zerg.scenarios.seed import seed_scenario
from zerg.services.agents_store import AgentsStore
from zerg.services.demo_sessions import build_demo_agent_sessions
from zerg.utils.time import utc_now_naive


def ensure_owner(db, email: str) -> User:
    user = crud.get_user_by_email(db, email)
    if user is not None:
        return user

    user = User(
        email=email,
        provider="dev",
        provider_user_id=email,
        role="ADMIN",
        is_active=True,
        created_at=utc_now_naive(),
        updated_at=utc_now_naive(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def main() -> int:
    repo_root = Path(__file__).resolve().parents[4]
    default_output = repo_root / "data" / "demo" / "longhouse-demo.db"

    parser = argparse.ArgumentParser(description="Build a demo SQLite database")
    parser.add_argument("--output", default=str(default_output), help="Output SQLite file path")
    parser.add_argument("--scenario", default="swarm-mvp", help="Scenario name to seed (default: swarm-mvp)")
    parser.add_argument("--skip-scenario", action="store_true", help="Skip scenario seeding")
    parser.add_argument("--owner-email", default="dev@local", help="Owner email for seeded runs")
    parser.add_argument("--force", action="store_true", help="Overwrite existing DB if present")

    args = parser.parse_args()
    output_path = Path(args.output).expanduser().resolve()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        if not args.force:
            print(f"ERROR: Demo DB already exists: {output_path}")
            print("Re-run with --force to overwrite.")
            return 2
        output_path.unlink()

    db_url = f"sqlite:///{output_path}"
    engine = make_engine(db_url).execution_options(schema_translate_map={"zerg": None, "agents": None})
    Base.metadata.create_all(bind=engine)
    AgentsBase.metadata.create_all(bind=engine)

    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)
    db = SessionLocal()
    try:
        owner = ensure_owner(db, args.owner_email)

        if not args.skip_scenario:
            seed_scenario(db, args.scenario, owner_id=owner.id, clean=True)

        store = AgentsStore(db)
        for session in build_demo_agent_sessions():
            store.ingest_session(session)

        print(f"Demo DB created: {output_path}")
    except ScenarioError as exc:
        print(f"Scenario error: {exc}")
        return 2
    finally:
        db.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
