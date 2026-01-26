"""Demo of concierge tools usage.

This script demonstrates how to use the concierge tools to:
1. Spawn commis fiches
2. List commis
3. Read commis results
4. Query commis metadata

Run this script with:
    uv run python examples/concierge_tools_demo.py
"""

import asyncio
import tempfile

from zerg.connectors.context import set_credential_resolver
from zerg.connectors.resolver import CredentialResolver
from zerg.database import SessionLocal
from zerg.models.models import User
from zerg.models_config import TIER_3  # Use cheapest model for demo
from zerg.services.commis_artifact_store import CommisArtifactStore
from zerg.tools.builtin.concierge_tools import (
    get_commis_metadata,
    list_commis,
    read_commis_result,
    spawn_commis,
)


async def main():
    """Run concierge tools demo."""
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

            # Set up credential resolver context (required for spawn_commis)
            resolver = CredentialResolver(fiche_id=1, db=db, owner_id=user.id)
            set_credential_resolver(resolver)

            print("=" * 60)
            print("CONCIERGE TOOLS DEMO")
            print("=" * 60)

            # 1. Spawn a commis
            print("\n1. Spawning a commis to calculate 10 + 15...")
            result = spawn_commis(
                task="Calculate 10 + 15 and explain the result",
                model=TIER_3
            )
            print(result)

            # Extract commis_id from the result
            lines = result.split("\n")
            commis_line = [line for line in lines if "Commis" in line][0]
            commis_id = commis_line.split()[1]

            # 2. Spawn another commis
            print("\n2. Spawning another commis to write a haiku about AI...")
            result2 = spawn_commis(
                task="Write a haiku about artificial intelligence",
                model=TIER_3
            )
            print(result2)

            # 3. List all commis
            print("\n3. Listing all commis...")
            commis_list = list_commis(limit=10)
            print(commis_list)

            # 4. Read commis result
            print(f"\n4. Reading result from commis {commis_id}...")
            commis_result = read_commis_result(commis_id)
            print(commis_result)

            # 5. Get commis metadata
            print(f"\n5. Getting metadata for commis {commis_id}...")
            metadata = get_commis_metadata(commis_id)
            print(metadata)

            # 6. List only successful commis
            print("\n6. Listing only successful commis...")
            success_commis = list_commis(status="success", limit=5)
            print(success_commis)

            print("\n" + "=" * 60)
            print("DEMO COMPLETE")
            print("=" * 60)

        finally:
            set_credential_resolver(None)
            db.close()


if __name__ == "__main__":
    asyncio.run(main())
