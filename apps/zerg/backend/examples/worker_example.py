"""Example: Using WorkerRunner and WorkerArtifactStore.

This example demonstrates how to use the worker system to run disposable
agent tasks and persist their results for later retrieval by supervisors.
"""

import asyncio
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from zerg.crud import crud
from zerg.models.models import Base
from zerg.services.worker_artifact_store import WorkerArtifactStore
from zerg.services.worker_runner import WorkerRunner


async def main():
    """Run example worker tasks."""
    # Setup in-memory database for demo
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()

    try:
        # Create a test user
        user = crud.create_user(db, email="demo@example.com", provider=None, role="USER")
        db.commit()

        # Setup worker storage (use temp directory for demo)
        import tempfile

        tmpdir = tempfile.mkdtemp()
        print(f"Worker artifacts stored in: {tmpdir}")

        artifact_store = WorkerArtifactStore(base_path=tmpdir)
        worker_runner = WorkerRunner(artifact_store=artifact_store)

        # Example 1: Run a simple calculation task
        print("\n=== Example 1: Simple Task ===")
        result1 = await worker_runner.run_worker(
            db=db,
            task="Calculate 42 * 137 and explain the result",
            agent=None,
            agent_config={"model": "gpt-4o-mini", "owner_id": user.id},
        )

        print(f"Worker ID: {result1.worker_id}")
        print(f"Status: {result1.status}")
        print(f"Duration: {result1.duration_ms}ms")
        print(f"Result: {result1.result[:100]}..." if len(result1.result) > 100 else f"Result: {result1.result}")

        # Example 2: Run multiple workers (simulate supervisor delegation)
        print("\n=== Example 2: Multiple Workers ===")
        tasks = [
            "Check system disk space",
            "Monitor memory usage",
            "Review CPU temperature",
        ]

        worker_ids = []
        for task in tasks:
            result = await worker_runner.run_worker(
                db=db,
                task=task,
                agent=None,
                agent_config={"model": "gpt-4o-mini", "owner_id": user.id},
            )
            worker_ids.append(result.worker_id)
            print(f"  - Completed: {task} ({result.worker_id})")

        # Example 3: Supervisor queries worker results
        print("\n=== Example 3: Query Worker Results ===")
        workers = artifact_store.list_workers(status="success", limit=10)
        print(f"Found {len(workers)} successful workers")

        for worker in workers[:3]:  # Show first 3
            print(f"\nWorker: {worker['worker_id']}")
            print(f"  Task: {worker['task']}")
            print(f"  Duration: {worker.get('duration_ms', 0)}ms")

            # Read full result
            result_text = artifact_store.get_worker_result(worker["worker_id"])
            print(f"  Result: {result_text[:80]}..." if len(result_text) > 80 else f"  Result: {result_text}")

        # Example 4: Drill into specific worker artifacts
        print("\n=== Example 4: Detailed Worker Inspection ===")
        if worker_ids:
            worker_id = worker_ids[0]
            print(f"Inspecting worker: {worker_id}")

            # Read metadata
            metadata = artifact_store.get_worker_metadata(worker_id)
            print(f"  Created: {metadata['created_at']}")
            print(f"  Config: {metadata['config']}")

            # Read thread messages
            thread_content = artifact_store.read_worker_file(worker_id, "thread.jsonl")
            lines = thread_content.strip().split("\n")
            print(f"  Messages: {len(lines)} total")

            # Check for tool calls
            tool_calls_dir = Path(tmpdir) / worker_id / "tool_calls"
            if tool_calls_dir.exists():
                tool_files = list(tool_calls_dir.glob("*.txt"))
                print(f"  Tool calls: {len(tool_files)}")

        # Example 5: Search across workers
        print("\n=== Example 5: Search Workers ===")
        search_results = artifact_store.search_workers("system", file_glob="*.txt")
        print(f"Found {len(search_results)} matches for 'system'")
        for match in search_results[:3]:
            print(f"  - {match['worker_id']}: {match['content'][:60]}...")

        print("\n=== Summary ===")
        print(f"Total workers: {len(workers)}")
        print(f"Artifacts directory: {tmpdir}")
        print("\nWorker directory structure:")
        print("  workers/")
        print("  ├── index.json")
        print("  └── {worker_id}/")
        print("      ├── metadata.json")
        print("      ├── result.txt")
        print("      ├── thread.jsonl")
        print("      └── tool_calls/")
        print("          └── 001_tool_name.txt")

    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
