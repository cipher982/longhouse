"""Example: Using CommisRunner and CommisArtifactStore.

This example demonstrates how to use the commis system to run disposable
fiche tasks and persist their results for later retrieval by concierges.
"""

import asyncio
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from zerg.crud import crud
from zerg.models.models import Base
from zerg.models_config import DEFAULT_COMMIS_MODEL_ID
from zerg.services.commis_artifact_store import CommisArtifactStore
from zerg.services.commis_runner import CommisRunner


async def main():
    """Run example commis tasks."""
    # Setup in-memory database for demo
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()

    try:
        # Create a test user
        user = crud.create_user(db, email="demo@example.com", provider=None, role="USER")
        db.commit()

        # Setup commis storage (use temp directory for demo)
        import tempfile

        tmpdir = tempfile.mkdtemp()
        print(f"Commis artifacts stored in: {tmpdir}")

        artifact_store = CommisArtifactStore(base_path=tmpdir)
        commis_runner = CommisRunner(artifact_store=artifact_store)

        # Example 1: Run a simple calculation task
        print("\n=== Example 1: Simple Task ===")
        result1 = await commis_runner.run_commis(
            db=db,
            task="Calculate 42 * 137 and explain the result",
            fiche=None,
            fiche_config={"model": DEFAULT_COMMIS_MODEL_ID, "owner_id": user.id},
        )

        print(f"Commis ID: {result1.commis_id}")
        print(f"Status: {result1.status}")
        print(f"Duration: {result1.duration_ms}ms")
        print(f"Result: {result1.result[:100]}..." if len(result1.result) > 100 else f"Result: {result1.result}")

        # Example 2: Run multiple commis (simulate concierge delegation)
        print("\n=== Example 2: Multiple Commis ===")
        tasks = [
            "Check system disk space",
            "Monitor memory usage",
            "Review CPU temperature",
        ]

        commis_ids = []
        for task in tasks:
            result = await commis_runner.run_commis(
                db=db,
                task=task,
                fiche=None,
                fiche_config={"model": DEFAULT_COMMIS_MODEL_ID, "owner_id": user.id},
            )
            commis_ids.append(result.commis_id)
            print(f"  - Completed: {task} ({result.commis_id})")

        # Example 3: Concierge queries commis results
        print("\n=== Example 3: Query Commis Results ===")
        commis = artifact_store.list_commis(status="success", limit=10)
        print(f"Found {len(commis)} successful commis")

        for commis in commis[:3]:  # Show first 3
            print(f"\nCommis: {commis['commis_id']}")
            print(f"  Task: {commis['task']}")
            print(f"  Duration: {commis.get('duration_ms', 0)}ms")

            # Read full result
            result_text = artifact_store.get_commis_result(commis["commis_id"])
            print(f"  Result: {result_text[:80]}..." if len(result_text) > 80 else f"  Result: {result_text}")

        # Example 4: Drill into specific commis artifacts
        print("\n=== Example 4: Detailed Commis Inspection ===")
        if commis_ids:
            commis_id = commis_ids[0]
            print(f"Inspecting commis: {commis_id}")

            # Read metadata
            metadata = artifact_store.get_commis_metadata(commis_id)
            print(f"  Created: {metadata['created_at']}")
            print(f"  Config: {metadata['config']}")

            # Read thread messages
            thread_content = artifact_store.read_commis_file(commis_id, "thread.jsonl")
            lines = thread_content.strip().split("\n")
            print(f"  Messages: {len(lines)} total")

            # Check for tool calls
            tool_calls_dir = Path(tmpdir) / commis_id / "tool_calls"
            if tool_calls_dir.exists():
                tool_files = list(tool_calls_dir.glob("*.txt"))
                print(f"  Tool calls: {len(tool_files)}")

        # Example 5: Search across commis
        print("\n=== Example 5: Search Commis ===")
        search_results = artifact_store.search_commis("system", file_glob="*.txt")
        print(f"Found {len(search_results)} matches for 'system'")
        for match in search_results[:3]:
            print(f"  - {match['commis_id']}: {match['content'][:60]}...")

        print("\n=== Summary ===")
        print(f"Total commis: {len(commis)}")
        print(f"Artifacts directory: {tmpdir}")
        print("\nCommis directory structure:")
        print("  commis/")
        print("  ├── index.json")
        print("  └── {commis_id}/")
        print("      ├── metadata.json")
        print("      ├── result.txt")
        print("      ├── thread.jsonl")
        print("      └── tool_calls/")
        print("          └── 001_tool_name.txt")

    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
