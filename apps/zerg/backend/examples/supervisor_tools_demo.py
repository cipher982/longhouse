"""Demo of supervisor tools usage.

This script demonstrates how to use the supervisor tools to:
1. Spawn worker agents
2. List workers
3. Read worker results
4. Query worker metadata

Run this script with:
    uv run python examples/supervisor_tools_demo.py
"""

import asyncio
import tempfile

from zerg.connectors.context import set_credential_resolver
from zerg.connectors.resolver import CredentialResolver
from zerg.database import SessionLocal
from zerg.models.models import User
from zerg.services.worker_artifact_store import WorkerArtifactStore
from zerg.tools.builtin.supervisor_tools import (
    get_worker_metadata,
    list_workers,
    read_worker_result,
    spawn_worker,
)


async def main():
    """Run supervisor tools demo."""
    # Set up temporary artifact store
    with tempfile.TemporaryDirectory() as tmpdir:
        import os
        os.environ["SWARMLET_DATA_PATH"] = tmpdir

        # Create database session
        db = SessionLocal()
        try:
            # Get or create a test user
            user = db.query(User).first()
            if not user:
                print("No users found. Please create a user first.")
                return

            # Set up credential resolver context (required for spawn_worker)
            resolver = CredentialResolver(agent_id=1, db=db, owner_id=user.id)
            set_credential_resolver(resolver)

            print("=" * 60)
            print("SUPERVISOR TOOLS DEMO")
            print("=" * 60)

            # 1. Spawn a worker
            print("\n1. Spawning a worker to calculate 10 + 15...")
            result = spawn_worker(
                task="Calculate 10 + 15 and explain the result",
                model="gpt-5-nano"
            )
            print(result)

            # Extract worker_id from the result
            lines = result.split("\n")
            worker_line = [line for line in lines if "Worker" in line][0]
            worker_id = worker_line.split()[1]

            # 2. Spawn another worker
            print("\n2. Spawning another worker to write a haiku about AI...")
            result2 = spawn_worker(
                task="Write a haiku about artificial intelligence",
                model="gpt-5-nano"
            )
            print(result2)

            # 3. List all workers
            print("\n3. Listing all workers...")
            workers_list = list_workers(limit=10)
            print(workers_list)

            # 4. Read worker result
            print(f"\n4. Reading result from worker {worker_id}...")
            worker_result = read_worker_result(worker_id)
            print(worker_result)

            # 5. Get worker metadata
            print(f"\n5. Getting metadata for worker {worker_id}...")
            metadata = get_worker_metadata(worker_id)
            print(metadata)

            # 6. List only successful workers
            print("\n6. Listing only successful workers...")
            success_workers = list_workers(status="success", limit=5)
            print(success_workers)

            print("\n" + "=" * 60)
            print("DEMO COMPLETE")
            print("=" * 60)

        finally:
            set_credential_resolver(None)
            db.close()


if __name__ == "__main__":
    asyncio.run(main())
