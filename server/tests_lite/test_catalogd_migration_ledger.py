from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from zerg.catalogd.client import CatalogClient
from zerg.catalogd.client import CatalogRemoteError
from zerg.catalogd.server import CatalogDaemon


@pytest.fixture
def daemon_paths():
    root = Path("/tmp") / f"lhcd-migration-ledger-{uuid4().hex[:12]}"
    root.mkdir(mode=0o700)
    yield root / "live.db", root / "catalogd.sock"
    for path in root.iterdir():
        path.unlink(missing_ok=True)
    root.rmdir()


@pytest.mark.asyncio
async def test_legacy_migration_ledger_is_resumable_bounded_and_proof_driven(daemon_paths):
    database_path, socket_path = daemon_paths
    now = datetime.now(UTC).replace(microsecond=0)
    run_id = str(uuid4())
    first_session, second_session = str(uuid4()), str(uuid4())
    create = {
        "run_id": run_id,
        "legacy_high_watermark": "events.id=24354;source_lines.id=91822",
        "expected_session_count": 2,
        "created_at": now.isoformat(),
    }
    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        created = await client.call("migration.run.create.v2", create)
        replay = await client.call("migration.run.create.v2", create)
        assert created["created"] is True
        assert created["run"]["state"] == "inventory"
        assert replay["exact_replay"] is True
        with pytest.raises(CatalogRemoteError) as conflict:
            await client.call("migration.run.create.v2", {**create, "legacy_high_watermark": "changed"})
        assert conflict.value.code == "conflict"

        inventory = {
            "run_id": run_id,
            "sessions": [
                {"session_id": first_session, "source_expected": 3, "media_expected": 1},
                {"session_id": second_session, "source_expected": 2, "media_expected": 0},
            ],
            "registered_at": (now + timedelta(seconds=1)).isoformat(),
        }
        registered = await client.call("migration.session.register.batch.v2", inventory)
        inventory_replay = await client.call("migration.session.register.batch.v2", inventory)
        assert registered["registered"] == 2
        assert inventory_replay["exact_replay"] is True

        first_token = str(uuid4())
        claim_params = {
            "run_id": run_id,
            "worker_id": "migration-worker-1",
            "claim_token": first_token,
            "now": (now + timedelta(seconds=2)).isoformat(),
            "lease_seconds": 60,
            "limit": 1,
        }
        claimed = await client.call("migration.session.claim.v2", claim_params)
        claimed_replay = await client.call("migration.session.claim.v2", claim_params)
        assert len(claimed["claimed"]) == 1
        assert claimed["claimed"][0]["attempts"] == 1
        assert claimed_replay["exact_replay"] is True

        claimed_session = claimed["claimed"][0]
        verified = await client.call(
            "migration.session.complete.v2",
            {
                "run_id": run_id,
                "session_id": claimed_session["session_id"],
                "claim_token": first_token,
                "source_covered": claimed_session["source_expected"],
                "source_missing": 0,
                "media_covered": claimed_session["media_expected"],
                "media_missing": 0,
                "output_proof_hash": "a" * 64,
                "parity_proof_hash": "b" * 64,
                "completed_at": (now + timedelta(seconds=3)).isoformat(),
            },
        )
        assert verified["session"]["state"] == "verified"
        with pytest.raises(CatalogRemoteError) as changed_completion:
            await client.call(
                "migration.session.complete.v2",
                {
                    "run_id": run_id,
                    "session_id": claimed_session["session_id"],
                    "claim_token": first_token,
                    "source_covered": claimed_session["source_expected"],
                    "source_missing": 0,
                    "media_covered": claimed_session["media_expected"],
                    "media_missing": 0,
                    "output_proof_hash": "f" * 64,
                    "parity_proof_hash": "b" * 64,
                    "completed_at": (now + timedelta(seconds=3)).isoformat(),
                },
            )
        assert changed_completion.value.code == "conflict"

        second_token = str(uuid4())
        second_claim = await client.call(
            "migration.session.claim.v2",
            {**claim_params, "claim_token": second_token, "now": (now + timedelta(seconds=4)).isoformat()},
        )
        second_claimed = second_claim["claimed"][0]
        failure_params = {
            "run_id": run_id,
            "session_id": second_claimed["session_id"],
            "claim_token": second_token,
            "error_code": "legacy_source_unreadable",
            "error_message": "source range failed hash verification",
            "failed_at": (now + timedelta(seconds=5)).isoformat(),
            "retry_at": (now + timedelta(seconds=10)).isoformat(),
        }
        failed = await client.call("migration.session.fail.v2", failure_params)
        failed_replay = await client.call("migration.session.fail.v2", failure_params)
        assert failed["session"]["state"] == "degraded"
        assert failed_replay["exact_replay"] is True

        early_claim = await client.call(
            "migration.session.claim.v2",
            {**claim_params, "claim_token": str(uuid4()), "now": (now + timedelta(seconds=9)).isoformat()},
        )
        assert early_claim["claimed"] == []
        summary = await client.call("migration.run.summary.v2", {"run_id": run_id})
        assert summary["run"]["state"] == "degraded"
        assert summary["summary"]["state_counts"] == {
            "pending": 0,
            "migrating": 0,
            "verified": 1,
            "degraded": 1,
        }
        gaps = await client.call(
            "migration.gaps.list.v2",
            {"run_id": run_id, "after_session_id": None, "limit": 100},
        )
        assert [row["session_id"] for row in gaps["gaps"]] == [second_claimed["session_id"]]

        retry_token = str(uuid4())
        retry_claim = await client.call(
            "migration.session.claim.v2",
            {**claim_params, "claim_token": retry_token, "now": (now + timedelta(seconds=10)).isoformat()},
        )
        assert retry_claim["claimed"][0]["attempts"] == 2
        retried = retry_claim["claimed"][0]
        completed = await client.call(
            "migration.session.complete.v2",
            {
                "run_id": run_id,
                "session_id": retried["session_id"],
                "claim_token": retry_token,
                "source_covered": retried["source_expected"],
                "source_missing": 0,
                "media_covered": retried["media_expected"],
                "media_missing": 0,
                "output_proof_hash": "c" * 64,
                "parity_proof_hash": "d" * 64,
                "completed_at": (now + timedelta(seconds=11)).isoformat(),
            },
        )
        assert completed["session"]["state"] == "verified"
    finally:
        await client.close()
        await daemon.close()

    daemon = CatalogDaemon(database_path=database_path, socket_path=socket_path)
    await daemon.start()
    client = CatalogClient(socket_path)
    try:
        final = await client.call("migration.run.read.v2", {"run_id": run_id})
        assert final["run"]["state"] == "complete"
        assert final["summary"]["state_counts"]["verified"] == 2
        assert final["summary"]["source_covered"] == 5
        assert final["summary"]["media_covered"] == 1
        gaps = await client.call(
            "migration.gaps.list.v2",
            {"run_id": run_id, "after_session_id": None, "limit": 100},
        )
        assert gaps["gaps"] == []
    finally:
        await client.close()
        await daemon.close()
