#!/usr/bin/env python3
"""Build a demo SQLite database with seeded Longhouse demo sessions.

Usage:
  uv run python server/scripts/build_demo_db.py
  uv run python server/scripts/build_demo_db.py --output /path/to/demo.db
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from datetime import timezone
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.orm import sessionmaker

from zerg.crud import get_user_by_email
from zerg.database import Base
from zerg.database import _ensure_agents_fts
from zerg.database import make_engine
from zerg.models.agents import AgentSession
from zerg.models.models import User
from zerg.services.agents import AgentsStore
from zerg.services.demo_sessions import build_demo_agent_sessions
from zerg.utils.time import utc_now_naive


def ensure_owner(db, email: str) -> User:
    user = get_user_by_email(db, email)
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


DEMO_PRESENTATION = {
    "demo-claude-01": (
        "Ship semantic search across 12,000 sessions",
        "Added embedding-backed recall with an FTS fallback, migration coverage, and a clean timeline toggle.",
    ),
    "demo-codex-01": (
        "Eliminate duplicate session ingest under load",
        "Traced a check-then-insert race and replaced it with a database-enforced idempotent ingest path.",
    ),
    "demo-antigravity-01": (
        "Fix the empty morning digest",
        "Found a timezone mismatch in the reporting window and verified the next scheduled delivery end to end.",
    ),
    "demo-claude-02": (
        "Add fast watermark detection to the photo pipeline",
        "Built a lightweight OpenCV preflight, friendly validation errors, and representative image tests.",
    ),
    "demo-codex-02": (
        "Polish the timeline search experience",
        "Wired the semantic-search control into the UI and validated responsive layout, types, and lint.",
    ),
    "demo-claude-03": (
        "Recover a production host from a memory spike",
        "Isolated an unbounded embedding backfill, restored headroom without downtime, "
        "and made processing stream in batches.",
    ),
    "demo-antigravity-02": (
        "Bring cross-session recall into the timeline",
        "Connected related past work to the active session so useful context is one click away.",
    ),
    "demo-claude-04": (
        "Find failure patterns across every project",
        "Compared recent incidents across repositories and distilled the recurring causes into practical guardrails.",
    ),
    "demo-codex-03": (
        "Repair the release image build",
        "Diagnosed the missing native dependency in CI and verified the rebuilt runtime image passed.",
    ),
    "demo-claude-05": (
        "Refresh the landing page visuals",
        "Auditing every marketing asset and rebuilding captures from a richer, current product story.",
    ),
}


def apply_demo_presentation(db) -> None:
    """Keep demo screenshots deterministic, readable, and independent of LLM jobs."""
    sessions = db.query(AgentSession).order_by(AgentSession.started_at.asc()).all()
    if len(sessions) != len(DEMO_PRESENTATION):
        raise RuntimeError(f"expected {len(DEMO_PRESENTATION)} demo sessions, found {len(sessions)}")
    for session, (title, summary) in zip(sessions, DEMO_PRESENTATION.values(), strict=True):
        session.summary_title = title
        session.anchor_title = title
        session.summary = summary
        session.summary_event_count = session.user_messages + session.assistant_messages + session.tool_calls


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    default_output = repo_root / "data" / "demo" / "longhouse-demo.db"

    parser = argparse.ArgumentParser(description="Build a demo SQLite database")
    parser.add_argument("--output", default=str(default_output), help="Output SQLite file path")
    parser.add_argument("--owner-email", default="dev@local", help="Owner email for seeded runs")
    parser.add_argument("--force", action="store_true", help="Overwrite existing DB if present")
    parser.add_argument(
        "--anchor",
        default=None,
        help=(
            "ISO timestamp to anchor seeded session times to (e.g. 2026-01-15T09:41:00). "
            "Fixing this makes captures byte-stable run-to-run; defaults to now."
        ),
    )

    args = parser.parse_args()
    output_path = Path(args.output).expanduser().resolve()

    anchor: datetime | None = None
    if args.anchor:
        anchor = datetime.fromisoformat(args.anchor)
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)

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

    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)
    db = SessionLocal()
    try:
        ensure_owner(db, args.owner_email)

        # Create FTS5 virtual table + triggers before inserting events
        _ensure_agents_fts(engine)

        # Seed demo agent sessions (AI session timeline)
        store = AgentsStore(db)
        for session in build_demo_agent_sessions(now=anchor):
            store.ingest_session(session)
        apply_demo_presentation(db)
        db.commit()

        print(f"Demo DB created: {output_path}")
    finally:
        db.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
